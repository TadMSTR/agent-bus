"""The shared append path: cross-process chaining, mixed writers, tail reads.

Covers finding D. Two of the three writers previously appended with no ``prev_hash``
and no lock, and the lock that did exist was a ``threading.Lock`` while 8+ separate
processes write these files.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import agent_bus_client
import event_log
import server as ab

REPO_ROOT = Path(__file__).parent.parent

# Appends from N real processes, not threads — a threading.Lock cannot serialise these.
_CHILD = """
import sys
from pathlib import Path
from event_log import append_event, build_event

logs_dir, count, source = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
for i in range(count):
    append_event(build_event("task.completed", source, f"{source}-{i}"), "cross-agent", logs_dir)
"""


def _today_log(logs_dir):
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return logs_dir / f"{date}-cross-agent.jsonl"


def _assert_chain_intact(path):
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert lines, "no events written"
    for i, line in enumerate(lines):
        event = json.loads(line)  # every line must be complete JSON
        if i == 0:
            assert "prev_hash" not in event
        else:
            assert event["prev_hash"] == event_log.sha256(lines[i - 1]), f"chain broken at line {i}"
    return lines


# ── cross-process ─────────────────────────────────────────────────────────────


def test_concurrent_processes_produce_an_unbroken_chain(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    writers, per_writer = 4, 15

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(logs_dir), str(per_writer), f"w{n}"],
            cwd=REPO_ROOT,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_ROOT)},
        )
        for n in range(writers)
    ]
    for p in procs:
        assert p.wait(timeout=60) == 0

    lines = _assert_chain_intact(_today_log(logs_dir))
    assert len(lines) == writers * per_writer


# ── mixed writers ─────────────────────────────────────────────────────────────


def test_client_event_chains_onto_a_server_event(comms_dir):
    ab.log_event(event_type="task.completed", source="server", summary="first")
    agent_bus_client.log_event(event_type="task.completed", source="cron", summary="second")

    lines = _assert_chain_intact(_today_log(comms_dir / "logs"))
    assert len(lines) == 2
    assert json.loads(lines[1])["source"] == "cron"


def test_verify_chain_reports_no_breaks_across_all_three_writers(comms_dir):
    """server + agent_bus_client + reconcile's append, interleaved, in one file."""
    logs_dir = comms_dir / "logs"

    ab.log_event(event_type="task.completed", source="server", summary="a")
    agent_bus_client.log_event(event_type="task.completed", source="cron", summary="b")
    event_log.append_event(
        event_log.build_event(
            event_type="artifact.untracked",
            source="reconciliation",
            summary="c",
            artifact_path="/tmp/x",
        ),
        "cross-agent",
        logs_dir,
    )
    ab.log_event(event_type="task.completed", source="server", summary="d")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = ab.verify_chain(scope="cross-agent", date=today)

    assert result["total_events"] == 4
    assert result["chain_breaks"] == 0
    assert result["verified"] == 4


# ── tail reads ────────────────────────────────────────────────────────────────


def test_last_line_matches_a_full_read_on_a_multi_chunk_file(tmp_path):
    path = tmp_path / "big.jsonl"
    lines = [json.dumps({"n": i, "pad": "x" * 200}) for i in range(500)]
    path.write_text("\n".join(lines) + "\n")

    assert path.stat().st_size > event_log._TAIL_CHUNK  # actually exercises the loop
    assert event_log.last_line(path) == lines[-1]


def test_last_line_handles_a_line_longer_than_the_chunk(tmp_path):
    path = tmp_path / "long.jsonl"
    lines = ["short", json.dumps({"pad": "y" * (event_log._TAIL_CHUNK * 3)})]
    path.write_text("\n".join(lines) + "\n")

    assert event_log.last_line(path) == lines[-1]


def test_last_line_ignores_trailing_blank_lines(tmp_path):
    path = tmp_path / "blanks.jsonl"
    path.write_text('{"a": 1}\n{"b": 2}\n\n\n')
    assert event_log.last_line(path) == '{"b": 2}'


@pytest.mark.parametrize("content", ["", "\n", "   \n\n"])
def test_last_line_returns_none_for_empty_files(tmp_path, content):
    path = tmp_path / "empty.jsonl"
    path.write_text(content)
    assert event_log.last_line(path) is None


def test_last_line_returns_none_for_missing_file(tmp_path):
    assert event_log.last_line(tmp_path / "nope.jsonl") is None


def test_append_creates_the_logs_dir(tmp_path):
    logs_dir = tmp_path / "not-yet"
    event_log.append_event(
        event_log.build_event("task.completed", "s", "x"), "cross-agent", logs_dir
    )
    assert _today_log(logs_dir).exists()
