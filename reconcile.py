#!/usr/bin/env python3
"""
reconcile.py — scan ~/.claude/comms/artifacts/ for files not yet in today's cross-agent log.
Runs every 5 minutes via PM2 cron. Writes directly to JSONL (not via MCP) since it IS
the agent-bus reconciliation path.

A directory that contains a ``.reconcile-ignore`` file is collapsed to one event: the marker
itself is logged (summary ``Ignored tree: <dir>``), and every file beside it and below it is
not. Whoever writes a scratch tree owns its marker (security-check-prep.py writes one into
each prep dir), so there is no path-pattern list here to keep in step with them. The marker
is checked on every scan, whatever the files' mtimes, so one added after the files were
written still collapses them. A skip is never silent: any writer under artifacts/ can drop a
marker, and the event is what shows where one was dropped.

A directory the walk cannot read is printed to stderr and the run exits 1 without moving
the cursor, so the next run retries it (the intra-day dedup keeps that from re-logging), and
hc-run reports the failure. Before this, an unreadable subtree read as empty.

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
    for f, ignored_tree in artifact_files(errors):
        if ignored_tree:
            # lstat: a marker that is a dangling link still hides its tree, so it is logged too
            mtime = f.lstat().st_mtime
        elif f.is_file():
            mtime = f.stat().st_mtime
        else:
            continue
        if mtime <= cursor_mtime:
            continue
        path_str = str(f)
        if path_str in known:
            continue

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
        found += 1

    if found:
        print(f"reconcile: logged {found} untracked artifact(s)")
    if errors:
        for e in errors:
            print(f"reconcile: could not read {e.filename}: {e.strerror}", file=sys.stderr)
        # cursor not moved: the unread subtree is retried next run
        raise SystemExit(1)
    CURSOR_FILE.touch()


if __name__ == "__main__":
    main()
