"""ntfy and webhook emitters — filtering, header-injection cleaning, isolation.

These are the two remaining subprocess call sites (curl). They are pre-existing
behaviour, pinned here because Phase 1 removed the third one and a future cleanup
should not quietly change what these do.
"""

import json

import server as ab


def _capture(monkeypatch):
    calls = []
    monkeypatch.setattr(ab.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    return calls


# ── ntfy ──────────────────────────────────────────────────────────────────────


def test_ntfy_is_skipped_without_a_url(comms_dir, monkeypatch):
    calls = _capture(monkeypatch)
    ab.emit_ntfy({"event": "task.failed", "source": "s", "summary": "x", "target": None})
    assert calls == []


def test_ntfy_fires_for_high_priority_events(comms_dir, monkeypatch):
    monkeypatch.setattr(ab, "NTFY_URL", "https://ntfy.example.com/c")
    calls = _capture(monkeypatch)

    ab.log_event(event_type="task.failed", source="dev", summary="broke")

    assert len(calls) == 1
    assert "https://ntfy.example.com/c" in calls[0]


def test_ntfy_does_not_fire_for_ordinary_events(comms_dir, monkeypatch):
    monkeypatch.setattr(ab, "NTFY_URL", "https://ntfy.example.com/c")
    calls = _capture(monkeypatch)

    ab.log_event(event_type="task.completed", source="dev", summary="fine")

    assert calls == []


def test_ntfy_strips_newlines_to_prevent_header_injection(comms_dir, monkeypatch):
    monkeypatch.setattr(ab, "NTFY_URL", "https://ntfy.example.com/c")
    calls = _capture(monkeypatch)

    ab.emit_ntfy(
        {
            "event": "task.failed\r\nX-Injected: yes",
            "source": "dev\r\nevil",
            "summary": "line1\nline2",
            "target": None,
        }
    )

    argv = calls[0]
    for arg in argv:
        assert "\r" not in arg
        assert "\n" not in arg


def test_ntfy_failure_does_not_propagate(comms_dir, monkeypatch):
    monkeypatch.setattr(ab, "NTFY_URL", "https://ntfy.example.com/c")
    monkeypatch.setattr(ab.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    result = ab.log_event(event_type="task.failed", source="dev", summary="s")

    assert result["logged"] is True


# ── webhook ───────────────────────────────────────────────────────────────────


def test_webhook_is_skipped_without_a_url(comms_dir, monkeypatch):
    calls = _capture(monkeypatch)
    ab.emit_webhook({"event": "handoff.created"})
    assert calls == []


def test_webhook_fires_only_for_subscribed_events(comms_dir, monkeypatch):
    monkeypatch.setattr(ab, "WEBHOOK_URL", "http://127.0.0.1:8499")
    monkeypatch.setattr(ab, "WEBHOOK_EVENTS", {"handoff.created"})
    calls = _capture(monkeypatch)

    ab.emit_webhook({"event": "task.completed"})
    assert calls == []

    ab.emit_webhook({"event": "handoff.created"})
    assert len(calls) == 1


def test_webhook_wildcard_matches_everything(comms_dir, monkeypatch):
    monkeypatch.setattr(ab, "WEBHOOK_URL", "http://127.0.0.1:8499")
    monkeypatch.setattr(ab, "WEBHOOK_EVENTS", {"*"})
    calls = _capture(monkeypatch)

    ab.emit_webhook({"event": "anything.at.all"})

    assert len(calls) == 1


def test_webhook_sends_the_event_as_json(comms_dir, monkeypatch):
    monkeypatch.setattr(ab, "WEBHOOK_URL", "http://127.0.0.1:8499")
    monkeypatch.setattr(ab, "WEBHOOK_EVENTS", {"*"})
    calls = _capture(monkeypatch)

    ab.emit_webhook({"event": "x", "id": "abc"})

    assert json.loads(calls[0][-1])["id"] == "abc"


def test_webhook_failure_does_not_propagate(comms_dir, monkeypatch):
    monkeypatch.setattr(ab, "WEBHOOK_URL", "http://127.0.0.1:8499")
    monkeypatch.setattr(ab, "WEBHOOK_EVENTS", {"*"})
    monkeypatch.setattr(ab.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    result = ab.log_event(event_type="task.completed", source="dev", summary="s")

    assert result["logged"] is True
