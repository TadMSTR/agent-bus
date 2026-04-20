"""
agent_bus_client.py — direct JSONL writer for non-MCP callers.

For Python scripts that can't call MCP directly (e.g. PM2 cron jobs,
task dispatchers), this module writes events to the same JSONL files
as the server — no MCP round-trip, no external dependency.

Usage:
    from agent_bus_client import log_event

    log_event(
        event_type="task.dispatched",
        source="task-dispatcher",
        target="claudebox",
        summary="Build phase 1 dispatched",
    )
"""
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

COMMS_DIR = Path(os.environ.get("AGENT_BUS_COMMS_DIR") or str(Path.home() / ".claude" / "comms"))
LOGS_DIR = COMMS_DIR / "logs"

CROSS_AGENT_EVENTS = {
    "task.dispatched", "task.approved", "task.completed", "task.failed",
    "task.routing-failed", "handoff.created", "handoff.picked-up",
    "handoff.completed", "audit.requested", "audit.completed",
    "build-plan.created", "diagnose.started", "diagnose.completed",
    "artifact.untracked",
}


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
    Write an event directly to the JSONL log. Returns the event dict with assigned id.
    Uses the same schema as the MCP server — events written here are visible to
    query_events and get_event tool calls.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    hostname = os.uname().nodename
    scope_resolved = "cross-agent" if event_type in CROSS_AGENT_EVENTS else scope
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    suffix = "cross-agent" if scope_resolved == "cross-agent" else "session"
    log_path = LOGS_DIR / f"{date}-{suffix}.jsonl"

    event = {
        "id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "scope": scope_resolved,
        "source": source,
        "target": target,
        "artifact_path": str(artifact_path) if artifact_path else None,
        "summary": summary,
        "hostname": hostname,
        "metadata": metadata or {},
    }

    with open(log_path, "a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())

    return event
