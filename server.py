import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from fastmcp import FastMCP

COMMS_DIR = Path(
    os.environ.get("AGENT_BUS_COMMS_DIR") or str(Path.home() / ".claude" / "comms")
)
LOGS_DIR = COMMS_DIR / "logs"

# Ensure log directory exists on first run
LOGS_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(server):
    asyncio.create_task(federation_loop())
    yield
    # federation_loop is fire-and-forget; process exit cleans it up


mcp = FastMCP("agent-bus", lifespan=lifespan)

CURSOR_FILE = COMMS_DIR / "federation-cursor.json"
HOSTNAME = os.uname().nodename
NTFY_URL = os.environ.get("NTFY_URL", "")
NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
WEBHOOK_URL = os.environ.get("AGENT_BUS_WEBHOOK_URL", "")
WEBHOOK_EVENTS = set(
    e.strip()
    for e in os.environ.get("AGENT_BUS_WEBHOOK_EVENTS", "").split(",")
    if e.strip()
)

CROSS_AGENT_EVENTS = {
    "task.dispatched",
    "task.approved",
    "task.completed",
    "task.failed",
    "task.routing-failed",
    "handoff.created",
    "handoff.picked-up",
    "handoff.completed",
    "audit.requested",
    "audit.completed",
    "build-plan.created",
    "diagnose.started",
    "diagnose.completed",
    "artifact.untracked",
}

HIGH_PRIORITY_EVENTS = {
    "audit.requested",
    "task.failed",
    "task.routing-failed",
    "handoff.created",
}

# ── Hash chaining ─────────────────────────────────────────────────────────────

_append_lock = threading.Lock()


def _last_line(path: Path) -> str | None:
    """Return the last non-empty line of a JSONL file, or None."""
    if not path.exists():
        return None
    try:
        text = path.read_text()
        for line in reversed(text.splitlines()):
            if line.strip():
                return line
    except Exception:
        pass
    return None


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _strip_qs(url: str | None) -> str | None:
    """Return URL with query string removed (prevents token leakage in get_status)."""
    if not url:
        return None
    p = urlparse(url)
    return urlunparse(p._replace(query="", fragment=""))


# ── Key registry ─────────────────────────────────────────────────────────────

KEY_REGISTRY_PATH = COMMS_DIR / "agent-keys.json"
VERIFY_SIGNATURES: str = os.environ.get("AGENT_BUS_VERIFY_SIGNATURES", "warn")


def _load_key_registry() -> dict[str, Any]:
    """Load public key registry from COMMS_DIR/agent-keys.json. Returns {} if absent."""
    if not KEY_REGISTRY_PATH.exists():
        return {}
    try:
        return json.loads(KEY_REGISTRY_PATH.read_text())
    except Exception:
        return {}


def _verify_signature(event: dict) -> bool:
    """Return True if signature is valid, False if invalid or keys unavailable."""
    registry = _load_key_registry()
    source = event.get("source", "")
    entry = registry.get(source)
    if not entry:
        return True  # unknown source — accepted (backwards compatible)

    sig_b64 = (event.get("metadata") or {}).get("sig", "")
    if not sig_b64:
        return True  # unsigned event — accepted in warn mode

    public_key_b64 = entry.get("pubkey", "")
    if not public_key_b64:
        return True

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key_b64)
        )

        # Reconstruct canonical payload — same logic as signing_hook.py
        metadata = {
            k: v
            for k, v in (event.get("metadata") or {}).items()
            if k not in ("sig", "prev_hash")
        }
        payload = {
            "event_type": event.get("event", ""),
            "source": source,
            "summary": event.get("summary", ""),
            "scope": event.get("scope", "cross-agent"),
            "target": event.get("target"),
            "artifact_path": event.get("artifact_path"),
            "metadata": metadata,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        public_key.verify(base64.b64decode(sig_b64), canonical.encode())
        return True
    except Exception:
        return False


def log_path(scope: str) -> Path:
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    suffix = "cross-agent" if scope == "cross-agent" else "session"
    return LOGS_DIR / f"{date}-{suffix}.jsonl"


def append_event(event: dict, scope: str) -> None:
    """Atomic-safe JSONL append with prev_hash chaining and fsync."""
    path = log_path(scope)
    with _append_lock:
        last = _last_line(path)
        if last is not None:
            event["prev_hash"] = _sha256(last)
        line = json.dumps(event, ensure_ascii=False)
        with open(path, "a") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())


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
                f"{_clean(event['source'])} → {_clean(event.get('target') or 'n/a')}: {_clean(event['summary'])}",
            ],
            timeout=5,
            capture_output=True,
        )
    except Exception:
        pass


def emit_nats(event: dict) -> None:
    try:
        subject = f"agent-bus.{HOSTNAME}.events"
        subprocess.run(
            ["nats", "pub", "--server", NATS_URL, subject, json.dumps(event)],
            timeout=5,
            capture_output=True,
        )
    except Exception:
        pass  # NATS unavailable — local log is authoritative


def emit_webhook(event: dict) -> None:
    if not WEBHOOK_URL:
        return
    # "*" in WEBHOOK_EVENTS matches all event types
    if (
        WEBHOOK_EVENTS
        and event["event"] not in WEBHOOK_EVENTS
        and "*" not in WEBHOOK_EVENTS
    ):
        return
    try:
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
    except Exception:
        pass  # webhook failure never blocks event logging


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
    scope_resolved = "cross-agent" if event_type in CROSS_AGENT_EVENTS else scope

    if VERIFY_SIGNATURES in ("warn", "enforce"):
        valid = _verify_signature(event)
        if not valid:
            if VERIFY_SIGNATURES == "enforce":
                return {
                    "id": event["id"],
                    "logged": False,
                    "error": "signature_invalid",
                }
            # warn mode: log and continue
            import logging

            logging.getLogger("agent-bus").warning(
                "signature_invalid source=%s event_type=%s id=%s",
                source,
                event_type,
                event["id"],
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
            prev_line = line
            continue

        # Verify hash chain
        declared_prev = event.get("prev_hash")
        if prev_line is not None:
            expected_prev = _sha256(prev_line)
            if declared_prev != expected_prev:
                chain_breaks += 1
        elif declared_prev is not None:
            # First event should have no prev_hash
            chain_breaks += 1

        # Verify signature
        source = event.get("source", "")
        if source in registry:
            sig = (event.get("metadata") or {}).get("sig", "")
            if not sig:
                unsigned_events += 1
            elif not _verify_signature(event):
                sig_failures += 1

        prev_line = line

    verified = total - chain_breaks - sig_failures - unsigned_events
    return {
        "file": str(path),
        "total_events": total,
        "verified": verified,
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
        try:
            today_count += sum(
                1 for line in path.read_text().splitlines() if line.strip()
            )
        except Exception:
            pass

    key_registry = _load_key_registry()

    return {
        "comms_dir": str(COMMS_DIR),
        "logs_dir": str(LOGS_DIR),
        "hostname": HOSTNAME,
        "signing": {
            "registered_agents": list(key_registry.keys()),
            "verify_mode": VERIFY_SIGNATURES,
        },
        "integrations": {
            "nats": {"enabled": bool(NATS_URL), "url": _strip_qs(NATS_URL)},
            "ntfy": {"enabled": bool(NTFY_URL), "url": _strip_qs(NTFY_URL)},
            "webhook": {
                "enabled": bool(WEBHOOK_URL),
                "url": _strip_qs(WEBHOOK_URL),
                "events": list(WEBHOOK_EVENTS)
                if WEBHOOK_EVENTS
                else ["*"]
                if WEBHOOK_URL
                else [],
            },
        },
        "logs": date_range,
        "events_today": today_count,
    }


# ── Federation background task ─────────────────────────────────────────────────
# Note: emit_nats() is called inline on every log_event(). The federation loop
# replays events from the file+offset cursor — events already published inline will be
# republished by the loop. NATS JetStream dedup (2-min window, configured on AGENT_BUS
# stream) handles recent duplicates. Inline emit is for real-time notification; loop
# replay is gap-fill after NATS downtime. Consumers treat AGENT_BUS as at-least-once.


def load_cursor() -> dict:
    if CURSOR_FILE.exists():
        return json.loads(CURSOR_FILE.read_text())
    return {
        "hostname": HOSTNAME,
        "last_federated_ts": None,
        "last_federated_id": None,
        "last_federated_file": None,
        "last_federated_offset": 0,
    }


def save_cursor(cursor: dict) -> None:
    tmp = CURSOR_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cursor, indent=2))
    tmp.rename(CURSOR_FILE)


async def federation_loop() -> None:
    """
    Background task: replay unfederated cross-agent events to NATS every 30s.
    Uses file+offset tracking to avoid re-scanning entire log history on each tick.
    """
    await asyncio.sleep(10)  # brief startup delay
    while True:
        try:
            cursor = load_cursor()
            last_file = cursor.get("last_federated_file")
            last_offset = cursor.get("last_federated_offset", 0)

            for path in sorted(LOGS_DIR.glob("*-cross-agent.jsonl")):
                path_str = str(path)
                start_offset = last_offset if path_str == last_file else 0

                with open(path) as f:
                    f.seek(start_offset)
                    while True:
                        line = f.readline()
                        if not line:
                            break
                        try:
                            e = json.loads(line)
                            emit_nats(e)
                            cursor["last_federated_ts"] = e["ts"]
                            cursor["last_federated_id"] = e["id"]
                            cursor["last_federated_file"] = path_str
                            cursor["last_federated_offset"] = f.tell()
                        except Exception:
                            continue

            save_cursor(cursor)
        except Exception:
            pass  # federation failure never blocks the server

        await asyncio.sleep(30)


if __name__ == "__main__":
    mcp.run()
