#!/usr/bin/env python3
"""
reconcile.py — scan ~/.claude/comms/artifacts/ for files not yet in today's cross-agent log.
Runs every 5 minutes from cron (the PM2 entry in ecosystem.config.js is not started). Writes
directly to JSONL (not via MCP) since it IS the agent-bus reconciliation path.

A directory that contains a ``.reconcile-ignore`` file is collapsed to one event: the marker
itself is logged (summary ``Ignored tree: <dir>``), and every file beside it and below it is
not. Whoever writes a scratch tree owns its marker (security-check-prep.py writes one into
each prep dir), so there is no path-pattern list here to keep in step with them. The marker
is checked on every scan, whatever the files' mtimes, so one added after the files were
written still collapses them.

A marker is honoured only at depth 3 or deeper under artifacts/ (a prep dir,
``audit-requests/<build>/prep``, is depth 3). One at depth 0-2 (the root, a category dir such
as ``build-plans/``, or a dir directly under one such as ``audit-requests/<build>/``) would hide
every build in a category or a build's requests, so it is not honoured: that tree is scanned as
usual, and the marker is logged as ``Marker not honoured: <dir>`` once a day for as long as it
stays. Depth is the whole test. It guards against a marker dropped one level too high; it does
not know what a scratch dir is, so a deeper marker is honoured wherever it is: most often a
programme part's plan dir (``build-plans/<programme>/<part>/``, where most live plans sit),
an archived build's dir (``build-plans/archive/<build>/``) or a nested request dir.

What is announced is a marker's *placement*, once, not what it hides afterwards: a file
written into an ignored tree later produces no event, by design. Any writer under artifacts/
can drop a marker, and the event is what shows where. A marker is announced by identity
(path and inode, kept in ``.reconcile-markers.json`` beside the cursor), not by mtime, so a
backdated marker, or an old file renamed into place, is still announced. An unreadable,
corrupt or non-regular state file re-announces every marker. The state file is writable by
the same uid as every agent, so a key written into it by hand hides a tree with no event at
all (see SECURITY[accepted] at MARKERS_FILE).

A name that is not valid UTF-8 is logged with its undecodable bytes escaped as ``\\xNN``, so it
lands as a normal event rather than failing every run. A literal backslash in any name is
logged doubled, so the escaping is one-to-one: no two names share a logged path, which is
also the dedup key.

A directory the walk cannot read is printed to stderr and the run exits 1 without moving
the cursor, so the next run retries it, and the cron wrapper reports the failure. While the
cursor is held, the dedup is today's log only, so an error that persists past midnight
re-logs what is newer than the cursor once more: a repeat, never a loss. Before this, an
unreadable subtree read as empty.

Appends through ``event_log`` so its events chain and lock like every other writer's.
It previously appended raw, with no ``prev_hash`` and no lock — which meant a PM2 cron
firing while the server was writing could interleave and break the chain outright.
"""

import json
import os
import stat
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
# and hide its tree with no event. That is persistent, not one window like a forward-dated
# .reconcile-cursor (the next run touches the cursor back to now): a seen key is carried
# forward every run for as long as the marker exists. The reconciler surfaces forgotten
# artifacts; it has never been a boundary against a same-uid agent. Closing it needs per-agent
# UIDs, vikunja#404. Accepted by the operator 2026-11-01; ruling upheld by audit F-04.
# Audit: 2026-11-01/audit-workflow-2026-11-p1-round-capture (pre-audit OV-40, audit F-04).
MARKERS_FILE = COMMS_DIR / ".reconcile-markers.json"
# Must match IGNORE_MARKER in host-forge-scripts security-check-prep.py, which writes it.
IGNORE_MARKER = ".reconcile-ignore"
# A marker shallower than this is not honoured: the artifacts root is 0, a category dir 1, a
# build's dir 2. security-check-prep.py writes its marker at audit-requests/<build>/prep, depth 3.
MIN_MARKER_DEPTH = 3
# The state holds only `path:inode` keys; anything larger is not ours. Read as unreadable.
MARKERS_MAX_BYTES = 4 << 20
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


def _read_regular(path: Path, cap: int) -> bytes:
    """Read a regular file of at most ``cap`` bytes. A FIFO, device or link (to anything) is
    refused before it is opened, so it can neither block the run nor feed it without end."""
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise OSError(f"not a regular file: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise OSError(f"replaced while opening: {path}")
        chunks, size = [], 0
        while size <= cap:
            chunk = os.read(fd, min(1 << 16, cap + 1 - size))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
        raise OSError(f"over {cap} bytes: {path}")
    finally:
        os.close(fd)


def load_markers() -> set[str]:
    """Identities of markers already announced. Anything unreadable reads as none, and every
    marker is announced again; a state file that exists but is refused says so on stderr, so a
    planted FIFO or link leaves a trace beside the re-announcements."""
    try:
        data = json.loads(_read_regular(MARKERS_FILE, MARKERS_MAX_BYTES))
        if not isinstance(data, list):
            raise ValueError("not a JSON list")
        return {k for k in data if isinstance(k, str)}
    except FileNotFoundError:
        return set()
    except Exception as e:
        print(
            f"reconcile: markers state refused ({type(e).__name__}); re-announcing every marker",
            file=sys.stderr,
        )
        return set()


def save_markers(keys: set[str]) -> None:
    tmp = MARKERS_FILE.with_name(MARKERS_FILE.name + ".tmp")
    tmp.unlink(missing_ok=True)  # a stale one, or a planted link: never written through
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(sorted(keys)))
    os.replace(tmp, MARKERS_FILE)


def _display(p: Path) -> str:
    """``p`` as a str the log can encode: undecodable bytes become ``\\xNN``. A literal backslash
    is doubled first, so two different names can never display (and dedup) as the same str."""
    return os.fsencode(p).replace(b"\\", b"\\\\").decode("utf-8", "backslashreplace")


FILE, TREE, NOT_HONOURED = "file", "tree", "not-honoured"


def artifact_files(errors: list[OSError]):
    """(path, kind) for every item to log: FILE for each file outside an ignored tree, TREE for
    the marker of each ignored tree, NOT_HONOURED for a marker too shallow to honour (its tree
    is scanned as usual). Directory links are not followed. Walk errors are appended to
    ``errors``, never swallowed."""
    for dirpath, dirnames, filenames in os.walk(ARTIFACTS_DIR, onerror=errors.append):
        here = Path(dirpath)
        if IGNORE_MARKER in filenames:
            if len(here.relative_to(ARTIFACTS_DIR).parts) >= MIN_MARKER_DEPTH:
                dirnames[:] = []
                yield here / IGNORE_MARKER, TREE
                continue
            yield here / IGNORE_MARKER, NOT_HONOURED
        dirnames.sort()
        for name in sorted(filenames):
            if name != IGNORE_MARKER:
                yield here / name, FILE


def main() -> None:
    cursor_mtime = CURSOR_FILE.stat().st_mtime if CURSOR_FILE.exists() else 0
    known = known_artifact_paths()
    found = 0

    errors: list[OSError] = []
    item_errors: list[str] = []
    announced = load_markers()
    seen: set[str] = set()
    for f, kind in artifact_files(errors):
        try:
            found += _reconcile_one(f, kind, cursor_mtime, known, announced, seen)
        except FileNotFoundError:
            if kind == TREE:
                # its tree was already pruned from this walk; holding the cursor makes the
                # next run walk it, so files beside a removed marker are not lost
                item_errors.append(f"{os.fsencode(f)!r}: marker vanished mid-scan")
            # a file removed between the walk and the stat: nothing left to log
        except Exception as e:  # one bad item must not stop the scan
            item_errors.append(f"{os.fsencode(f)!r}: {type(e).__name__}")

    if found:
        print(f"reconcile: logged {found} untracked artifact(s)")
    for e in errors:
        print(f"reconcile: could not read {e.filename}: {e.strerror}", file=sys.stderr)
    for line in item_errors:
        print(f"reconcile: could not log {line}", file=sys.stderr)
    # markers in unread dirs weren't seen; keep their keys rather than re-announce them later.
    # A failed save is one more error line, not a traceback that hides the ones above.
    try:
        save_markers(seen | announced if errors or item_errors else seen)
    except OSError as e:
        print(f"reconcile: could not save markers state: {type(e).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
    if errors or item_errors:
        # cursor not moved: the unread or unlogged items are retried next run
        raise SystemExit(1)
    CURSOR_FILE.touch()


def _reconcile_one(
    f: Path,
    kind: str,
    cursor_mtime: float,
    known: set[str],
    announced: set[str],
    seen: set[str],
) -> int:
    """Log one artifact (or one marker). Returns 1 if an event was appended, else 0."""
    path_str = _display(f)
    if kind == TREE:
        # identity, not mtime: a backdated or renamed-in marker must still be announced.
        # lstat: a marker that is a dangling link still hides its tree, so it counts too.
        key = f"{path_str}:{f.lstat().st_ino}"
        if key in announced:
            seen.add(key)
            return 0
    elif kind == NOT_HONOURED:
        f.lstat()  # gone since the walk: FileNotFoundError, nothing to log
        if path_str in known:  # once a day, whatever its mtime, for as long as it stays
            return 0
    elif not f.is_file() or f.stat().st_mtime <= cursor_mtime or path_str in known:
        return 0

    rel = _display(f.parent.relative_to(ARTIFACTS_DIR))
    event = build_event(
        event_type="artifact.untracked",
        source="reconciliation",
        summary={
            TREE: f"Ignored tree: {rel}",
            NOT_HONOURED: f"Marker not honoured: {rel}",
        }.get(kind, f"Untracked artifact: {_display(Path(f.name))}"),
        scope="cross-agent",
        artifact_path=path_str,
        hostname=HOSTNAME,
    )
    append_event(event, "cross-agent", LOGS_DIR)
    if kind == TREE:
        seen.add(key)  # only once its event is on the log: a failed append re-announces next run
    return 1


if __name__ == "__main__":
    main()
