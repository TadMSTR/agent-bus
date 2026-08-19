"""The federation loop's gating: leader election, NATS availability, and the caps.

federation_loop is started from the FastMCP lifespan, so it runs in every process that
starts the server. These cover the conditions under which a cycle must *not* run.
"""

import asyncio
import contextlib
import fcntl
import json

import server as ab


def _run_briefly(coro_fn, seconds=0.25):
    """Run an endless loop for a moment, then cancel it."""

    async def runner():
        task = asyncio.create_task(coro_fn())
        await asyncio.sleep(seconds)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(runner())


def _seed(comms_dir, count=3):
    path = comms_dir / "logs" / "2026-08-01-cross-agent.jsonl"
    path.write_text("".join(json.dumps({"id": str(i), "ts": "t"}) + "\n" for i in range(count)))
    return path


def _fast(monkeypatch):
    monkeypatch.setattr(ab, "FEDERATION_STARTUP_DELAY", 0)
    monkeypatch.setattr(ab, "FEDERATION_INTERVAL", 0.01)


def test_loop_returns_immediately_when_disabled(comms_dir, publisher, monkeypatch):
    monkeypatch.setattr(ab, "FEDERATION_ENABLED", False)
    _seed(comms_dir)

    asyncio.run(asyncio.wait_for(ab.federation_loop(), timeout=2))

    assert publisher.published == []


def test_loop_federates_when_nats_is_up(comms_dir, publisher, monkeypatch):
    _fast(monkeypatch)
    _seed(comms_dir, 3)

    _run_briefly(ab.federation_loop)

    assert len(publisher.published) == 3


def test_loop_does_not_federate_while_nats_is_down(comms_dir, publisher, monkeypatch):
    """Publishing now would be dropped — the cursor must not move past those events."""
    _fast(monkeypatch)
    _seed(comms_dir, 3)
    publisher._connected = False

    _run_briefly(ab.federation_loop)

    assert publisher.published == []
    assert not ab.CURSOR_FILE.exists()


def test_loop_resumes_after_nats_comes_back(comms_dir, publisher, monkeypatch):
    _fast(monkeypatch)
    _seed(comms_dir, 3)
    publisher._connected = False
    _run_briefly(ab.federation_loop)
    assert publisher.published == []

    publisher._connected = True
    _run_briefly(ab.federation_loop)

    assert len(publisher.published) == 3


def test_loop_does_nothing_without_a_credential(comms_dir, publisher, monkeypatch):
    _fast(monkeypatch)
    _seed(comms_dir, 3)
    publisher._enabled = False

    _run_briefly(ab.federation_loop)

    assert publisher.published == []


def test_loop_defers_to_the_process_holding_the_lock(comms_dir, publisher, monkeypatch):
    """8+ processes start this loop; exactly one may federate."""
    _fast(monkeypatch)
    _seed(comms_dir, 3)

    holder = open(comms_dir / ".federation.lock", "w")  # noqa: SIM115 — held across the loop run
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        _run_briefly(ab.federation_loop)
        assert publisher.published == []
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_loop_survives_an_error_and_keeps_going(comms_dir, publisher, monkeypatch):
    _fast(monkeypatch)
    _seed(comms_dir, 2)
    calls = []
    real = ab.federate_once

    def flaky(cursor):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk hiccup")
        return real(cursor)

    monkeypatch.setattr(ab, "federate_once", flaky)

    _run_briefly(ab.federation_loop)

    assert len(calls) > 1  # did not die on the first error
    assert len(publisher.published) == 2
