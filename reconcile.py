#!/usr/bin/env python3
"""
reconcile.py — scan ~/.claude/comms/artifacts/ for files not yet in today's cross-agent log.
Runs every 5 minutes via PM2 cron. Writes directly to JSONL (not via MCP) since it IS
the agent-bus reconciliation path.
"""
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

COMMS_DIR = Path.home() / ".claude" / "comms"
ARTIFACTS_DIR = COMMS_DIR / "artifacts"
LOGS_DIR = COMMS_DIR / "logs"
CURSOR_FILE = COMMS_DIR / ".reconcile-cursor"
HOSTNAME = os.uname().nodename

# Self-healing: create log dir if missing (e.g. fresh Helm host)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def log_path() -> Path:
    return LOGS_DIR / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-cross-agent.jsonl"


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

        event = {
            "id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "artifact.untracked",
            "scope": "cross-agent",
            "source": "reconciliation",
            "target": None,
            "artifact_path": path_str,
            "summary": f"Untracked artifact: {f.name}",
            "hostname": HOSTNAME,
            "metadata": {},
        }
        with open(log_path(), "a") as lf:
            lf.write(json.dumps(event) + "\n")
        found += 1

    CURSOR_FILE.touch()
    if found:
        print(f"reconcile: logged {found} untracked artifact(s)")


if __name__ == "__main__":
    main()
