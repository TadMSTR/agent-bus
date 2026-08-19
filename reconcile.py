#!/usr/bin/env python3
"""
reconcile.py — scan ~/.claude/comms/artifacts/ for files not yet in today's cross-agent log.
Runs every 5 minutes via PM2 cron. Writes directly to JSONL (not via MCP) since it IS
the agent-bus reconciliation path.

Appends through ``event_log`` so its events chain and lock like every other writer's.
It previously appended raw, with no ``prev_hash`` and no lock — which meant a PM2 cron
firing while the server was writing could interleave and break the chain outright.
"""

import json
import os
from pathlib import Path

from event_log import append_event, build_event
from event_log import log_path as _log_path

COMMS_DIR = Path(os.environ.get("AGENT_BUS_COMMS_DIR") or str(Path.home() / ".claude" / "comms"))
ARTIFACTS_DIR = COMMS_DIR / "artifacts"
LOGS_DIR = COMMS_DIR / "logs"
CURSOR_FILE = COMMS_DIR / ".reconcile-cursor"
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


def main() -> None:
    cursor_mtime = CURSOR_FILE.stat().st_mtime if CURSOR_FILE.exists() else 0
    known = known_artifact_paths()
    found = 0

    for f in ARTIFACTS_DIR.rglob("*"):
        if not f.is_file():
            continue
        if f.stat().st_mtime <= cursor_mtime:
            continue
        path_str = str(f)
        if path_str in known:
            continue

        event = build_event(
            event_type="artifact.untracked",
            source="reconciliation",
            summary=f"Untracked artifact: {f.name}",
            scope="cross-agent",
            artifact_path=path_str,
            hostname=HOSTNAME,
        )
        append_event(event, "cross-agent", LOGS_DIR)
        found += 1

    CURSOR_FILE.touch()
    if found:
        print(f"reconcile: logged {found} untracked artifact(s)")


if __name__ == "__main__":
    main()
