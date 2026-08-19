# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.3.0] — 2026-08-19

Hardening pass plus the repo-standard backfill. Tracked as vikunja#433; also closes
vikunja#432 (federation cursor) and vikunja#431 (steward credential, deployed separately).

### Security

- **The NATS credential is no longer passed on the command line.** `emit_nats()` shelled
  out to `nats pub --server … --user … --password <plaintext> …`, once per event, so the
  password was continuously readable by any local user via `ps` / `/proc/*/cmdline`. It is
  now a `connect()` keyword argument to `nats-py`. **The previous credential must be
  treated as compromised and rotated** — removing it from argv does not undo the exposure.
- **`AGENT_BUS_VERIFY_SIGNATURES=enforce` now enforces something.** It previously returned
  "valid" when the source was absent from the key registry, when the event carried no
  signature at all, and when the registry entry had no pubkey — so only a *badly* signed
  event was ever rejected, which is not a case an attacker produces. `enforce` now rejects
  unsigned events and events claiming an unregistered source. `warn` is unchanged.
- **Replay rejection for `sig_v: 2` signatures.** A captured event replayed verbatim
  carries a valid signature, so signing alone cannot stop it. Signers declaring `sig_v: 2`
  must include `sig_ts` and `sig_nonce` in `metadata` (already covered by the canonical
  payload); both are enforced, with a freshness window set by `AGENT_BUS_SIG_MAX_AGE`.
  Version-1 signatures remain valid but cannot be replay-checked — the signer lives in
  scoped-mcp and is updated separately.
- **`get_status` no longer leaks URL userinfo.** `_strip_qs` dropped the query and
  fragment but kept `netloc`, so `nats://user:pass@host` was returned intact to any
  calling agent. Userinfo is now stripped too. (v0.2.0 advertised this leak as closed.)

### Fixed

- **Federation no longer replays the entire event history every cycle.** The cursor held a
  single `(file, offset)` pair while the loop iterated all ~70 daily files, so every file
  that was not the cursor's file restarted at offset 0 — roughly 2,700 events republished
  per cycle, one subprocess each, and `save_cursor()` only ran after the whole loop, which
  is why the cursor appeared frozen. Replaced with a per-file offset map (cursor version 2)
  that skips completed files. A version-1 cursor is migrated on load, treating every file
  older than the recorded one as fully federated so the migration cannot itself replay.
- **Federation runs in exactly one process.** `federation_loop` is started from the FastMCP
  lifespan, so it ran in the PM2 service *and* in every scoped-mcp stdio child — each
  replaying the whole corpus and racing the others' read-modify-write on the shared cursor
  file. Processes now contend for an `fcntl.flock`; the holder federates and the rest retry.
- **The hash chain is sound across processes.** `_append_lock` was a `threading.Lock`, but
  8+ separate processes write these files. Appends now hold an `fcntl.flock` across the
  whole read-last-line → write → fsync sequence.
- **All three writers chain.** `agent_bus_client.py` and `reconcile.py` appended with no
  `prev_hash` and no lock, so `verify_chain` reported breaks during entirely normal
  operation and the tamper-evidence claim did not hold. Both now write through
  `event_log.append_event()`.
- **`agent_bus_client.py` no longer carries a stale copy of the event vocabulary.** It was
  missing the seven types added in PR #5 (`preflight.*`, `build.*`, `deploy.*`,
  `security.finding`), so non-MCP callers logging `build.completed` landed in the *session*
  log — invisible to `query_events(scope="cross-agent")` and never federated. Both writers
  now import `event_vocab`.
- **`verify_chain` no longer double-subtracts.** `verified = total - chain_breaks -
  sig_failures - unsigned_events` counted an event that was both unsigned and chain-broken
  twice, and could return a negative count.
- **The federation loop no longer blocks the event loop.** It was `async` but called
  `subprocess.run()` synchronously per event, stalling everything else for the duration of
  the replay. Publishing is now an enqueue, and the file walk runs via `asyncio.to_thread`.
- **`build-backend` was not a real setuptools backend.** `setuptools.backends.legacy:build`
  does not exist, so `pip install -e ".[dev]"` — the command AGENTS.md tells you to run —
  failed. Corrected to `setuptools.build_meta`.

### Added

- `nats_publisher.NatsPublisher` — one background thread, one asyncio loop, one persistent
  connection, and a bounded queue. Replaces a process spawn per event on the hot path of
  every `log_event()` call. Publishing never blocks and never raises; the JSONL write stays
  authoritative. Publisher counters are exposed via `get_status`.
- `event_log.py` — the single append path (flock, chaining, fsync) shared by all writers.
- `event_vocab.py` — the single definition of the event vocabulary.
- Per-cycle federation caps (`AGENT_BUS_FEDERATION_MAX_EVENTS`,
  `AGENT_BUS_FEDERATION_MAX_SECONDS`) so a future cursor reset degrades to slower
  catch-up rather than an 18-minute blocking cycle, plus `AGENT_BUS_FEDERATION` and
  `AGENT_BUS_FEDERATION_INTERVAL`.
- `nats-py` is now a real dependency. AGENTS.md had claimed it was one since May.

### Changed

- **CI actually runs the test suite.** The test job was
  `python -c "import server; import reconcile; import agent_bus_client"`, and
  `pip install -r requirements.txt` does not install pytest — so `tests/test_server.py`,
  added in PR #5, had never once executed in CI. CI now installs `.[dev]` and runs `ruff
  check`, `ruff format --check`, and `pytest` with coverage, and a new job builds the
  wheel and imports it. That build step is why the invalid backend went unnoticed since May.
- Test suite grown from 18 to 147 tests; coverage floor of 80% enforced in CI.
- `_last_line()` seeks from the end instead of reading the whole file on every append —
  it was O(n²) across a day, on the hot path for all seven agents.
- AGENTS.md rewritten from the code. It listed `structlog`, `nats-py` and `httpx` as
  dependencies when none were, described federation as syncing events *from* remote hosts
  when it only publishes, described `reconcile.py` as a chain-integrity auditor when it
  scans the artifacts directory, and omitted `NATS_AGENT_BUS_PASSWORD` and
  `AGENT_BUS_VERIFY_SIGNATURES` entirely.
- `ruff` (line-length 100, `E,F,W,I,UP,B,SIM,RUF`) and `[tool.coverage]` added.

### Deviations from the repo standard, accepted rather than fixed

- **Flat layout** instead of `src/<package>/`. Moving it would break every import path plus
  `ecosystem.config.js` and `conftest.py`'s `sys.path` insert, for a repo of this size.
- **Stdlib `logging`** instead of structured logging.

### Note

v0.2.0 was written to this changelog on 2026-05-26 but never tagged, so `release.yml` never
fired for it. The entries between it and this release — PR #5's test suite and vocabulary
expansion, `AGENT_BUS_VERIFY_SIGNATURES`, SECURITY.md, AGENTS.md, and PR #6's NATS
authentication fix — shipped unreleased and are covered here.

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
