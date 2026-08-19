"""
event_log.py — the one append path for the hash-chained JSONL logs.

Three separate writers append to these files: the MCP server, ``agent_bus_client.py``
(for PM2 cron jobs and other non-MCP callers), and ``reconcile.py``. Two of the three
previously appended with no ``prev_hash`` at all, so ``verify_chain`` reported breaks
during entirely normal operation and the tamper-evidence claim did not hold.

The lock was also wrong in kind, not just in scope: ``threading.Lock`` guards one
process, but agent-bus is a module in all seven scoped-mcp manifests, so the PM2
service plus one stdio child per broker — 8+ *processes* — write the same files
concurrently. Chaining is a read-last-line → write sequence, so two processes
interleaving there produce a genuinely broken chain, not merely a lost update.

``append_event()`` therefore holds an ``fcntl.flock`` across the whole read → write →
fsync sequence, with the thread lock retained underneath it for intra-process callers.

Paths are passed in rather than read from module state so tests can redirect writes to
a tmpdir without any risk of appending to the real log corpus.
"""

import fcntl
import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from event_vocab import resolve_scope

# Read backwards in chunks — the previous implementation read the entire file on every
# append, which is O(n^2) across a day and sat on the hot path for all seven agents.
_TAIL_CHUNK = 8192

_append_lock = threading.Lock()


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def log_path(scope: str, logs_dir: Path) -> Path:
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    suffix = "cross-agent" if scope == "cross-agent" else "session"
    return logs_dir / f"{date}-{suffix}.jsonl"


def _last_line_fd(f) -> str | None:
    """Last non-empty line of an open binary file, read from the end.

    Takes an already-open descriptor so the caller can do this under the same flock
    that guards the append.
    """
    f.seek(0, os.SEEK_END)
    size = f.tell()
    if size == 0:
        return None

    buf = b""
    pos = size
    while pos > 0:
        step = min(_TAIL_CHUNK, pos)
        pos -= step
        f.seek(pos)
        buf = f.read(step) + buf
        parts = buf.split(b"\n")
        # With pos > 0, parts[0] may be a partial line — only trust the rest.
        candidates = parts if pos == 0 else parts[1:]
        for cand in reversed(candidates):
            if cand.strip():
                return cand.decode("utf-8", errors="replace")
    return None


def last_line(path: Path) -> str | None:
    """Last non-empty line of a JSONL file, or None."""
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            return _last_line_fd(f)
    except OSError:
        return None


def build_event(
    event_type: str,
    source: str,
    summary: str,
    scope: str = "cross-agent",
    target: str | None = None,
    artifact_path: str | None = None,
    metadata: dict | None = None,
    hostname: str | None = None,
) -> dict:
    """Construct an event in the canonical schema. Shared by every writer."""
    return {
        "id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "scope": resolve_scope(event_type, scope),
        "source": source,
        "target": target,
        "artifact_path": str(artifact_path) if artifact_path else None,
        "summary": summary,
        "hostname": hostname or os.uname().nodename,
        "metadata": metadata or {},
    }


def append_event(event: dict, scope: str, logs_dir: Path) -> Path:
    """Append one event, chaining it onto the current last line.

    Holds an exclusive flock across read-last-line → write → fsync so concurrent
    processes cannot interleave and break the chain.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = log_path(scope, logs_dir)

    # "a+b" gives O_APPEND, so the write lands at the end regardless of the seeks
    # done while reading the tail.
    with _append_lock, open(path, "a+b") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            last = _last_line_fd(f)
            if last is not None:
                event["prev_hash"] = sha256(last)
            line = json.dumps(event, ensure_ascii=False)
            f.write((line + "\n").encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return path
