import asyncio
import base64
import contextlib
import fcntl
import json
import logging
import os
import re
import subprocess
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from fastmcp import FastMCP

import event_log
from event_log import append_event as _append_event
from event_log import sha256 as _sha256
from event_vocab import CROSS_AGENT_EVENTS, HIGH_PRIORITY_EVENTS, resolve_scope
from nats_publisher import NatsPublisher

# Re-exported: the vocabulary is documented as part of this module's surface, and
# external callers import it from here. event_vocab is the single definition.
__all__ = ["CROSS_AGENT_EVENTS", "HIGH_PRIORITY_EVENTS", "resolve_scope"]

_log = logging.getLogger("agent-bus")


def _env_num(name: str, default, cast):
    """Read a numeric env var, tolerating unset, empty, and malformed values.

    ecosystem.config.js passes every configured variable through, so an unset one
    arrives as an empty string rather than being absent — `int("")` would raise at
    import time and put the service into a restart loop.
    """
    raw = os.environ.get(name) or ""
    try:
        return cast(raw)
    except (TypeError, ValueError):
        if raw:
            _log.warning("ignoring malformed %s=%r — using %r", name, raw, default)
        return default


COMMS_DIR = Path(os.environ.get("AGENT_BUS_COMMS_DIR") or str(Path.home() / ".claude" / "comms"))
LOGS_DIR = COMMS_DIR / "logs"

# Ensure log directory exists on first run
LOGS_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(server):
    task = asyncio.create_task(federation_loop())
    try:
        yield
    finally:
        task.cancel()
        _publisher.close()


mcp = FastMCP("agent-bus", lifespan=lifespan)

CURSOR_FILE = COMMS_DIR / "federation-cursor.json"
FEDERATION_LOCK_FILE = COMMS_DIR / ".federation.lock"
HOSTNAME = os.uname().nodename
NTFY_URL = os.environ.get("NTFY_URL", "")
NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
NATS_AGENT_BUS_PASSWORD = os.environ.get("NATS_AGENT_BUS_PASSWORD", "")
NATS_USER = os.environ.get("NATS_AGENT_BUS_USER", "agent-bus")
WEBHOOK_URL = os.environ.get("AGENT_BUS_WEBHOOK_URL", "")
WEBHOOK_EVENTS = {
    e.strip() for e in os.environ.get("AGENT_BUS_WEBHOOK_EVENTS", "").split(",") if e.strip()
}

# Federation tuning. The loop used to be unbounded, which is how a single cycle grew to
# 18+ minutes; these caps mean a future cursor reset degrades to "slower catch-up"
# rather than a blocking replay storm.
FEDERATION_ENABLED = (os.environ.get("AGENT_BUS_FEDERATION") or "1") != "0"
FEDERATION_INTERVAL = _env_num("AGENT_BUS_FEDERATION_INTERVAL", 30, int)
FEDERATION_MAX_EVENTS_PER_CYCLE = _env_num("AGENT_BUS_FEDERATION_MAX_EVENTS", 500, int)
FEDERATION_MAX_SECONDS_PER_CYCLE = _env_num("AGENT_BUS_FEDERATION_MAX_SECONDS", 10.0, float)
FEDERATION_STARTUP_DELAY = 10

CURSOR_VERSION = 2

_publisher = NatsPublisher(
    url=NATS_URL,
    user=NATS_USER,
    password=NATS_AGENT_BUS_PASSWORD,
    subject=f"events.agent-bus.{HOSTNAME}",
)

# ── Hash chaining ─────────────────────────────────────────────────────────────
# The append path (flock, chaining, tail read) lives in event_log so that
# agent_bus_client.py and reconcile.py write through exactly the same code.


def _last_line(path: Path) -> str | None:
    """Return the last non-empty line of a JSONL file, or None."""
    return event_log.last_line(path)


def _strip_credentials(url: str | None) -> str | None:
    """Return URL with userinfo, query and fragment removed.

    ``get_status`` hands this to any calling agent. The previous version dropped the
    query string but kept ``netloc`` intact, so ``nats://user:pass@host`` was returned
    verbatim — the CHANGELOG advertised a leak that was still open.
    """
    if not url:
        return None
    p = urlparse(url)
    host = p.hostname or ""
    if ":" in host:  # IPv6 literal — urlparse strips the brackets
        host = f"[{host}]"
    netloc = f"{host}:{p.port}" if p.port else host
    return urlunparse(p._replace(netloc=netloc, query="", fragment=""))


# Retained under the old name: it is part of the module's tested surface.
_strip_qs = _strip_credentials


# ── Key registry ─────────────────────────────────────────────────────────────

KEY_REGISTRY_PATH = COMMS_DIR / "agent-keys.json"
VERIFY_SIGNATURES: str = os.environ.get("AGENT_BUS_VERIFY_SIGNATURES", "warn")

# Replay window for signature version 2+. See _check_replay.
SIG_MAX_AGE_SECONDS = _env_num("AGENT_BUS_SIG_MAX_AGE", 300, int)

# Seen nonces, keyed by nonce with the monotonic time they were first accepted. Entries
# are evicted when they age out of the freshness window (see _prune_nonces), so eviction
# is driven by the clock rather than by insertion volume. The hard cap below is only a
# memory backstop; at forge's observed rate (~40 cross-agent events/day) a 300s window
# holds single digits, so it should never be reached in normal operation.
_SEEN_NONCE_LIMIT = 16384
_seen_nonces: OrderedDict[str, float] = OrderedDict()


def _load_key_registry() -> dict[str, Any]:
    """Load public key registry from COMMS_DIR/agent-keys.json. Returns {} if absent."""
    if not KEY_REGISTRY_PATH.exists():
        return {}
    try:
        return json.loads(KEY_REGISTRY_PATH.read_text())
    except Exception:
        return {}


def _canonical_payload(event: dict) -> str:
    """Rebuild the canonical signed payload — must match scoped-mcp's signing_hook.

    Only fields the *signer* controls can appear here. ``ts``, ``id``, ``hostname`` and
    ``prev_hash`` are assigned by this server after the client has signed, so they are
    unsignable by construction. Replay protection is therefore carried by signed
    ``metadata`` fields instead — see _check_replay.
    """
    metadata = {
        k: v for k, v in (event.get("metadata") or {}).items() if k not in ("sig", "prev_hash")
    }
    payload = {
        "event_type": event.get("event", ""),
        "source": event.get("source", ""),
        "summary": event.get("summary", ""),
        "scope": event.get("scope", "cross-agent"),
        "target": event.get("target"),
        "artifact_path": event.get("artifact_path"),
        "metadata": metadata,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _signature_valid(event: dict, pubkey_b64: str) -> bool:
    """Cryptographic check only — no policy."""
    sig_b64 = (event.get("metadata") or {}).get("sig", "")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64))
        public_key.verify(base64.b64decode(sig_b64), _canonical_payload(event).encode())
        return True
    except Exception:
        return False


def _check_replay(event: dict) -> str | None:
    """Reject a replayed signed event. Returns a rejection reason, or None if fine.

    A signature alone cannot prevent replay: a captured event re-sent verbatim carries
    a valid signature. Prevention needs something signed, per-event and non-reusable.
    Since the server assigns ``ts``/``id`` *after* signing, that has to come from the
    signer, inside ``metadata`` — which the canonical payload already covers.

    A signer declaring ``sig_v >= 2`` must therefore also send ``sig_ts`` and
    ``sig_nonce``; both are enforced here. Version-1 signers predate this and cannot be
    replay-checked — that is reported by get_status rather than silently tolerated.
    """
    metadata = event.get("metadata") or {}
    try:
        sig_version = int(metadata.get("sig_v", 1))
    except (TypeError, ValueError):
        return "sig_version_invalid"
    if sig_version < 2:
        return None

    sig_ts = metadata.get("sig_ts")
    nonce = metadata.get("sig_nonce")
    if not sig_ts or not nonce:
        return "replay_fields_missing"

    try:
        signed_at = datetime.fromisoformat(str(sig_ts))
    except ValueError:
        return "sig_ts_invalid"
    if signed_at.tzinfo is None:
        signed_at = signed_at.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    if abs(now - signed_at) > timedelta(seconds=SIG_MAX_AGE_SECONDS):
        return "sig_expired"

    now_mono = time.monotonic()
    _prune_nonces(now_mono)
    if nonce in _seen_nonces:
        return "nonce_reused"
    _seen_nonces[nonce] = now_mono
    if len(_seen_nonces) > _SEEN_NONCE_LIMIT:
        # Memory backstop only — reaching this means unexpected volume, and dropping the
        # oldest entry is the one case where eviction is still attacker-influenceable.
        # Log it rather than shedding replay protection silently.
        _seen_nonces.popitem(last=False)
        _log.warning(
            "nonce cache hit its %d-entry cap — replay protection degraded for evicted "
            "nonces still inside the %ds window",
            _SEEN_NONCE_LIMIT,
            SIG_MAX_AGE_SECONDS,
        )
    return None


def _prune_nonces(now_mono: float) -> None:
    """Drop nonces that have aged out of the freshness window.

    Eviction used to be pure insertion-order FIFO against a fixed bound, so an attacker
    able to submit signed events could flood distinct nonces to evict one specific
    legitimate nonce and then replay that event inside the still-open window
    (audit 2026-08-19, LOW). Expiring by age instead puts eviction under the clock, which
    the attacker does not control: a nonce can only leave the cache once replaying it
    would already fail the sig_ts freshness check.

    Insertion order is monotonic-time order, so the expired entries are always a prefix.
    """
    cutoff = now_mono - SIG_MAX_AGE_SECONDS
    while _seen_nonces:
        _, seen_at = next(iter(_seen_nonces.items()))
        if seen_at > cutoff:
            break
        _seen_nonces.popitem(last=False)


def _check_signature(event: dict) -> tuple[bool, str | None]:
    """Apply signature policy. Returns (accepted, reason_if_rejected).

    In ``enforce`` this now rejects the cases that actually matter. Previously it
    returned True for an unregistered source, an absent signature, and a registry entry
    with no pubkey — so only a *badly* signed event was ever refused, which is not a
    case an attacker produces. ``warn`` stays permissive by design.
    """
    strict = VERIFY_SIGNATURES == "enforce"
    registry = _load_key_registry()
    source = event.get("source", "")
    entry = registry.get(source)

    if not entry:
        return (not strict, "source_not_registered")

    pubkey = entry.get("pubkey", "")
    if not pubkey:
        return (not strict, "registry_entry_has_no_pubkey")

    if not (event.get("metadata") or {}).get("sig"):
        return (not strict, "event_unsigned")

    if not _signature_valid(event, pubkey):
        return (False, "signature_invalid")

    replay_reason = _check_replay(event)
    if replay_reason:
        return (not strict, replay_reason)

    return (True, None)


def _verify_signature(event: dict) -> bool:
    """Back-compatible boolean check: is the signature cryptographically valid?"""
    registry = _load_key_registry()
    entry = registry.get(event.get("source", ""))
    if not entry:
        return True
    if not (event.get("metadata") or {}).get("sig"):
        return True
    pubkey = entry.get("pubkey", "")
    if not pubkey:
        return True
    return _signature_valid(event, pubkey)


def log_path(scope: str) -> Path:
    return event_log.log_path(scope, LOGS_DIR)


def append_event(event: dict, scope: str) -> None:
    """Chained, flock-guarded JSONL append with fsync."""
    _append_event(event, scope, LOGS_DIR)


def emit_ntfy(event: dict) -> None:
    if not NTFY_URL:
        return
    try:
        # Strip \r\n from interpolated fields to prevent header injection
        def _clean(s: str) -> str:
            return s.replace("\r", "").replace("\n", " ")

        subprocess.run(
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-X",
                "POST",
                NTFY_URL,
                "-H",
                f"Title: agent-bus: {_clean(event['event'])}",
                "-H",
                "Priority: default",
                "-H",
                "Tags: agent",
                "-d",
                f"{_clean(event['source'])} → {_clean(event.get('target') or 'n/a')}: "
                f"{_clean(event['summary'])}",
            ],
            timeout=5,
            capture_output=True,
        )
    except Exception:
        pass


def emit_nats(event: dict) -> bool:
    """Hand the event to the persistent publisher. Never blocks, never raises.

    Was a ``nats pub --password <plaintext>`` subprocess per event: the credential was
    readable in ``ps`` by any local user, and it spawned a process on the hot path of
    every log_event call.
    """
    try:
        return _publisher.publish(event)
    except Exception:  # pragma: no cover — publish() is already non-raising
        return False


def emit_webhook(event: dict) -> None:
    if not WEBHOOK_URL:
        return
    # "*" in WEBHOOK_EVENTS matches all event types
    if WEBHOOK_EVENTS and event["event"] not in WEBHOOK_EVENTS and "*" not in WEBHOOK_EVENTS:
        return
    # Webhook failure never blocks event logging.
    with contextlib.suppress(Exception):
        subprocess.run(
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-X",
                "POST",
                WEBHOOK_URL,
                "-H",
                "Content-Type: application/json",
                "-d",
                json.dumps(event),
            ],
            timeout=5,
            capture_output=True,
        )


@mcp.tool()
def log_event(
    event_type: str,
    source: str,
    summary: str,
    scope: str = "cross-agent",
    target: str | None = None,
    artifact_path: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """
    Log an inter-agent communication event.

    scope: "cross-agent" (handoffs, tasks, audits) or "session" (memory, skills)
    event_type: one of the defined event vocabulary (see server docs)
    Returns the assigned event ID.
    """
    event = {
        "id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "scope": scope,
        "source": source,
        "target": target,
        "artifact_path": str(artifact_path) if artifact_path else None,
        "summary": summary,
        "hostname": HOSTNAME,
        "metadata": metadata or {},
    }
    scope_resolved = resolve_scope(event_type, scope)

    if VERIFY_SIGNATURES in ("warn", "enforce"):
        accepted, reason = _check_signature(event)
        if not accepted:
            return {"id": event["id"], "logged": False, "error": reason}
        if reason:
            _log.warning(
                "signature_policy source=%s event_type=%s id=%s reason=%s",
                source,
                event_type,
                event["id"],
                reason,
            )

    append_event(event, scope_resolved)

    if event_type in HIGH_PRIORITY_EVENTS:
        emit_ntfy(event)

    emit_nats(event)
    emit_webhook(event)
    return {"id": event["id"], "logged": True, "scope": scope_resolved}


@mcp.tool()
def query_events(
    since: str | None = None,
    source: str | None = None,
    target: str | None = None,
    event_type: str | None = None,
    scope: str = "cross-agent",
    limit: int = 50,
) -> list[dict]:
    """
    Query logged events. since is an ISO timestamp string.
    Returns most-recent-first, capped at limit (max 500).
    """
    limit = min(limit, 500)
    suffix = "cross-agent" if scope == "cross-agent" else "session"
    events: list[dict] = []

    for path in sorted(LOGS_DIR.glob(f"*-{suffix}.jsonl"), reverse=True):
        if len(events) >= limit:
            break
        try:
            for line in reversed(path.read_text().splitlines()):
                if not line.strip():
                    continue
                e = json.loads(line)
                if since and e["ts"] < since:
                    continue
                if source and e.get("source") != source:
                    continue
                if target and e.get("target") != target:
                    continue
                if event_type and e.get("event") != event_type:
                    continue
                events.append(e)
                if len(events) >= limit:
                    break
        except Exception:
            continue

    return events


@mcp.tool()
def get_event(event_id: str) -> dict | None:
    """Retrieve a specific event by UUID."""
    for path in sorted(LOGS_DIR.glob("*.jsonl"), reverse=True):
        try:
            for line in path.read_text().splitlines():
                if event_id in line:
                    e = json.loads(line)
                    if e.get("id") == event_id:
                        return e
        except Exception:
            continue
    return None


@mcp.tool()
def verify_chain(
    scope: str = "cross-agent",
    date: str | None = None,
) -> dict:
    """Walk a JSONL log file and verify its hash chain and signatures.

    Args:
        scope: "cross-agent" or "session" (determines file suffix).
        date:  ISO date string (YYYY-MM-DD). Defaults to today.

    Returns a summary dict with counts: verified, chain_breaks, sig_failures,
    unsigned_events, total_events.
    """
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError(f"Invalid date format: {date!r} — expected YYYY-MM-DD")
    suffix = "cross-agent" if scope == "cross-agent" else "session"
    path = LOGS_DIR / f"{date}-{suffix}.jsonl"

    if not path.exists():
        return {
            "file": str(path),
            "error": "file_not_found",
            "total_events": 0,
        }

    registry = _load_key_registry()
    total = 0
    chain_breaks = 0
    sig_failures = 0
    unsigned_events = 0
    flawed = 0
    prev_line: str | None = None

    try:
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    except Exception as exc:
        return {"file": str(path), "error": str(exc), "total_events": 0}

    for line in lines:
        total += 1
        try:
            event = json.loads(line)
        except Exception:
            chain_breaks += 1
            flawed += 1
            prev_line = line
            continue

        # An event can be both unsigned and chain-broken. Count each problem in its own
        # bucket but the event only once, so `verified` cannot be double-subtracted.
        problem = False

        # Verify hash chain
        declared_prev = event.get("prev_hash")
        if prev_line is not None:
            if declared_prev != _sha256(prev_line):
                chain_breaks += 1
                problem = True
        elif declared_prev is not None:
            # First event should have no prev_hash
            chain_breaks += 1
            problem = True

        # Verify signature
        source = event.get("source", "")
        if source in registry:
            sig = (event.get("metadata") or {}).get("sig", "")
            if not sig:
                unsigned_events += 1
                problem = True
            elif not _verify_signature(event):
                sig_failures += 1
                problem = True

        if problem:
            flawed += 1
        prev_line = line

    return {
        "file": str(path),
        "total_events": total,
        "verified": total - flawed,
        "chain_breaks": chain_breaks,
        "sig_failures": sig_failures,
        "unsigned_events": unsigned_events,
    }


@mcp.tool()
def get_status() -> dict:
    """
    Return the current configuration and health of the agent-bus server.
    Useful for verifying setup after installation.
    """
    # Collect log file info
    log_files = sorted(LOGS_DIR.glob("*.jsonl")) if LOGS_DIR.exists() else []
    date_range = None
    if log_files:
        first = log_files[0].stem.split("-cross-agent")[0].split("-session")[0]
        last = log_files[-1].stem.split("-cross-agent")[0].split("-session")[0]
        date_range = {"first": first, "last": last, "files": len(log_files)}

    # Count today's events
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_count = 0
    for path in LOGS_DIR.glob(f"{today}-*.jsonl") if LOGS_DIR.exists() else []:
        with contextlib.suppress(Exception):
            today_count += sum(1 for line in path.read_text().splitlines() if line.strip())

    key_registry = _load_key_registry()

    return {
        "comms_dir": str(COMMS_DIR),
        "logs_dir": str(LOGS_DIR),
        "hostname": HOSTNAME,
        "signing": {
            "registered_agents": list(key_registry.keys()),
            "verify_mode": VERIFY_SIGNATURES,
            "replay_protection": f"signatures declaring sig_v>=2 (window {SIG_MAX_AGE_SECONDS}s)",
        },
        "integrations": {
            "nats": {
                "enabled": bool(NATS_URL),
                "url": _strip_credentials(NATS_URL),
                "publisher": _publisher.stats(),
            },
            "ntfy": {"enabled": bool(NTFY_URL), "url": _strip_credentials(NTFY_URL)},
            "webhook": {
                "enabled": bool(WEBHOOK_URL),
                "url": _strip_credentials(WEBHOOK_URL),
                "events": list(WEBHOOK_EVENTS) if WEBHOOK_EVENTS else ["*"] if WEBHOOK_URL else [],
            },
        },
        "federation": {
            "enabled": FEDERATION_ENABLED,
            "active_in_this_process": _federation_lock_fd is not None,
            "interval_seconds": FEDERATION_INTERVAL,
            "max_events_per_cycle": FEDERATION_MAX_EVENTS_PER_CYCLE,
        },
        "logs": date_range,
        "events_today": today_count,
    }


# ── Federation background task ─────────────────────────────────────────────────
# emit_nats() is called inline on every log_event(). The federation loop replays from
# the per-file cursor — events already published inline get republished, and NATS
# JetStream dedup (2-min window on the AGENT_BUS stream) absorbs the recent ones.
# Inline emit is real-time notification; loop replay is gap-fill after NATS downtime.
# Consumers treat AGENT_BUS as at-least-once.

_federation_lock_fd = None


def acquire_federation_lock(lock_path: Path | None = None) -> bool:
    """Elect a single federating process, via an exclusive non-blocking flock.

    ``federation_loop`` is started from the FastMCP lifespan, so it runs in *every*
    process that starts this server — the PM2 service and one stdio child per
    scoped-mcp broker, 8+ in total. Without this, each replays the whole corpus and
    they race each other's read-modify-write on the cursor file. Whichever process
    holds the lock federates; the rest skip and retry, so it fails over on its own.
    """
    global _federation_lock_fd
    if _federation_lock_fd is not None:
        return True
    path = lock_path or FEDERATION_LOCK_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Deliberately not a context manager: the flock must outlive this call and
        # is held for the process lifetime. Released by release_federation_lock().
        fd = open(path, "w")  # noqa: SIM115
    except OSError:
        return False
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return False
    _federation_lock_fd = fd  # held for process lifetime
    return True


def release_federation_lock() -> None:
    global _federation_lock_fd
    if _federation_lock_fd is None:
        return
    with contextlib.suppress(OSError):
        fcntl.flock(_federation_lock_fd.fileno(), fcntl.LOCK_UN)
        _federation_lock_fd.close()
    _federation_lock_fd = None


def _default_cursor() -> dict:
    return {
        "version": CURSOR_VERSION,
        "hostname": HOSTNAME,
        "files": {},
        "last_federated_ts": None,
        "last_federated_id": None,
    }


def migrate_cursor(cursor: dict, logs_dir: Path) -> dict:
    """Convert a v1 (single file+offset) cursor into the v2 per-file offset map.

    The v1 cursor held one ``(file, offset)`` pair while the loop iterated every daily
    file, so each cycle restarted all ~70 of them at offset 0 and republished the whole
    history. Migrating must not reproduce that: every file *older* than the recorded one
    is treated as fully federated.
    """
    if cursor.get("version") == CURSOR_VERSION and isinstance(cursor.get("files"), dict):
        return cursor

    last_file = cursor.get("last_federated_file")
    last_offset = cursor.get("last_federated_offset", 0)
    migrated = _default_cursor()
    migrated["last_federated_ts"] = cursor.get("last_federated_ts")
    migrated["last_federated_id"] = cursor.get("last_federated_id")

    if last_file:
        for path in sorted(logs_dir.glob("*-cross-agent.jsonl")):
            path_str = str(path)
            if path_str < last_file:
                # Older than the cursor — already federated. Mark complete so the
                # migration itself cannot trigger the replay it exists to prevent.
                migrated["files"][path_str] = path.stat().st_size
            elif path_str == last_file:
                migrated["files"][path_str] = last_offset
            # Newer files are absent from the map and correctly start at offset 0.
    return migrated


def load_cursor() -> dict:
    if CURSOR_FILE.exists():
        try:
            return migrate_cursor(json.loads(CURSOR_FILE.read_text()), LOGS_DIR)
        except (OSError, ValueError) as exc:
            _log.warning("unreadable federation cursor (%s) — starting fresh", exc)
    return _default_cursor()


def save_cursor(cursor: dict) -> None:
    tmp = CURSOR_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cursor, indent=2))
    tmp.rename(CURSOR_FILE)


def federate_once(cursor: dict) -> tuple[dict, int]:
    """Publish one bounded batch of unfederated events. Returns (cursor, published).

    Stops at FEDERATION_MAX_EVENTS_PER_CYCLE or FEDERATION_MAX_SECONDS_PER_CYCLE,
    whichever comes first, and saves the cursor after each file so progress survives a
    mid-cycle restart.
    """
    files = cursor.setdefault("files", {})
    deadline = time.monotonic() + FEDERATION_MAX_SECONDS_PER_CYCLE
    published = 0

    for path in sorted(LOGS_DIR.glob("*-cross-agent.jsonl")):
        path_str = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        start_offset = files.get(path_str, 0)
        if start_offset >= size:
            continue  # fully federated — skip without opening

        advanced = False
        with open(path) as f:
            f.seek(start_offset)
            while published < FEDERATION_MAX_EVENTS_PER_CYCLE and time.monotonic() < deadline:
                line = f.readline()
                if not line:
                    break
                try:
                    e = json.loads(line)
                except ValueError:
                    # Unparseable line: step over it rather than re-reading it forever.
                    files[path_str] = f.tell()
                    advanced = True
                    continue
                if not emit_nats(e):
                    # Not accepted for delivery — stop here so the cursor does not
                    # advance past events that were never published. The gap is
                    # replayed on a later cycle.
                    break
                files[path_str] = f.tell()
                cursor["last_federated_ts"] = e.get("ts")
                cursor["last_federated_id"] = e.get("id")
                published += 1
                advanced = True

        if advanced:
            save_cursor(cursor)
        if published >= FEDERATION_MAX_EVENTS_PER_CYCLE or time.monotonic() >= deadline:
            break

    return cursor, published


async def federation_loop() -> None:
    """
    Background task: replay unfederated cross-agent events to NATS every 30s.
    Uses a per-file offset map so completed files are skipped entirely.
    """
    if not FEDERATION_ENABLED:
        _log.info("federation disabled by AGENT_BUS_FEDERATION=0")
        return

    await asyncio.sleep(FEDERATION_STARTUP_DELAY)  # brief startup delay
    while True:
        try:
            if not acquire_federation_lock():
                # Another process is federating. Retry — it may exit.
                await asyncio.sleep(FEDERATION_INTERVAL)
                continue

            if not _publisher.enabled:
                # No credential configured — the server requires auth, so there is
                # nothing to federate to. Don't walk the corpus every cycle.
                await asyncio.sleep(FEDERATION_INTERVAL)
                continue

            _publisher.start()
            if not _publisher.connected:
                # Anything published now would be dropped. Leave the cursor untouched
                # so the gap is replayed once NATS is reachable again.
                await asyncio.sleep(FEDERATION_INTERVAL)
                continue

            cursor = load_cursor()
            # Enqueueing is non-blocking, but the file walk is not — keep the whole
            # cycle off the event loop so a large batch cannot stall get_status.
            cursor, published = await asyncio.to_thread(federate_once, cursor)
            if published:
                _log.info("federated %d event(s)", published)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("federation_loop error: %s", exc)

        await asyncio.sleep(FEDERATION_INTERVAL)


if __name__ == "__main__":
    mcp.run()
