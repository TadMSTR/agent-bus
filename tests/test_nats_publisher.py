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
    assert stats["stream"] is None
    assert stats["deliverable"] is False
    assert set(stats) == {
        "enabled",
        "connected",
        "subject",
        "stream",
        "stream_error",
        "deliverable",
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


class _FakeJetStream:
    """The JetStream context, with the two calls the publisher makes.

    ``stream`` is the name returned for a matching subject; None means the server has
    no stream capturing it, which is the state forge was actually in for three months
    (vikunja#561) and the one the publisher must now refuse to paper over.
    """

    def __init__(self, conn, stream="AGENT_BUS"):
        self._conn = conn
        self.stream = stream
        self.lookups = 0

    async def find_stream_name_by_subject(self, subject):
        self.lookups += 1
        if self.stream is None:
            raise LookupError(f"no stream matches {subject}")
        return self.stream

    async def publish(self, subject, payload, timeout=None):
        # Mirrors the real client: no stream, no PubAck, an exception — never a
        # silent success. That asymmetry with core publish is the whole change.
        if self.stream is None:
            raise TimeoutError("no response from stream")
        self._conn.published.append((subject, payload))
        return object()  # stands in for the PubAck


class _FakeConn:
    def __init__(self, stream="AGENT_BUS"):
        self.published = []
        self.closed = False
        self.is_connected = True
        self.js = _FakeJetStream(self, stream)

    def jetstream(self):
        return self.js

    async def publish(self, subject, payload):  # pragma: no cover — must not be called
        raise AssertionError(
            "core publish is what discarded three months of events — use JetStream"
        )

    async def close(self):
        self.closed = True


class _FakeNats:
    """Stands in for the nats-py module inside the publisher thread."""

    def __init__(self, fail=False, stream="AGENT_BUS"):
        self.fail = fail
        self.connect_kwargs = []
        self.conn = _FakeConn(stream)

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
        assert pub.stream_name == "AGENT_BUS"
        assert pub.deliverable is True
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


# ── the missing stream must be detectable (vikunja#561, plan 5.0 d) ───────────
#
# The premise, measured on forge 2026-08-28: `GET :8222/jsz` reported `"streams": 0`
# while this publisher's counters showed months of clean publishes. Core `nc.publish()`
# to a subject no stream matches succeeds and the message is discarded, so `published`
# incremented for every event that no longer existed. These are the assertions that
# make that state impossible to report as healthy.


def test_no_matching_stream_is_a_publish_failure_not_a_silent_success(monkeypatch):
    """The regression test for three months of federating into a void."""
    fake = _FakeNats(stream=None)
    _install_fake_nats(monkeypatch, fake)
    pub = _publisher()
    try:
        assert pub.publish({"id": "x"}) is True  # accepted onto the queue
        assert _wait_for(lambda: pub.publish_errors >= 1), "publish never failed"

        # The counter that used to lie. It must not move for a message no stream took.
        assert pub.published == 0
        assert pub.stream_name is None
        assert pub.deliverable is False
        assert fake.conn.published == []
    finally:
        pub.close(timeout=2)


def test_stats_expose_the_contradiction_a_bare_counter_hid(monkeypatch):
    """`published` alone cannot distinguish delivered from discarded; `stream` can."""
    fake = _FakeNats(stream=None)
    _install_fake_nats(monkeypatch, fake)
    pub = _publisher()
    try:
        pub.publish({"id": "x"})
        assert _wait_for(lambda: pub.publish_errors >= 1)

        stats = pub.stats()
        assert stats["connected"] is True  # the server is up and answering...
        assert stats["stream"] is None  # ...and nothing is retaining our subject
        assert stats["deliverable"] is False
        assert stats["subject"] == "events.agent-bus.test"
        assert "no stream matches" in stats["stream_error"]
    finally:
        pub.close(timeout=2)


def test_the_lookup_is_by_subject_not_by_stream_name(monkeypatch):
    """vikunja#562: the README named a subject the code never publishes to.

    A check that confirmed "a stream called AGENT_BUS exists" would have passed against
    a stream subscribed to `agent-bus.>` while this publisher sends to
    `events.agent-bus.<host>` — recreating the exact bug it was added to catch. So the
    resolution must go through the SUBJECT.
    """
    fake = _FakeNats()
    _install_fake_nats(monkeypatch, fake)
    pub = _publisher(subject="events.agent-bus.forge")
    try:
        # publish() is what starts the thread, so it comes before any wait on state
        # the thread produces.
        pub.publish({"id": "x"})
        assert _wait_for(lambda: fake.conn.published), "event never published"
        assert fake.conn.js.lookups >= 1, "the stream was never resolved by subject"
        assert fake.conn.published[0][0] == "events.agent-bus.forge"
    finally:
        pub.close(timeout=2)


def test_a_stream_created_while_running_is_picked_up_without_a_reconnect(monkeypatch):
    """sysadmin creating the stream must not require an agent-bus restart.

    Phase 5.0 had sysadmin create AGENT_BUS on a server this publisher was already
    connected to. If the subject lookup only ran on connect, every event until the next
    reconnect would still fail — and the operator would conclude the stream was wrong.
    """
    fake = _FakeNats(stream=None)
    _install_fake_nats(monkeypatch, fake)
    pub = _publisher()
    try:
        pub.publish({"id": "before"})
        assert _wait_for(lambda: pub.publish_errors >= 1)
        assert pub.deliverable is False

        fake.conn.js.stream = "AGENT_BUS"  # sysadmin creates it, out of band

        assert _wait_for(lambda: pub.deliverable, timeout=10), "stream never re-resolved"
        pub.publish({"id": "after"})
        assert _wait_for(lambda: fake.conn.published), "publishing never recovered"
        assert json.loads(fake.conn.published[0][1].decode())["id"] == "after"
        assert len(fake.connect_kwargs) == 1  # ...and it never reconnected
    finally:
        pub.close(timeout=2)
