"""Tests for agent-bus server — hash chain, query, verify, and status tools."""

import json
import threading
import time
from pathlib import Path

import pytest
import server as ab


# ── log_event ────────────────────────────────────────────────────────────────


def test_log_event_returns_id_and_logged(comms_dir):
    result = ab.log_event(
        event_type="task.completed",
        source="dev",
        summary="build done",
    )
    assert result["logged"] is True
    assert "id" in result
    assert result["scope"] == "cross-agent"


def test_log_event_cross_agent_routes_to_cross_agent_file(comms_dir):
    ab.log_event(event_type="task.completed", source="dev", summary="s")
    files = list((comms_dir / "logs").glob("*-cross-agent.jsonl"))
    assert len(files) == 1


def test_log_event_unknown_scope_falls_back_to_session(comms_dir):
    result = ab.log_event(
        event_type="memory.written",
        source="dev",
        summary="s",
        scope="session",
    )
    assert result["scope"] == "session"
    files = list((comms_dir / "logs").glob("*-session.jsonl"))
    assert len(files) == 1


def test_log_event_hash_chain_links_events(comms_dir):
    ab.log_event(event_type="task.completed", source="dev", summary="first")
    ab.log_event(event_type="task.completed", source="dev", summary="second")

    log_file = sorted((comms_dir / "logs").glob("*-cross-agent.jsonl"))[0]
    lines = [l for l in log_file.read_text().splitlines() if l.strip()]
    assert len(lines) == 2

    first = json.loads(lines[0])
    second = json.loads(lines[1])

    assert "prev_hash" not in first
    expected_hash = ab._sha256(lines[0])
    assert second["prev_hash"] == expected_hash


def test_log_event_metadata_stored(comms_dir):
    meta = {"build_id": "abc123", "phase": 1}
    result = ab.log_event(
        event_type="task.completed",
        source="dev",
        summary="s",
        metadata=meta,
    )
    event = ab.get_event(result["id"])
    assert event["metadata"]["build_id"] == "abc123"


# ── verify_chain ─────────────────────────────────────────────────────────────


def test_verify_chain_clean(comms_dir):
    ab.log_event(event_type="task.completed", source="dev", summary="a")
    ab.log_event(event_type="task.completed", source="dev", summary="b")

    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = ab.verify_chain(scope="cross-agent", date=today)

    assert result["total_events"] == 2
    assert result["chain_breaks"] == 0
    assert result["sig_failures"] == 0


def test_verify_chain_detects_tampered_hash(comms_dir):
    ab.log_event(event_type="task.completed", source="dev", summary="a")
    ab.log_event(event_type="task.completed", source="dev", summary="b")

    log_file = sorted((comms_dir / "logs").glob("*-cross-agent.jsonl"))[0]
    lines = log_file.read_text().splitlines()
    # Tamper with first line
    first_event = json.loads(lines[0])
    first_event["summary"] = "tampered"
    lines[0] = json.dumps(first_event)
    log_file.write_text("\n".join(lines) + "\n")

    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = ab.verify_chain(scope="cross-agent", date=today)
    assert result["chain_breaks"] >= 1


def test_verify_chain_missing_file(comms_dir):
    result = ab.verify_chain(scope="cross-agent", date="2000-01-01")
    assert result["error"] == "file_not_found"
    assert result["total_events"] == 0


def test_verify_chain_invalid_date_raises(comms_dir):
    with pytest.raises(ValueError, match="Invalid date format"):
        ab.verify_chain(date="not-a-date")


# ── query_events ──────────────────────────────────────────────────────────────


def test_query_events_returns_most_recent_first(comms_dir):
    ab.log_event(event_type="task.completed", source="dev", summary="first")
    time.sleep(0.01)
    ab.log_event(event_type="task.completed", source="dev", summary="second")

    events = ab.query_events(scope="cross-agent")
    assert len(events) == 2
    assert events[0]["summary"] == "second"
    assert events[1]["summary"] == "first"


def test_query_events_filter_by_source(comms_dir):
    ab.log_event(event_type="task.completed", source="dev", summary="s1")
    ab.log_event(event_type="task.completed", source="security", summary="s2")

    events = ab.query_events(source="security")
    assert len(events) == 1
    assert events[0]["source"] == "security"


def test_query_events_filter_by_event_type(comms_dir):
    ab.log_event(event_type="task.completed", source="dev", summary="s1")
    ab.log_event(event_type="audit.requested", source="dev", summary="s2")

    events = ab.query_events(event_type="audit.requested")
    assert len(events) == 1
    assert events[0]["event"] == "audit.requested"


def test_query_events_limit(comms_dir):
    for i in range(10):
        ab.log_event(event_type="task.completed", source="dev", summary=f"e{i}")

    events = ab.query_events(limit=3)
    assert len(events) == 3


# ── get_event ─────────────────────────────────────────────────────────────────


def test_get_event_found(comms_dir):
    result = ab.log_event(event_type="task.completed", source="dev", summary="x")
    event = ab.get_event(result["id"])
    assert event is not None
    assert event["id"] == result["id"]
    assert event["summary"] == "x"


def test_get_event_not_found(comms_dir):
    result = ab.get_event("00000000-0000-0000-0000-000000000000")
    assert result is None


# ── get_status ────────────────────────────────────────────────────────────────


def test_get_status_empty(comms_dir):
    status = ab.get_status()
    assert status["events_today"] == 0
    assert status["logs"] is None


def test_get_status_with_events(comms_dir):
    ab.log_event(event_type="task.completed", source="dev", summary="s")
    status = ab.get_status()
    assert status["events_today"] == 1
    assert status["logs"] is not None


# ── lock prevents race ────────────────────────────────────────────────────────


def test_append_event_lock_prevents_interleave(comms_dir):
    """Two threads appending concurrently should not produce interleaved JSON lines."""
    errors = []

    def append_many():
        for _ in range(20):
            try:
                ab.log_event(event_type="task.completed", source="dev", summary="race")
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=append_many) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors

    log_file = sorted((comms_dir / "logs").glob("*-cross-agent.jsonl"))[0]
    lines = [l for l in log_file.read_text().splitlines() if l.strip()]
    # Every line must be valid JSON
    for line in lines:
        json.loads(line)  # raises if malformed

    assert len(lines) == 80
