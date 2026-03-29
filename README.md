# agent-bus-mcp

A FastMCP server that provides a unified inter-agent event log for multi-agent Claude Code setups. Agents log communication events (task handoffs, audit requests, build completions) via MCP tools; events are written to local JSONL files and federated to NATS JetStream for real-time observability.

## Why

When multiple Claude Code agents run concurrently — a dev agent, a security agent, a writer agent — they have no shared communication channel. Events like "the claudebox agent handed off a task to the security agent" exist only in session notes or memory files, with no queryable history.

`agent-bus-mcp` provides a lightweight event bus:
- Agents call `log_event` when they produce or consume work items
- Events are indexed in JSONL files by date and scope
- A background federation loop replays events to NATS JetStream for downstream consumers
- A reconciler catches artifacts (build plans, audit requests, handoffs) that were created without a corresponding log event

## Architecture

```
Claude Code Agent
    │
    │  log_event(event_type, source, target, summary, ...)
    ▼
server.py (FastMCP, stdio transport)
    │
    ├── append to ~/.claude/comms/logs/YYYY-MM-DD-{scope}.jsonl
    ├── emit_nats() — inline publish to agent-bus.{hostname}.events
    └── emit_ntfy() — push notification for high-priority events
          (audit.requested, task.failed, task.routing-failed, handoff.created)

Background federation loop (every 30s):
    Read logs from file+offset cursor → publish unseen events to NATS
    (gap-fill for NATS downtime; inline emit handles real-time)

reconcile.py (PM2 cron, every 5 min):
    Scan ~/.claude/comms/artifacts/ for files newer than mtime cursor
    → log artifact.untracked events for each file not yet in today's log

cleanup.sh (PM2 cron, 3:50 AM daily):
    Delete cross-agent logs older than 90 days
    Delete session logs older than 30 days
```

## Installation

```bash
git clone <repo> ~/repos/agent-bus
cd ~/repos/agent-bus
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Register with PM2:

```bash
pm2 start ecosystem.config.js
pm2 save
```

Configure as an MCP server in your Claude Desktop or Claude Code settings:

```json
{
  "mcpServers": {
    "agent-bus": {
      "command": "/path/to/agent-bus/venv/bin/python3",
      "args": ["/path/to/agent-bus/server.py"],
      "env": {
        "NATS_URL": "nats://localhost:4222",
        "NTFY_URL": "https://your-ntfy-server/your-topic"
      }
    }
  }
}
```

`NATS_URL` and `NTFY_URL` are optional — the server operates without them (local JSONL log only).

## MCP Tools

### `log_event`

Log an inter-agent communication event.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `event_type` | str | yes | Event vocabulary string (see below) |
| `source` | str | yes | Originating agent name |
| `summary` | str | yes | One-line human-readable description |
| `scope` | str | no | `"cross-agent"` (default) or `"session"` |
| `target` | str | no | Receiving agent name |
| `artifact_path` | str | no | Absolute path to related file |
| `metadata` | dict | no | Arbitrary key-value context |

Returns `{"id": "<uuid>", "logged": true, "scope": "<scope>"}`.

### `query_events`

Query the event log with optional filters.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `since` | str | none | ISO timestamp lower bound |
| `source` | str | none | Filter by source agent |
| `target` | str | none | Filter by target agent |
| `event_type` | str | none | Filter by event type |
| `scope` | str | `"cross-agent"` | Log file scope |
| `limit` | int | 50 | Max results (cap: 500) |

Returns events most-recent-first.

### `get_event`

Retrieve a single event by UUID.

## Event Vocabulary

Events that automatically route to `cross-agent` scope regardless of the `scope` parameter:

| Event | When to use |
|-------|-------------|
| `task.dispatched` | Task written to agent queue |
| `task.approved` | Task approved for execution |
| `task.completed` | Task completed by agent |
| `task.failed` | Task failed or rejected |
| `task.routing-failed` | No agent manifest match found |
| `handoff.created` | Build plan or work item handed to another agent |
| `handoff.picked-up` | Agent picked up a handoff |
| `handoff.completed` | Handoff resolved |
| `audit.requested` | Security audit request written |
| `audit.completed` | Security audit report written |
| `build-plan.created` | New build plan added to queue |
| `diagnose.started` | Diagnostic session begun |
| `diagnose.completed` | Diagnostic session concluded |
| `artifact.untracked` | File in artifacts dir with no log entry (reconciler) |

High-priority events that also trigger a push notification: `audit.requested`, `task.failed`, `task.routing-failed`, `handoff.created`.

For session-scoped events (memory flushes, skill executions, etc.), use `scope="session"` — these go to a separate daily log file and are not federated to NATS.

## Storage Layout

```
~/.claude/comms/
├── logs/
│   ├── 2026-03-29-cross-agent.jsonl   # inter-agent events
│   └── 2026-03-29-session.jsonl       # session-scoped events
├── artifacts/
│   ├── build-plans/
│   ├── audit-requests/
│   ├── audit-reports/
│   ├── diagnose-sessions/
│   └── handoffs/
├── federation-cursor.json             # NATS federation offset tracker
└── .reconcile-cursor                  # reconciler mtime watermark
```

Each JSONL line is a complete event object:

```json
{
  "id": "a1b2c3d4-...",
  "ts": "2026-03-29T14:30:00+00:00",
  "event": "handoff.created",
  "scope": "cross-agent",
  "source": "claudebox",
  "target": "security",
  "artifact_path": "/home/user/.claude/comms/artifacts/audit-requests/my-build/request.md",
  "summary": "Security audit request: my-build",
  "hostname": "myhost",
  "metadata": {}
}
```

## NATS Federation

Events are published to `agent-bus.{hostname}.events` on the local NATS server. The AGENT_BUS JetStream stream should subscribe to `agent-bus.>` subjects with:
- 30-day retention
- 2-minute dedup window (covers inline + federation-loop double-publish)
- Storage: file

The federation loop re-publishes from the file cursor every 30 seconds to fill gaps from NATS downtime. Consumers should treat the stream as **at-least-once**.

## Non-MCP Callers

For Python scripts that can't call MCP directly (e.g., PM2 cron jobs), use `agent_bus_client.py`:

```python
from agent_bus_client import log_event

log_event(
    event_type="task.dispatched",
    source="task-dispatcher",
    target="claudebox",
    summary="Build phase 1 dispatched to claudebox agent",
)
```

The client writes directly to the JSONL files using the same schema as the server — no MCP round-trip, no external dependency.

## Requirements

- Python 3.11+
- `fastmcp==3.1.0`
- NATS CLI on PATH (optional, for federation)
- `curl` on PATH (optional, for ntfy notifications)
