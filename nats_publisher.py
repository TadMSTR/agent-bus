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

WHY THIS PUBLISHES VIA JETSTREAM (vikunja#561, plan Phase 5.0 d)
----------------------------------------------------------------
This used to call **core** ``nc.publish()``. A core publish to a subject that no
stream and no subscriber matches SUCCEEDS and the message is discarded — so the very
next line incremented ``self.published``, and every counter ``get_status`` reported
recorded a success for a message that no longer existed.

That was not hypothetical. On 2026-08-28 the NATS server had **zero streams**
(``GET :8222/jsz`` → ``"streams": 0``) while this publisher's counters showed a clean
run stretching back months. agent-bus had been federating into a void, and nothing
could tell — the same shape as vikunja#444 (webhook emitter configured but silently
dead) and #479 (Loki collecting into one unlabelled stream). Configured is not
delivered.

``js.publish()`` waits for a **PubAck** from the stream that accepted the message, and
raises when none does. So ``published`` now means "accepted by a stream" rather than
"handed to a socket", which is the only version of that counter worth having. On top
of that, ``stream_name`` is resolved once per connection from the server's own subject
mapping, so ``get_status`` can answer "is anything actually matching my subject?"
without waiting for an event to be published.

A COUNTER THAT CANNOT DISTINGUISH THE TWO IS WORSE THAN NO COUNTER, so there is
deliberately no fallback to core publish when the stream is missing. Falling back
would restore the silent discard and the false success with it. If no stream matches,
publishes fail loudly, ``publish_errors`` climbs, and ``stream_name`` reads ``None``.
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
# How long to wait for a stream's PubAck. Generous relative to a loopback NATS, because
# the cost of it being too short is a false publish_error on a message that did land —
# the exact ambiguity this whole change exists to remove.
DEFAULT_PUBLISH_TIMEOUT = 5.0
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
        publish_timeout: float = DEFAULT_PUBLISH_TIMEOUT,
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
        self._publish_timeout = publish_timeout
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
        self._js = None
        # The stream currently accepting self._subject, or None. Resolved from the
        # server on each connect — not configured here, because a name we were TOLD is
        # correct is exactly the assumption that let the missing stream hide.
        self._stream_name: str | None = None
        self._stream_error: str | None = None

        self._retry_delay = retry_delay
        self._retry_not_before = 0.0

        # Observability — read by get_status().
        # `published` counts PubAcks, i.e. messages a stream ACCEPTED — not messages
        # handed to a socket. See the module docstring for why that distinction is the
        # whole point of this class.
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
        """Whether the background connection is currently established."""
        return self._connected

    @property
    def stream_name(self) -> str | None:
        """The stream accepting this publisher's subject, or None if none does."""
        return self._stream_name

    @property
    def deliverable(self) -> bool:
        """Whether a publish right now could actually be retained by a stream.

        THE FEDERATION LOOP GATES ON THIS, NOT ON ``connected``. Connected-but-no-stream
        is precisely the state the server was in for three months: every publish
        "succeeded" and every message was discarded. Advancing the federation cursor
        across that would mark those events delivered and never replay them — turning a
        recoverable outage into permanent loss. Gating here means the cursor stands
        still until a stream exists, and the backlog federates when one does.
        """
        return self._connected and self._stream_name is not None

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "connected": self._connected,
            "subject": self._subject,
            # None here means nothing on the server matches `subject`, so nothing
            # published is being retained. A `published` count above zero alongside a
            # null stream is the exact contradiction this field exists to expose.
            "stream": self._stream_name,
            "stream_error": self._stream_error,
            "deliverable": self.deliverable,
            "published": self.published,  # PubAcks — accepted BY A STREAM
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
                if nc is None or self._js is None:
                    self.publish_errors += 1
                    continue
                try:
                    # JetStream, not core. This AWAITS a PubAck from the stream that
                    # accepted the message; with no matching stream it raises rather
                    # than succeeding into a void. See the module docstring.
                    await self._js.publish(
                        self._subject,
                        json.dumps(item).encode(),
                        timeout=self._publish_timeout,
                    )
                    self.published += 1
                except Exception as exc:
                    self.publish_errors += 1
                    _log.warning(
                        "nats publish failed (subject=%s stream=%s): %s",
                        self._subject,
                        self._stream_name,
                        exc,
                    )
                    # A publish failure is not necessarily a broken connection — "no
                    # stream matches this subject" is a live server answering
                    # correctly. Re-resolve rather than tearing the connection down, so
                    # a stream created while we are running is picked up on the next
                    # event instead of on the next reconnect.
                    self._stream_name = None
                    self._stream_error = f"{type(exc).__name__}: {exc}"
                    if not getattr(nc, "is_connected", False):
                        nc = await self._discard(nc)
        finally:
            await self._discard(nc)

    async def _resolve_stream(self) -> None:
        """Ask the server which stream, if any, is capturing our subject.

        Resolved from the server rather than configured, because a configured name is
        an assumption and this class exists because an assumption went unchecked for
        three months. It is also why the lookup is by SUBJECT: the README described the
        stream as subscribing to ``agent-bus.>`` while the code publishes to
        ``events.agent-bus.<host>``, so a check that confirmed "a stream named
        AGENT_BUS exists" would have reported healthy against a stream matching nothing
        this publisher sends (vikunja#562).
        """
        if self._js is None:
            self._stream_name = None
            self._stream_error = "no jetstream context"
            return
        try:
            self._stream_name = await self._js.find_stream_name_by_subject(self._subject)
            self._stream_error = None
            _log.info("nats subject %s is captured by stream %s", self._subject, self._stream_name)
        except Exception as exc:
            self._stream_name = None
            self._stream_error = f"{type(exc).__name__}: {exc}"
            _log.warning(
                "NO JETSTREAM STREAM MATCHES %s — published events would be discarded. "
                "Publishes will fail loudly rather than silently succeed. (%s)",
                self._subject,
                self._stream_error,
            )

    async def _ensure_connection(self, nc):
        if nc is not None and getattr(nc, "is_connected", False):
            self._connected = True
            # Cheap retry of a previously failed lookup: if the stream was created
            # after we connected, this picks it up without waiting for a reconnect.
            if self._stream_name is None:
                await self._resolve_stream()
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
            self._js = nc.jetstream()
            await self._resolve_stream()
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
        self._js = None
        self._stream_name = None
        if nc is None:
            return None
        with contextlib.suppress(Exception):  # pragma: no cover — defensive
            await nc.close()
        return None
