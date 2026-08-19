# agent-bus

FastMCP server and client library for cross-agent event logging on forge.

## What it does

Provides structured event logging to JSONL files in `~/.claude/comms/logs/`. Agents emit
events (task dispatched, completed, failed, etc.) through the MCP server or the direct
client library. Events are hash-chained so the log is tamper-evident.

A background federation loop **publishes** local cross-agent events to NATS. It is
one-directional — nothing is consumed from remote hosts, and there is no inbound sync.

## Tools

- `log_event` — Write an event to the JSONL log. Chain-hashed with SHA-256.
- `get_event` — Retrieve a single event by ID.
- `query_events` — Filter events by type, source, target, or time range.
- `get_status` — Server health, log stats, publisher and federation state.
- `verify_chain` — Audit SHA-256 hash chain integrity for a log file.

## Structure

```
agent-bus/
  server.py              FastMCP server — 5 tools, federation_loop background task
  event_log.py           The one append path: flock + prev_hash chaining + fsync
  event_vocab.py         Single definition of the event vocabulary
  nats_publisher.py      Persistent NATS connection on a background thread
  agent_bus_client.py    Direct JSONL writer for non-MCP callers (PM2 cron jobs)
  reconcile.py           Scans the artifacts dir for files with no logged event
  cleanup.sh             Prune old JSONL log files
  ecosystem.config.js    PM2 process config
  pyproject.toml         deps: fastmcp, cryptography, nats-py
  tests/                 pytest tests
```

## Dependencies

| Package      | Role                                        |
|--------------|---------------------------------------------|
| fastmcp      | MCP server framework                        |
| cryptography | ed25519 signature verification              |
| nats-py      | NATS federation publishing                  |

ntfy and webhook delivery shell out to `curl`; there is no HTTP client dependency.

## Configuration

| Env var                            | Default             | Purpose                                              |
|------------------------------------|---------------------|------------------------------------------------------|
| `AGENT_BUS_COMMS_DIR`              | `~/.claude/comms`   | Root directory for JSONL logs, cursors, key registry |
| `NATS_URL`                         | `nats://localhost:4222` | NATS server for federation publishing            |
| `NATS_AGENT_BUS_USER`              | `agent-bus`         | NATS username                                        |
| `NATS_AGENT_BUS_PASSWORD`          | —                   | NATS password. Unset disables publishing entirely.   |
| `AGENT_BUS_VERIFY_SIGNATURES`      | `warn`              | `warn` (log and accept) or `enforce` (reject)        |
| `AGENT_BUS_SIG_MAX_AGE`            | `300`               | Freshness window, seconds, for `sig_v>=2` signatures |
| `NTFY_URL`                         | —                   | ntfy push for high-priority events                   |
| `AGENT_BUS_WEBHOOK_URL`            | —                   | Webhook endpoint for event delivery                  |
| `AGENT_BUS_WEBHOOK_EVENTS`         | —                   | Comma-separated event types to forward (`*` = all)   |
| `AGENT_BUS_FEDERATION`             | `1`                 | Set `0` to disable the federation loop               |
| `AGENT_BUS_FEDERATION_INTERVAL`    | `30`                | Seconds between federation cycles                    |
| `AGENT_BUS_FEDERATION_MAX_EVENTS`  | `500`               | Cap on events published per cycle                    |
| `AGENT_BUS_FEDERATION_MAX_SECONDS` | `10`                | Wall-clock cap per cycle                             |
| `AGENT_BUS_CROSS_AGENT_RETENTION_DAYS` | `90`            | `cleanup.sh` retention for cross-agent logs          |
| `AGENT_BUS_SESSION_RETENTION_DAYS` | `30`                | `cleanup.sh` retention for session logs              |

## Key architecture decisions

- **One append path.** `event_log.append_event()` is the only writer. It holds an
  `fcntl.flock` across read-last-line → write → fsync, with a thread lock underneath.
  The flock is not optional: agent-bus is a module in all seven scoped-mcp manifests, so
  the PM2 service plus one stdio child per broker — 8+ *processes* — write these files.
  A `threading.Lock` cannot serialise those, and chaining is a read-then-write sequence,
  so interleaving there breaks the chain outright.

- **`agent_bus_client.py` exists for PM2 cron jobs** — PM2 processes can't make MCP
  calls, so this module writes the same JSONL format directly, through `event_log`.

- **One vocabulary.** `event_vocab.py` defines `CROSS_AGENT_EVENTS` and
  `HIGH_PRIORITY_EVENTS`; server and client both import it. They used to carry separate
  copies, and the client's went stale — events like `build.completed` silently landed in
  the session log instead of the cross-agent log.

- **Event routing split** — `CROSS_AGENT_EVENTS` go to the cross-agent log and are
  federated. Everything else is local-only. `HIGH_PRIORITY_EVENTS` also trigger ntfy.

- **NATS publishing is fire-and-forget.** `nats_publisher.NatsPublisher` owns one
  background thread, one asyncio loop, and one persistent connection. `publish()` enqueues
  onto a bounded queue and returns immediately — it never blocks and never raises, so a
  NATS outage cannot affect the JSONL write, which is authoritative. Credentials are
  passed as `connect()` keyword arguments and never appear in any process's argv.

- **Federation is one process, elected by flock.** `federation_loop` is started from the
  FastMCP lifespan, so every process running this server starts one. They take a
  non-blocking exclusive lock on `$AGENT_BUS_COMMS_DIR/.federation.lock`; the holder
  federates and the rest retry, so it fails over without configuration.

- **The federation cursor is a per-file offset map** (`federation-cursor.json`, version 2).
  Version 1 held a single `(file, offset)` pair while the loop iterated every daily file,
  so each cycle restarted ~70 files at offset 0 and republished the whole history. A v1
  cursor is migrated on load, treating every file older than the recorded one as complete.
  Cycles are capped by event count and wall-clock time.

- **Chain hashing** — each event includes a SHA-256 hash of the previous line, so
  `verify_chain` detects tampering or deletion.

- **Signatures.** ed25519, verified against `$AGENT_BUS_COMMS_DIR/agent-keys.json`. In
  `enforce`, unsigned events and events from unregistered sources are rejected; `warn`
  logs and accepts. Note that `ts`, `id`, `hostname` and `prev_hash` are assigned by the
  server *after* the client signs, so they cannot be part of the signed payload. Replay
  protection therefore relies on signer-supplied `sig_ts` and `sig_nonce` inside
  `metadata`, declared by `sig_v: 2`. Signers that predate that are accepted but cannot
  be replay-checked.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

Unit tests use fixtures in `tests/`. No external NATS server is required — the publisher
is stubbed, and the connection path is exercised against a fake `nats` module.

## Git workflow

Branch before editing — do not commit directly to `main`.
