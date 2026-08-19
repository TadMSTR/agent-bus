"""The NATS publish path.

Covers finding A (credential in argv) and finding C (blocking subprocess in an async
task). The credential is now a connect() keyword argument and publishing is an enqueue
onto a background thread, so nothing about NATS sits on log_event's critical path.
"""

import asyncio
import json
from datetime import datetime, timezone

import server as ab
from nats_publisher import NatsPublisher


def _today_log(comms_dir):
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return comms_dir / "logs" / f"{date}-cross-agent.jsonl"


# ── the credential must never reach a command line ────────────────────────────


def test_logging_an_event_spawns_no_subprocess(comms_dir, monkeypatch):
    """The old path shelled out to `nats pub --password <plaintext>` per event."""
    calls = []
    monkeypatch.setattr(ab.subprocess, "run", lambda *a, **k: calls.append(a))

    ab.log_event(event_type="task.completed", source="dev", summary="s")

    assert calls == []


def test_federating_spawns_no_subprocess(comms_dir, publisher, monkeypatch):
    """The replay loop spawned one `nats pub` per event — ~2,700 per cycle."""
    calls = []
    monkeypatch.setattr(ab.subprocess, "run", lambda *a, **k: calls.append(a))
    log = comms_dir / "logs" / "2026-08-01-cross-agent.jsonl"
    log.write_text("".join(json.dumps({"id": str(i), "ts": "t"}) + "\n" for i in range(10)))

    _, published = ab.federate_once({"version": 2, "files": {}})

    assert published == 10
    assert calls == []


# ── NATS failure must never affect the authoritative write ────────────────────


def test_event_is_still_written_when_publishing_raises(comms_dir, publisher):
    publisher.raises = True

    result = ab.log_event(event_type="task.completed", source="dev", summary="s")

    assert result["logged"] is True
    lines = [ln for ln in _today_log(comms_dir).read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["summary"] == "s"


def test_log_event_does_not_raise_when_publishing_raises(comms_dir, publisher):
    publisher.raises = True
    ab.log_event(event_type="task.completed", source="dev", summary="s")  # must not raise


def test_event_is_still_written_when_publishing_is_refused(comms_dir, publisher):
    publisher.accept = False

    result = ab.log_event(event_type="task.completed", source="dev", summary="s")

    assert result["logged"] is True
    assert _today_log(comms_dir).exists()


def test_emit_nats_reports_refusal_without_raising(comms_dir, publisher):
    publisher.accept = False
    assert ab.emit_nats({"id": "x"}) is False


# ── publisher unit behaviour ──────────────────────────────────────────────────


def _publisher(**kw):
    kw.setdefault("url", "nats://127.0.0.1:14222")
    kw.setdefault("user", "agent-bus")
    kw.setdefault("password", "pw")
    kw.setdefault("subject", "events.agent-bus.test")
    return NatsPublisher(**kw)


def test_publisher_is_disabled_without_a_credential():
    pub = _publisher(password="")

    assert pub.enabled is False
    assert pub.publish({"id": "x"}) is False
    assert pub._thread is None  # never even started a thread


def test_publish_after_close_is_refused_not_raised():
    pub = _publisher()
    pub.close()
    assert pub.publish({"id": "x"}) is False


def test_offer_drops_instead_of_blocking_when_saturated():
    """A sustained outage must cost bounded memory, not unbounded growth."""
    pub = _publisher(queue_size=1)
    queue = asyncio.Queue(maxsize=1)
    queue.put_nowait({"first": True})

    pub._offer(queue, {"second": True})

    assert pub.dropped == 1
    assert queue.qsize() == 1  # the queue did not grow past its bound


def test_stats_are_reported_for_get_status():
    pub = _publisher(password="")
    stats = pub.stats()

    assert stats["enabled"] is False
    assert stats["connected"] is False
    assert set(stats) == {
        "enabled",
        "connected",
        "published",
        "dropped",
        "publish_errors",
        "connect_failures",
    }


def test_close_is_idempotent():
    pub = _publisher(password="")
    pub.close()
    pub.close()  # must not raise


def test_unreachable_server_does_not_block_the_caller(comms_dir):
    """publish() returns immediately even when nothing is listening."""
    pub = _publisher(url="nats://127.0.0.1:1", connect_timeout=1)
    try:
        # Accepted for delivery — delivery itself happens off-thread and will fail.
        assert pub.publish({"id": "x"}) is True
        assert pub.connected is False
    finally:
        pub.close(timeout=2)


def test_get_status_includes_publisher_stats(comms_dir):
    status = ab.get_status()
    assert "publisher" in status["integrations"]["nats"]
    assert "federation" in status


# ── the connection itself, against a fake nats module ─────────────────────────


class _FakeConn:
    def __init__(self):
        self.published = []
        self.closed = False
        self.is_connected = True

    async def publish(self, subject, payload):
        self.published.append((subject, payload))

    async def close(self):
        self.closed = True


class _FakeNats:
    """Stands in for the nats-py module inside the publisher thread."""

    def __init__(self, fail=False):
        self.fail = fail
        self.connect_kwargs = []
        self.conn = _FakeConn()

    async def connect(self, **kwargs):
        self.connect_kwargs.append(kwargs)
        if self.fail:
            raise OSError("connection refused")
        return self.conn


def _install_fake_nats(monkeypatch, fake):
    import sys
    import types

    module = types.ModuleType("nats")
    module.connect = fake.connect
    monkeypatch.setitem(sys.modules, "nats", module)


def _wait_for(predicate, timeout=5.0):
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        _time.sleep(0.01)
    return False


def test_credentials_are_passed_as_kwargs_never_as_argv(monkeypatch):
    """Finding A, stated as an assertion: the password reaches nats-py, not a cmdline."""
    fake = _FakeNats()
    _install_fake_nats(monkeypatch, fake)
    pub = _publisher(password="s3cret")
    try:
        assert pub.publish({"id": "x"}) is True
        assert _wait_for(lambda: fake.connect_kwargs), "never connected"

        kwargs = fake.connect_kwargs[0]
        assert kwargs["user"] == "agent-bus"
        assert kwargs["password"] == "s3cret"
        assert kwargs["servers"] == ["nats://127.0.0.1:14222"]
    finally:
        pub.close(timeout=2)


def test_event_reaches_the_connection(monkeypatch):
    fake = _FakeNats()
    _install_fake_nats(monkeypatch, fake)
    pub = _publisher()
    try:
        pub.publish({"id": "abc", "event": "task.completed"})
        assert _wait_for(lambda: fake.conn.published), "event never published"

        subject, payload = fake.conn.published[0]
        assert subject == "events.agent-bus.test"
        assert json.loads(payload.decode())["id"] == "abc"
        assert pub.published == 1
        assert pub.connected is True
    finally:
        pub.close(timeout=2)


def test_one_connection_is_reused_across_many_events(monkeypatch):
    """The old path spawned a process per event; this must spawn nothing per event."""
    fake = _FakeNats()
    _install_fake_nats(monkeypatch, fake)
    pub = _publisher()
    try:
        for i in range(25):
            pub.publish({"id": str(i)})
        assert _wait_for(lambda: len(fake.conn.published) == 25)
        assert len(fake.connect_kwargs) == 1
    finally:
        pub.close(timeout=2)


def test_connect_failure_is_recorded_and_does_not_raise(monkeypatch):
    fake = _FakeNats(fail=True)
    _install_fake_nats(monkeypatch, fake)
    pub = _publisher()
    try:
        assert pub.publish({"id": "x"}) is True  # accepted, delivery fails later
        assert _wait_for(lambda: pub.connect_failures >= 1)
        assert pub.connected is False
    finally:
        pub.close(timeout=2)


def test_close_shuts_the_connection_down(monkeypatch):
    fake = _FakeNats()
    _install_fake_nats(monkeypatch, fake)
    pub = _publisher()
    pub.publish({"id": "x"})
    assert _wait_for(lambda: fake.conn.published)

    pub.close(timeout=3)

    assert _wait_for(lambda: fake.conn.closed)
