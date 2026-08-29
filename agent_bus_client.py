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

import logging
import os
from pathlib import Path

from event_log import append_event, build_event
from event_vocab import CROSS_AGENT_EVENTS, SESSION_EVENTS, resolve_scope, unknown_event_reason

__all__ = ["CROSS_AGENT_EVENTS", "SESSION_EVENTS", "VocabularyError", "log_event", "resolve_scope"]

_log = logging.getLogger("agent-bus.client")

COMMS_DIR = Path(os.environ.get("AGENT_BUS_COMMS_DIR") or str(Path.home() / ".claude" / "comms"))
LOGS_DIR = COMMS_DIR / "logs"

# THIS WRITER NEEDS THE GATE MORE THAN THE SERVER DOES. task-dispatcher — the emitter
# whose undeclared `task.workflow_started` is the worked example in the build plan —
# writes through here, not through the MCP tool. A vocabulary check that lived only in
# server.py would leave the one caller it was designed for entirely ungated.
#
# Same variable, same default, its own module global so tests can monkeypatch this
# writer's mode independently of the server's.
STRICT_VOCAB: str = os.environ.get("AGENT_BUS_STRICT_VOCAB") or "warn"


class VocabularyError(ValueError):
    """Raised by log_event under `enforce` when the event type is undeclared.

    The server returns `{"logged": False, "error": ...}` for the same condition
    because it is an MCP tool and must answer over the wire. This is a plain Python
    function whose contract is "returns the event dict it wrote", so the equivalent
    is an exception: a caller that ignored a sentinel return would carry on believing
    it had logged something.

    Callers that must not fail on a bus problem should catch it — task-dispatcher's
    `bus_log` is already wrapped for exactly this reason.
    """


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

    Raises VocabularyError for an undeclared event_type when AGENT_BUS_STRICT_VOCAB is
    `enforce`. Under `warn` (the default) it logs and writes anyway — and build_event's
    resolve_scope has already routed it cross-agent, so the event stays visible either
    way. See event_vocab's module docstring.
    """
    vocab_reason = unknown_event_reason(event_type, STRICT_VOCAB)
    if vocab_reason:
        if STRICT_VOCAB == "enforce":
            raise VocabularyError(vocab_reason)
        _log.warning("vocabulary_policy source=%s %s", source, vocab_reason)

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
