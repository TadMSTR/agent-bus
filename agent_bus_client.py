"""
agent_bus_client.py — direct JSONL writer for non-MCP callers.

For Python scripts that can't call MCP directly (e.g. PM2 cron jobs,
task dispatchers), this module writes events to the same JSONL files
as the server — no MCP round-trip, no external dependency.

It writes through ``event_log``, so events land chained and flock-guarded exactly as
the server writes them. Previously it appended with no ``prev_hash`` and no lock, which
is one of the two reasons ``verify_chain`` reported breaks during normal operation.

The event vocabulary comes from ``event_vocab`` rather than a local copy. The local
copy had gone stale — it was missing ``preflight.*``, ``build.*``, ``deploy.*`` and
``security.finding``, so callers logging ``build.completed`` silently landed in the
session file, invisible to ``query_events(scope="cross-agent")`` and never federated.

Usage:
    from agent_bus_client import log_event

    log_event(
        event_type="task.dispatched",
        source="task-dispatcher",
        target="claudebox",
        summary="Build phase 1 dispatched",
    )
"""

import os
from pathlib import Path

from event_log import append_event, build_event
from event_vocab import CROSS_AGENT_EVENTS, resolve_scope

__all__ = ["CROSS_AGENT_EVENTS", "log_event", "resolve_scope"]

COMMS_DIR = Path(os.environ.get("AGENT_BUS_COMMS_DIR") or str(Path.home() / ".claude" / "comms"))
LOGS_DIR = COMMS_DIR / "logs"


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
    query_events and get_event tool calls, and chain onto the preceding event.
    """
    event = build_event(
        event_type=event_type,
        source=source,
        summary=summary,
        scope=scope,
        target=target,
        artifact_path=artifact_path,
        metadata=metadata,
    )
    append_event(event, event["scope"], LOGS_DIR)
    return event
