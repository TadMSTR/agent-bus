"""
nats_publisher.py — persistent, non-blocking NATS publisher.

Replaces the previous ``nats pub --password <plaintext>`` subprocess shell-out, which
was wrong in two independent ways:

* the credential sat in ``ps`` / ``/proc/*/cmdline``, readable by any local user, and
* it spawned one process per event — on the hot path of every ``log_event()`` call,
  and once per replayed event in the federation loop.

Credentials are handed to nats-py as ``connect()`` keyword arguments, so they never
appear in any process's argv.

Publishing is fire-and-forget by construction. ``publish()`` hands the event to a
bounded queue owned by a background thread and returns immediately: it never blocks
and never raises. A NATS outage therefore cannot affect the JSONL append, which is
the authoritative record. The queue is bounded so a sustained outage costs a fixed
amount of memory and drops the overflow rather than growing without limit.
"""

import asyncio
import contextlib
import json
import logging
import threading
import time

_log = logging.getLogger("agent-bus.nats")

DEFAULT_QUEUE_SIZE = 10_000
DEFAULT_CONNECT_TIMEOUT = 2
DEFAULT_RETRY_DELAY = 2.0
DEFAULT_MAX_RETRY_DELAY = 60.0
DEFAULT_IDLE_INTERVAL = 5.0

_SHUTDOWN = object()


class NatsPublisher:
    """Owns one background thread, one asyncio loop, and one persistent connection.

    Thread-safe. ``publish()`` may be called from any thread — including the sync
    worker threads FastMCP runs tool functions on.
    """

    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        subject: str,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        max_retry_delay: float = DEFAULT_MAX_RETRY_DELAY,
        idle_interval: float = DEFAULT_IDLE_INTERVAL,
    ) -> None:
        self._url = url
        self._user = user
        self._password = password
        self._subject = subject
        self._queue_size = queue_size
        self._connect_timeout = connect_timeout
        self._base_retry_delay = retry_delay
        self._max_retry_delay = max_retry_delay
        self._idle_interval = idle_interval

        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = False
        self._connected = False

        self._retry_delay = retry_delay
        self._retry_not_before = 0.0

        # Observability — read by get_status().
        self.published = 0
        self.dropped = 0
        self.publish_errors = 0
        self.connect_failures = 0

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """False when no credential is configured — the server requires auth."""
        return bool(self._password)

    @property
    def connected(self) -> bool:
        """Whether the background connection is currently established.

        The federation loop uses this to avoid advancing its cursor across events it
        could not actually deliver, so the gap is replayed once NATS returns.
        """
        return self._connected

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "connected": self._connected,
            "published": self.published,
            "dropped": self.dropped,
            "publish_errors": self.publish_errors,
            "connect_failures": self.connect_failures,
        }

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Start the background thread. Idempotent, and does not wait on the network."""
        if self._closed:
            return False
        with self._start_lock:
            if self._closed:
                return False
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._thread_main, name="nats-publisher", daemon=True
                )
                self._thread.start()
        # _ready is set once the loop and queue exist — connecting happens after.
        return self._ready.wait(timeout=5)

    def close(self, timeout: float = 5.0) -> None:
        """Drain and shut down. Safe to call more than once."""
        with self._start_lock:
            if self._closed:
                return
            self._closed = True
            thread, loop, queue_ = self._thread, self._loop, self._queue

        if thread is None or loop is None or queue_ is None:
            return
        with contextlib.suppress(RuntimeError, asyncio.QueueFull):
            loop.call_soon_threadsafe(queue_.put_nowait, _SHUTDOWN)
        thread.join(timeout=timeout)

    # ── publish ───────────────────────────────────────────────────────────────

    def publish(self, event: dict) -> bool:
        """Queue an event for publication.

        Returns True if the event was accepted for delivery — not that it was
        delivered. Never blocks, never raises.
        """
        if not self.enabled or self._closed:
            return False
        if not self.start():
            return False

        loop, queue_ = self._loop, self._queue
        if loop is None or queue_ is None or loop.is_closed():
            return False
        try:
            loop.call_soon_threadsafe(self._offer, queue_, event)
            return True
        except RuntimeError:
            # Loop shut down between the check above and the call.
            return False

    def _offer(self, queue_: asyncio.Queue, item: dict) -> None:
        """Runs on the publisher loop. Drops rather than blocking when saturated."""
        try:
            queue_.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                _log.warning("nats publish queue full — dropped %d event(s)", self.dropped)

    # ── background thread ─────────────────────────────────────────────────────

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._queue = asyncio.Queue(maxsize=self._queue_size)
        self._ready.set()
        try:
            loop.run_until_complete(self._pump())
        except Exception as exc:  # pragma: no cover — defensive
            _log.warning("nats publisher thread exiting: %s", exc)
        finally:
            self._connected = False
            with contextlib.suppress(Exception):  # pragma: no cover — defensive
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _pump(self) -> None:
        nc = None
        try:
            while True:
                nc = await self._ensure_connection(nc)
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=self._idle_interval)
                except asyncio.TimeoutError:
                    # Idle tick — loop back so connection state stays fresh.
                    continue
                if item is _SHUTDOWN:
                    break
                if nc is None:
                    self.publish_errors += 1
                    continue
                try:
                    await nc.publish(self._subject, json.dumps(item).encode())
                    self.published += 1
                except Exception as exc:
                    self.publish_errors += 1
                    _log.warning("nats publish failed: %s", exc)
                    nc = await self._discard(nc)
        finally:
            await self._discard(nc)

    async def _ensure_connection(self, nc):
        if nc is not None and getattr(nc, "is_connected", False):
            self._connected = True
            return nc
        if nc is not None:
            nc = await self._discard(nc)

        now = time.monotonic()
        if now < self._retry_not_before:
            return None

        try:
            import nats

            nc = await nats.connect(
                servers=[self._url],
                user=self._user,
                password=self._password,
                connect_timeout=self._connect_timeout,
                allow_reconnect=True,
                max_reconnect_attempts=-1,
            )
            self._connected = True
            self._retry_delay = self._base_retry_delay
            self._retry_not_before = 0.0
            return nc
        except Exception as exc:
            self.connect_failures += 1
            self._connected = False
            self._retry_not_before = now + self._retry_delay
            _log.warning("nats connect failed (%s) — retrying in %.0fs", exc, self._retry_delay)
            self._retry_delay = min(self._retry_delay * 2, self._max_retry_delay)
            return None

    async def _discard(self, nc):
        self._connected = False
        if nc is None:
            return None
        with contextlib.suppress(Exception):  # pragma: no cover — defensive
            await nc.close()
        return None
