#!/usr/bin/env python3
"""
reconcile.py — scan ~/.claude/comms/artifacts/ for files not yet in today's cross-agent log.
Runs every 5 minutes from the ted crontab, through hc-run (the PM2 entry in ecosystem.config.js
is not started). Writes directly to JSONL (not via MCP) since it IS
the agent-bus reconciliation path.

A directory that contains a ``.reconcile-ignore`` file is collapsed to one event: the marker
itself is logged (summary ``Ignored tree: <dir>``), and every file beside it and below it is
not. Whoever writes a scratch tree owns its marker (security-check-prep.py writes one into
each prep dir), so there is no path-pattern list here to keep in step with them. The marker
is checked on every scan, whatever the files' mtimes, so one added after the files were
written still collapses them. A skip is never silent: any writer under artifacts/ can drop a
marker, and the event is what shows where one was dropped. A marker is announced by identity
(path and inode, kept in ``.reconcile-markers.json`` beside the cursor), not by mtime, so a
backdated marker, or an old file renamed into place, is still announced. An unreadable or
corrupt state file re-announces every marker: noisy, never silent.

A directory the walk cannot read is printed to stderr and the run exits 1 without moving
the cursor, so the next run retries it, and hc-run reports the failure. While the cursor is
held, the dedup is today's log only, so an error that persists past midnight re-logs what is
newer than the cursor once more: a repeat, never a loss. Before this, an unreadable subtree
read as empty.

Appends through ``event_log`` so its events chain and lock like every other writer's.
It previously appended raw, with no ``prev_hash`` and no lock — which meant a PM2 cron
firing while the server was writing could interleave and break the chain outright.
"""

import json
import os
import sys
from pathlib import Path

from event_log import append_event, build_event
from event_log import log_path as _log_path

COMMS_DIR = Path(os.environ.get("AGENT_BUS_COMMS_DIR") or str(Path.home() / ".claude" / "comms"))
ARTIFACTS_DIR = COMMS_DIR / "artifacts"
LOGS_DIR = COMMS_DIR / "logs"
CURSOR_FILE = COMMS_DIR / ".reconcile-cursor"
# SECURITY[accepted]: the markers state, like the cursor beside it, is writable by the same uid
# as every agent whose artifacts this scans. An agent can pre-seed a marker's `path:inode` here
# and hide its tree with no event, exactly as it can backdate .reconcile-cursor today. The
# reconciler surfaces forgotten artifacts; it has never been a boundary against a same-uid
# agent. Closing it needs per-agent UIDs, vikunja#404. Accepted by the operator 2026-11-01.
# Audit: 2026-11-01/audit-workflow-2026-11-p1-round-capture (pre-audit OV-40).
MARKERS_FILE = COMMS_DIR / ".reconcile-markers.json"
IGNORE_MARKER = ".reconcile-ignore"
HOSTNAME = os.uname().nodename

# Self-healing: create log dir if missing on first run
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def log_path() -> Path:
    return _log_path("cross-agent", LOGS_DIR)


def known_artifact_paths() -> set[str]:
    """
    Returns artifact paths already logged today. Used for intra-day dedup only —
    the mtime cursor prevents reprocessing artifacts from prior days.
    """
    paths: set[str] = set()
    p = log_path()
    if not p.exists():
        return paths
    for line in p.read_text().splitlines():
        try:
            e = json.loads(line)
            if e.get("artifact_path"):
                paths.add(e["artifact_path"])
        except Exception:
            continue
    return paths


def load_markers() -> set[str]:
    """Identities of markers already announced. Anything unreadable reads as none."""
    try:
        data = json.loads(MARKERS_FILE.read_text())
        return {k for k in data if isinstance(k, str)} if isinstance(data, list) else set()
    except Exception:
        return set()


def save_markers(keys: set[str]) -> None:
    tmp = MARKERS_FILE.with_name(MARKERS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(sorted(keys)))
    os.replace(tmp, MARKERS_FILE)


def artifact_files(errors: list[OSError]):
    """(path, ignored_tree) for every file to log: each file outside an ignored tree, and the
    marker of each ignored tree. Directory links are not followed. Walk errors are appended
    to ``errors``, never swallowed."""
    for dirpath, dirnames, filenames in os.walk(ARTIFACTS_DIR, onerror=errors.append):
        if IGNORE_MARKER in filenames:
            dirnames[:] = []
            yield Path(dirpath) / IGNORE_MARKER, True
            continue
        dirnames.sort()
        for name in sorted(filenames):
            yield Path(dirpath) / name, False


def main() -> None:
    cursor_mtime = CURSOR_FILE.stat().st_mtime if CURSOR_FILE.exists() else 0
    known = known_artifact_paths()
    found = 0

    errors: list[OSError] = []
    item_errors: list[str] = []
    announced = load_markers()
    seen: set[str] = set()
    for f, ignored_tree in artifact_files(errors):
        try:
            found += _reconcile_one(f, ignored_tree, cursor_mtime, known, announced, seen)
        except FileNotFoundError:
            continue  # removed between the walk and the stat: nothing left to log
        except Exception as e:  # one bad item (e.g. a non-UTF-8 name) must not stop the scan
            item_errors.append(f"{os.fsencode(f)!r}: {type(e).__name__}")

    if found:
        print(f"reconcile: logged {found} untracked artifact(s)")
    # markers in unread dirs weren't seen; keep their keys rather than re-announce them later
    save_markers(seen | announced if errors or item_errors else seen)
    if errors or item_errors:
        for e in errors:
            print(f"reconcile: could not read {e.filename}: {e.strerror}", file=sys.stderr)
        for line in item_errors:
            print(f"reconcile: could not log {line}", file=sys.stderr)
        # cursor not moved: the unread or unlogged items are retried next run
        raise SystemExit(1)
    CURSOR_FILE.touch()


def _reconcile_one(
    f: Path,
    ignored_tree: bool,
    cursor_mtime: float,
    known: set[str],
    announced: set[str],
    seen: set[str],
) -> int:
    """Log one artifact (or one marker). Returns 1 if an event was appended, else 0."""
    path_str = str(f)
    if ignored_tree:
        # identity, not mtime: a backdated or renamed-in marker must still be announced.
        # lstat: a marker that is a dangling link still hides its tree, so it counts too.
        key = f"{path_str}:{f.lstat().st_ino}"
        if key in announced:
            seen.add(key)
            return 0
    elif not f.is_file() or f.stat().st_mtime <= cursor_mtime or path_str in known:
        return 0

    event = build_event(
        event_type="artifact.untracked",
        source="reconciliation",
        summary=(
            f"Ignored tree: {f.parent.relative_to(ARTIFACTS_DIR)}"
            if ignored_tree
            else f"Untracked artifact: {f.name}"
        ),
        scope="cross-agent",
        artifact_path=path_str,
        hostname=HOSTNAME,
    )
    append_event(event, "cross-agent", LOGS_DIR)
    if ignored_tree:
        seen.add(key)  # only once its event is on the log: a failed append re-announces next run
    return 1


if __name__ == "__main__":
    main()
