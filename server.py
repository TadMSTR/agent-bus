import asyncio
import json
import os
import subprocess
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastmcp import FastMCP

COMMS_DIR = Path(os.environ.get("AGENT_BUS_COMMS_DIR") or str(Path.home() / ".claude" / "comms"))
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
    e.strip() for e in os.environ.get("AGENT_BUS_WEBHOOK_EVENTS", "").split(",") if e.strip()
)

CROSS_AGENT_EVENTS = {
    "task.dispatched", "task.approved", "task.completed", "task.failed",
    "task.routing-failed", "handoff.created", "handoff.picked-up",
    "handoff.completed", "audit.requested", "audit.completed",
    "build-plan.created",
    "diagnose.started", "diagnose.completed", "artifact.untracked",
}

HIGH_PRIORITY_EVENTS = {
    "audit.requested", "task.failed", "task.routing-failed", "handoff.created",
}


def log_path(scope: str) -> Path:
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    suffix = "cross-agent" if scope == "cross-agent" else "session"
    return LOGS_DIR / f"{date}-{suffix}.jsonl"


def append_event(event: dict, scope: str) -> None:
    """Atomic-safe JSONL append with fsync."""
    path = log_path(scope)
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
                "curl", "-s", "-o", "/dev/null", "-X", "POST", NTFY_URL,
                "-H", f"Title: agent-bus: {_clean(event['event'])}",
                "-H", "Priority: default",
                "-H", "Tags: agent",
                "-d", f"{_clean(event['source'])} → {_clean(event.get('target') or 'n/a')}: {_clean(event['summary'])}",
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
    if WEBHOOK_EVENTS and event["event"] not in WEBHOOK_EVENTS and "*" not in WEBHOOK_EVENTS:
        return
    try:
        subprocess.run(
            [
                "curl", "-s", "-o", "/dev/null", "-X", "POST", WEBHOOK_URL,
                "-H", "Content-Type: application/json",
                "-d", json.dumps(event),
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
    for path in (LOGS_DIR.glob(f"{today}-*.jsonl") if LOGS_DIR.exists() else []):
        try:
            today_count += sum(1 for line in path.read_text().splitlines() if line.strip())
        except Exception:
            pass

    return {
        "comms_dir": str(COMMS_DIR),
        "logs_dir": str(LOGS_DIR),
        "hostname": HOSTNAME,
        "integrations": {
            "nats": {"enabled": bool(NATS_URL), "url": NATS_URL or None},
            "ntfy": {"enabled": bool(NTFY_URL), "url": NTFY_URL or None},
            "webhook": {
                "enabled": bool(WEBHOOK_URL),
                "url": WEBHOOK_URL or None,
                "events": list(WEBHOOK_EVENTS) if WEBHOOK_EVENTS else ["*"] if WEBHOOK_URL else [],
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
