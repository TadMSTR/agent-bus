# agent-bus

FastMCP server and client library for cross-agent event logging on forge.

## What it does

Provides structured event logging to JSONL files in `~/.claude/comms/logs/`. Agents emit events (task dispatched, completed, failed, etc.) through the MCP server or the direct client library. A background federation loop syncs events from remote hosts via NATS.

## Tools

- `log_event` — Write an event to the JSONL log. Chain-hashed with SHA-256.
- `get_event` — Retrieve a single event by ID.
- `query_events` — Filter events by type, source, target, or time range.
- `get_status` — Server health and log stats.
- `verify_chain` — Audit SHA-256 hash chain integrity for a log file.

## Structure

```
agent-bus/
  server.py              FastMCP server — 5 tools, federation_loop background task
  agent_bus_client.py    Direct JSONL writer for non-MCP callers (PM2 cron jobs)
  reconcile.py           Reconciliation script for chain integrity audits
  cleanup.sh             Prune old JSONL log files
  ecosystem.config.js    PM2 stdio process config
  pyproject.toml         deps: fastmcp, structlog, nats-py, httpx
  tests/                 pytest tests
```

## Dependencies

| Package    | Role                          |
|------------|-------------------------------|
| fastmcp    | MCP server framework          |
| structlog  | JSON structured logging       |
| nats-py    | NATS federation sync          |
| httpx      | Webhook delivery              |

## Configuration

| Env var                      | Default                       | Purpose                                    |
|------------------------------|-------------------------------|--------------------------------------------|
| `AGENT_BUS_COMMS_DIR`        | `~/.claude/comms`             | Root directory for JSONL log files         |
| `NATS_URL`                   | —                             | NATS server for federation sync            |
| `NTFY_URL`                   | —                             | ntfy push notifications for high-priority events |
| `AGENT_BUS_WEBHOOK_URL`      | —                             | Webhook endpoint for event delivery        |
| `AGENT_BUS_WEBHOOK_EVENTS`   | —                             | Comma-separated event types to forward     |

## Key architecture decisions

- **`agent_bus_client.py` exists for PM2 cron jobs** — PM2 processes can't make MCP calls, so this module writes the same JSONL format directly. Logs are consistent regardless of which path was used.
- **Event routing split** — `CROSS_AGENT_EVENTS` are published to the NATS federation subject and replicated to remote nodes. Events outside this set are local-only. `HIGH_PRIORITY_EVENTS` (`audit.requested`, `task.failed`, `task.routing-failed`, `handoff.created`) trigger ntfy push if `NTFY_URL` is set.
- **Chain hashing** — each event includes a SHA-256 hash of the previous event, enabling `verify_chain` to detect any tampering or deletion in the JSONL log.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

Unit tests use fixtures in `tests/`. No external NATS server is required.

## Git workflow

Branch before editing — do not commit directly to `main`.
