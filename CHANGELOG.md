# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.2.0] — 2026-05-26

### Added

- **Hash chaining** — each event written by `append_event()` includes a `prev_hash` field
  (SHA-256 of the previous JSONL line). Establishes a tamper-evident chain across all log
  entries in a file.
- **Threading lock** — `append_event()` acquires `_append_lock` before reading the last
  line and holds it through the `open().write().fsync()` sequence. Prevents concurrent
  write corruption under multi-threaded FastMCP.
- **Signature verification** (`_verify_signature`) — verifies ed25519 signatures on
  incoming `log_event` calls against a per-source key registry at
  `~/.claude/comms/agent-keys.json`. Defaults to `warn` mode (log and accept); set
  `AGENT_BUS_VERIFY_SIGNATURES=enforce` to reject invalid signatures.
- **`verify_chain` tool** — walks a JSONL log file and returns `{total_events, verified,
  chain_breaks, sig_failures, unsigned_events}`. Useful for post-incident integrity checks.
- **Key registry in `get_status`** — `get_status()` now returns `signing.registered_agents`
  and `signing.verify_mode`.

### Security

- **URL query string stripping in `get_status`** — integration URLs (ntfy, webhook) are
  stripped of query parameters before being returned, preventing embedded auth tokens from
  leaking to callers.
- **`date` parameter validation in `verify_chain`** — date is validated against
  `\d{4}-\d{2}-\d{2}` before use in filename construction, preventing path traversal.

### Dependencies

- `cryptography>=41.0` added to `requirements.txt` (used by `_verify_signature` for
  ed25519 public key verification).

## [0.1.0] — 2026-04-20

### Added
- `AGENT_BUS_COMMS_DIR` env var — base directory for logs, artifacts, and cursors is now
  configurable (default: `~/.claude/comms`). Propagated to `server.py`, `reconcile.py`,
  `cleanup.sh`, and `ecosystem.config.js`.
- `AGENT_BUS_CROSS_AGENT_RETENTION_DAYS` and `AGENT_BUS_SESSION_RETENTION_DAYS` env vars —
  log retention periods are now configurable in `cleanup.sh` (defaults: 90 and 30 days).
- `AGENT_BUS_WEBHOOK_URL` and `AGENT_BUS_WEBHOOK_EVENTS` env vars — fire-and-forget HTTP
  webhook support; POSTs event JSON on matching event types (`*` fires on all events).
- `get_status` MCP tool — returns current server configuration and health: configured paths,
  active integrations (NATS/ntfy/webhook), log date range, and today's event count.
- `agent_bus_client.py` — direct JSONL writer for non-MCP callers (PM2 cron jobs, task
  dispatchers); uses the same event schema as the server.
- GitHub Actions CI workflow — import smoke test on Python 3.11/3.12/3.13 plus `pip-audit`
  dependency security audit.
- CI badge in README.

### Changed
- README: added optional components table (NATS, ntfy, webhook), full environment variables
  reference table, `get_status` tool documentation, real clone URL, updated storage layout
  to reference `$AGENT_BUS_COMMS_DIR`.
- Removed Helm-specific language from code comments.

### Fixed
- Upgraded `fastmcp` from 3.1.0 to 3.2.4 to resolve CVE-2025-64340 and CVE-2026-27124.
