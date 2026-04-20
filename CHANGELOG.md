# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

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
