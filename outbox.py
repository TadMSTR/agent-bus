"""
outbox.py — the producer outbox: one JSON file per event, drained into the chained log.

Some producers cannot reach the bus log. task-queue-mcp is a read-only container whose
only mount is the queue directory; scoped-mcp is a public package that should not take
an agent-bus dependency; ``coderabbit-review-assert.py`` is a stdlib script. Each of
them writes one small file per event into a directory it can already write. The
process that holds the drain lock validates every file, appends it through
``event_log.append_event`` (chained, flocked) and deletes it. One writer of the chained
log stays one writer.

THE FILE CONTRACT
-----------------
This is the contract every producer implements. It is short on purpose, because three
producers implement it independently; the validator below is the gate that keeps them
honest, and each producer's tests run its output through ``validate_event``.

- Name: ``<YYYYmmddTHHMMSS.ffffff>-<uuid4>.json`` (UTC). Write it as ``.tmp-<anything>``
  in the same directory, then ``rename`` it into place. Dot-files are never read.
- Body: a JSON object with exactly these keys:
  ``id`` (the uuid in the name; it becomes the bus event id and is the dedupe key),
  ``ts`` (ISO 8601 with a UTC offset: when the state change happened),
  ``event`` (a declared cross-agent type), ``source``, ``target`` (string or null),
  ``summary`` (string, at most 300 characters) and ``metadata`` (an object, at most
  4 KiB encoded).
- The whole file is at most 8 KiB.
- No ``scope``, ``prev_hash``, ``hostname`` or ``sig``. agent-bus assigns the first
  three, and an outbox event is not signed.

WHAT THE DRAINER REFUSES
------------------------
A refused file is moved to ``<root>/.rejected/`` beside a ``.reason`` file and logged.
It is never silently dropped. Each class has a fixed reason string (``REASONS``).

- not a regular file (a symlink is not followed: it is moved, not read)
- owned by a uid other than the drainer's
- a name that does not match the contract
- larger than 8 KiB, not JSON, not an object, or with missing or extra keys
- an ``id`` that is not the uuid in the file name
- an event type outside ``CROSS_AGENT_EVENTS``. This holds REGARDLESS of
  ``AGENT_BUS_STRICT_VOCAB``: a new path starts strict, and nothing written through it
  predates the vocabulary.
- a ``source`` that is not on that root's allowlist
- an over-long summary or metadata

TRUST
-----
Outbox events are display-only. Any process running as the bus's uid can write an
outbox file, exactly as it can append to the JSONL itself. The owner check and the
per-root source allowlist stop a *confused* producer from writing as another one; they
do not authenticate anybody. Nothing may approve, grant or launch from a bus event.

This module is stdlib-only, like the client path, so a producer's test suite can import
``validate_event`` without installing the server.
"""

from __future__ import annotations

import contextlib
import heapq
import json
import logging
import os
import re
import stat
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from event_vocab import CROSS_AGENT_EVENTS

_log = logging.getLogger("agent-bus.outbox")

MAX_FILE_BYTES = 8192
MAX_SUMMARY_CHARS = 300
MAX_METADATA_BYTES = 4096
MAX_TARGET_CHARS = 128
FIELDS = frozenset({"id", "ts", "event", "source", "target", "summary", "metadata"})

NAME_RE = re.compile(
    r"^(\d{8}T\d{6}\.\d{6})-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\.json$"
)
SOURCE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
REJECTED_DIR = ".rejected"

# One fixed string per rejection class, so a reader can count by class without parsing
# prose, and a test can assert the class rather than a message.
REASONS = (
    "not_regular_file",
    "foreign_owner",
    "bad_name",
    "too_large",
    "unreadable",
    "not_json",
    "not_object",
    "bad_fields",
    "id_mismatch",
    "bad_ts",
    "unknown_event",
    "source_not_allowed",
    "bad_target",
    "bad_summary",
    "bad_metadata",
)


class _AppendFailed(Exception):
    """The chained log refused an append: stop the cycle, delete nothing."""


class OutboxRejection(ValueError):
    """An outbox file the drainer will not ingest. ``reason`` is one of REASONS."""

    def __init__(self, reason: str, detail: str = ""):
        assert reason in REASONS, reason
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class OutboxRoot:
    path: Path
    sources: frozenset[str]


def parse_outboxes(spec: str | None) -> list[OutboxRoot]:
    """Parse ``AGENT_BUS_OUTBOXES``.

    Shape: entries separated by ``;``, each ``<absolute path>=<source>[,<source>...]``::

        /srv/agents/task-queue/.outbox=task-queue-mcp;
        /srv/agents/comms/outbox/scoped-mcp=scoped-mcp

    ``;`` rather than ``:`` so the list cannot be mistaken for a PATH-style variable,
    which has no place to put the allowlist. A root with no source would accept nothing
    and is refused, as are a relative path, a path given twice, and a source name that is
    not lower-case kebab. A malformed spec raises: the drainer is new, and a typo in it
    must stop the service from claiming to ingest rather than ingest nothing quietly.
    """
    roots: list[OutboxRoot] = []
    seen: set[Path] = set()
    for raw in (spec or "").split(";"):
        entry = raw.strip()
        if not entry:
            continue
        path_s, sep, sources_s = entry.partition("=")
        if not sep:
            raise ValueError(f"AGENT_BUS_OUTBOXES entry {entry!r} has no '=<sources>'")
        path = Path(path_s.strip()).expanduser()
        if not path.is_absolute():
            raise ValueError(f"AGENT_BUS_OUTBOXES path {path_s!r} is not absolute")
        sources = frozenset(s.strip() for s in sources_s.split(",") if s.strip())
        if not sources:
            raise ValueError(f"AGENT_BUS_OUTBOXES entry {entry!r} allows no source")
        bad = sorted(s for s in sources if not SOURCE_RE.match(s))
        if bad:
            raise ValueError(f"AGENT_BUS_OUTBOXES entry {entry!r}: bad source name(s) {bad}")
        if path in seen:
            raise ValueError(f"AGENT_BUS_OUTBOXES lists {path} twice")
        seen.add(path)
        roots.append(OutboxRoot(path=path, sources=sources))
    return roots


def parse_ts(ts: object) -> datetime | None:
    """Parse a contract ``ts``, or None. Timezone-aware results only.

    A trailing ``Z`` is normalised to ``+00:00`` first: ``datetime.fromisoformat``
    accepts it from Python 3.11 but not on 3.10, which this package still supports, and
    a contract whose answer depends on the drainer's Python version is two contracts.
    """
    if not isinstance(ts, str):
        return None
    text = ts[:-1] + "+00:00" if ts.endswith(("Z", "z")) else ts
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def validate_event(obj: object, name: str, allowed_sources: frozenset[str] | set[str]) -> dict:
    """Validate a decoded outbox body against the contract. Returns it, or raises.

    ``name`` is the file name, because the id must match it. Producers call this from
    their tests with a name their writer produced.
    """
    m = NAME_RE.match(name)
    if not m:
        raise OutboxRejection("bad_name", name[:80])
    if not isinstance(obj, dict):
        raise OutboxRejection("not_object", type(obj).__name__)
    keys = set(obj)
    if keys != FIELDS:
        missing, extra = sorted(FIELDS - keys), sorted(keys - FIELDS)
        raise OutboxRejection("bad_fields", f"missing={missing} extra={extra}")
    if obj["id"] != m.group(2):
        raise OutboxRejection("id_mismatch")
    if parse_ts(obj["ts"]) is None:
        raise OutboxRejection("bad_ts")
    event = obj["event"]
    if not isinstance(event, str) or event not in CROSS_AGENT_EVENTS:
        raise OutboxRejection("unknown_event", str(event)[:80])
    source = obj["source"]
    if not isinstance(source, str) or source not in allowed_sources:
        raise OutboxRejection("source_not_allowed", str(source)[:80])
    target = obj["target"]
    if target is not None and (not isinstance(target, str) or len(target) > MAX_TARGET_CHARS):
        raise OutboxRejection("bad_target")
    summary = obj["summary"]
    if not isinstance(summary, str) or len(summary) > MAX_SUMMARY_CHARS:
        raise OutboxRejection("bad_summary")
    metadata = obj["metadata"]
    if not isinstance(metadata, dict):
        raise OutboxRejection("bad_metadata", "not an object")
    if _depth(metadata) > MAX_METADATA_DEPTH:
        raise OutboxRejection("bad_metadata", f"nested deeper than {MAX_METADATA_DEPTH}")
    if len(json.dumps(metadata, ensure_ascii=False).encode()) > MAX_METADATA_BYTES:
        raise OutboxRejection("bad_metadata", "over 4 KiB")
    return obj


MAX_METADATA_DEPTH = 16
# Bounds on what one cycle (and one get_status call) will look at, whatever a producer
# puts in its root (audit F-03). The drain itself is bounded by max_per_cycle.
SCAN_LIMIT = 5000
REJECTED_LIMIT = 1000


def _reject_constant(name: str) -> None:
    # json.loads accepts NaN / Infinity by default; the chained log would then hold
    # non-standard JSON (audit F-04).
    raise ValueError(f"non-finite constant {name}")


def _depth(obj: object) -> int:
    """Nesting depth, iteratively, so a deep document cannot exhaust the stack here."""
    deepest, stack = 0, [(obj, 1)]
    while stack:
        cur, d = stack.pop()
        if isinstance(cur, dict | list):
            deepest = max(deepest, d)
            if d > MAX_METADATA_DEPTH:
                return d
            stack.extend((v, d + 1) for v in (cur.values() if isinstance(cur, dict) else cur))
    return deepest


def open_root(path: Path) -> int | None:
    """Pin an outbox root: a directory, not a symlink, owned by this uid. None otherwise.

    Every later operation on the root is relative to this descriptor (audit F-01, F-02).
    A path that is re-resolved for each operation can be swapped for a symlink between
    the check and the use, by any producer that can write the root's parent: the
    task-queue-mcp container can write the parent of its own outbox.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        _log.error("outbox root %r cannot be opened as a real directory: %s", str(path), exc)
        return None
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        os.close(fd)
        _log.error("outbox root %r is not a directory owned by this uid", str(path))
        return None
    return fd


def list_pending(dir_fd: int, limit: int | None = None) -> tuple[list[str], bool]:
    """The ``limit`` oldest pending names (name = time order), and whether there were more.

    Dot-entries are not pending. The scan reads at most SCAN_LIMIT entries, so a flooded
    root costs a bounded amount per cycle (audit F-03), and keeps the ``limit`` smallest
    names with a heap. ``scandir`` returns directory order, not name order: taking the
    first ``limit`` entries it returns and sorting them would drain an arbitrary slice of
    a large backlog, newest first as often as not (CodeRabbit CR-03 on #13).
    """
    limit = SCAN_LIMIT if limit is None else limit
    read = 0
    more = False
    names: list[str] = []
    with os.scandir(dir_fd) as it:
        for entry in it:
            if entry.name.startswith("."):
                continue
            if read >= SCAN_LIMIT:
                more = True
                break
            read += 1
            names.append(entry.name)
    if len(names) > limit:
        more = True
        names = heapq.nsmallest(limit, names)
    else:
        names.sort()
    return names, more


def read_and_validate_at(
    dir_fd: int, name: str, allowed_sources: frozenset[str] | set[str]
) -> dict:
    """Read one outbox entry relative to its pinned root, without following a symlink."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        raise OutboxRejection("unreadable", exc.__class__.__name__) from None
    if not stat.S_ISREG(st.st_mode):
        raise OutboxRejection("not_regular_file")
    if st.st_uid != os.getuid():
        raise OutboxRejection("foreign_owner", f"uid {st.st_uid}")
    if not NAME_RE.match(name):
        raise OutboxRejection("bad_name", name[:80])
    if st.st_size > MAX_FILE_BYTES:
        raise OutboxRejection("too_large", f"{st.st_size} bytes")
    try:
        # The stat above can be raced: the name can be swapped between it and this
        # open. O_NOFOLLOW refuses a swapped-in symlink. O_NONBLOCK stops a swapped-in
        # FIFO from blocking the open until a writer appears, which would hang the
        # drain thread and with it every root. The type and owner checks are then
        # repeated on the descriptor itself, which is what is actually read.
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as exc:
        raise OutboxRejection("unreadable", exc.__class__.__name__) from None
    with os.fdopen(fd, "rb") as f:
        fst = os.fstat(f.fileno())
        if not stat.S_ISREG(fst.st_mode):
            raise OutboxRejection("not_regular_file", "changed after stat")
        if fst.st_uid != os.getuid():
            raise OutboxRejection("foreign_owner", f"uid {fst.st_uid}, changed after stat")
        try:
            raw = f.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            raise OutboxRejection("unreadable", exc.__class__.__name__) from None
    if len(raw) > MAX_FILE_BYTES:
        raise OutboxRejection("too_large", "grew after stat")
    try:
        obj = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise OutboxRejection("not_json") from None
    return validate_event(obj, name, allowed_sources)


def read_and_validate(path: Path, allowed_sources: frozenset[str] | set[str]) -> dict:
    """Path form of ``read_and_validate_at``, for producers' tests and one-off checks."""
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise OutboxRejection("unreadable", exc.__class__.__name__) from None
    try:
        return read_and_validate_at(dir_fd, path.name, allowed_sources)
    finally:
        os.close(dir_fd)


def write_event(
    outbox: Path,
    event: str,
    source: str,
    summary: str,
    target: str | None = None,
    metadata: dict | None = None,
) -> Path:
    """Reference writer for the contract. Used by this repo's tests.

    Producers do not import this: each carries its own ~30-line writer, and its tests
    prove the output passes ``validate_event``.
    """
    outbox.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    eid = str(uuid.uuid4())
    body = {
        "id": eid,
        "ts": now.isoformat(),
        "event": event,
        "source": source,
        "target": target,
        "summary": summary[:MAX_SUMMARY_CHARS],
        "metadata": metadata or {},
    }
    final = outbox / f"{now.strftime('%Y%m%dT%H%M%S.%f')}-{eid}.json"
    tmp = outbox / f".tmp-{eid}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(body, f, ensure_ascii=False)
    os.rename(tmp, final)
    return final


def pending_files(root: Path) -> list[Path]:
    """Entries waiting in a root, in name order. Empty if the root is absent or unsafe."""
    fd = open_root(root)
    if fd is None:
        return []
    try:
        names, _ = list_pending(fd)
        return [root / n for n in names]
    finally:
        os.close(fd)


def _count_rejected(dir_fd: int, limit: int | None = None) -> tuple[int, bool]:
    """Quarantined files under ``.rejected`` (not their ``.reason`` files), bounded."""
    limit = SCAN_LIMIT if limit is None else limit
    try:
        rfd = os.open(REJECTED_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        return 0, False
    try:
        n = 0
        with os.scandir(rfd) as it:
            for entry in it:
                if entry.name.endswith(".reason"):
                    continue
                n += 1
                if n >= limit:
                    return n, True
        return n, False
    finally:
        os.close(rfd)


def root_status(root: OutboxRoot, now: datetime | None = None) -> dict:
    """Live counts for one root, read from disk so any process can report them.

    Bounded (audit F-03): counts stop at SCAN_LIMIT and say so with ``*_capped``, because
    every stdio child answers get_status and a flooded root must not make that expensive.
    """
    now = now or datetime.now(timezone.utc)
    out: dict = {"path": str(root.path), "sources": sorted(root.sources)}
    fd = open_root(root.path)
    if fd is None:
        out.update(exists=root.path.exists(), pending=0, oldest_pending_age_s=None, rejected=0)
        return out
    try:
        names, capped = list_pending(fd)
        rejected, rejected_capped = _count_rejected(fd)
    finally:
        os.close(fd)
    oldest = None
    if names:
        m = NAME_RE.match(names[0])
        if m:
            with_tz = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S.%f").replace(tzinfo=timezone.utc)
            oldest = round((now - with_tz).total_seconds(), 1)
    out.update(
        exists=True,
        pending=len(names),
        pending_capped=capped,
        oldest_pending_age_s=oldest,
        rejected=rejected,
        rejected_capped=rejected_capped,
    )
    return out


@dataclass
class Drainer:
    """Drains configured roots into the chained log. One instance per draining process.

    ``append`` and ``fanout`` are injected so the server supplies its own append path
    and NATS/ntfy/webhook fan-out, and tests can supply recorders.
    """

    roots: list[OutboxRoot]
    logs_dir: Path
    hostname: str
    append: object  # Callable[[dict], None]
    fanout: object  # Callable[[dict], None]
    max_per_cycle: int = 500
    seen_limit: int = 20000
    _seen: OrderedDict[str, None] = field(default_factory=OrderedDict)
    seeded: bool = False
    drained: int = 0
    duplicates: int = 0
    rejected: int = 0
    dropped: int = 0
    dropped_by_root: dict[str, int] = field(default_factory=dict)
    # (root path, entry name) -> cycles left to skip it. An entry the drainer could not
    # move or delete would otherwise come back first every cycle and use the budget
    # again (round 2 R2-02). Bounded, and it expires, so a fixed entry is retried.
    stuck_skip_cycles: int = 10
    stuck_limit: int = 10000
    _stuck: OrderedDict[tuple[str, str], int] = field(default_factory=OrderedDict)
    # (root path, entry name) -> event id, for files appended but not deleted. Not
    # subject to _seen's eviction; dropped when the file is gone (round 3 R3-01).
    _appended_stuck: OrderedDict[tuple[str, str], str] = field(default_factory=OrderedDict)
    _cycle: int = 0
    last_cycle: str | None = None

    def seed(self) -> None:
        """Load ids already in the newest cross-agent logs.

        This is what makes a crash between append and unlink harmless: the outbox id IS
        the bus event id, so after a restart the file's id is found in the log and the
        file is deleted without a second append. Two days covers a crash at midnight.
        """
        files = sorted(self.logs_dir.glob("*-cross-agent.jsonl"))[-2:]
        for path in files:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        try:
                            eid = json.loads(line).get("id")
                        except (ValueError, AttributeError):
                            continue
                        if isinstance(eid, str):
                            self._remember(eid)
            except OSError as exc:
                _log.warning("outbox seed could not read %s: %s", path, exc)
        self.seeded = True

    def _remember(self, eid: str) -> None:
        self._seen[eid] = None
        self._seen.move_to_end(eid)
        while len(self._seen) > self.seen_limit:
            self._seen.popitem(last=False)

    def _drop(self, dir_fd: int, root: OutboxRoot, name: str, why: str) -> None:
        """Remove an entry that cannot be quarantined safely. Logged, never silent.

        unlink() of a name relative to the pinned root removes that entry itself, a
        symlink included, and never what a symlink points at.
        """
        self.dropped += 1
        key = str(root.path)
        self.dropped_by_root[key] = self.dropped_by_root.get(key, 0) + 1
        try:
            os.unlink(name, dir_fd=dir_fd)
        except (IsADirectoryError, PermissionError) as left:
            # A directory is never removed recursively from here, and a root the producer
            # made read-only refuses the unlink. Either way it is left for a human; the
            # stuck-entry skip keeps it from using the budget every cycle.
            _log.error(
                "outbox_left_in_place root=%r file=%r why=%s (%s)",
                str(root.path),
                name,
                why,
                type(left).__name__,
            )
            return
        except OSError as exc:
            _log.error("outbox could not drop %r in %r: %s", name, str(root.path), exc)
            return
        _log.error("outbox_dropped root=%r file=%r why=%s", str(root.path), name, why)

    def _quarantine_dir(self, dir_fd: int) -> int | None:
        """Open ``.rejected`` under the pinned root, creating it if absent.

        It must be a real directory owned by this uid. A producer can create anything
        in its own root, including a ``.rejected`` symlink pointing anywhere the drainer
        can write (audit F-01); that is refused, not followed.
        """
        # Any mkdir failure (EACCES on a root the producer made read-only, ENOSPC,
        # EROFS, EDQUOT), not only EEXIST, means "no safe quarantine here": the open below
        # then fails or the checks refuse it, and the caller drops instead (round 2 R2-01).
        with contextlib.suppress(OSError):
            os.mkdir(REJECTED_DIR, 0o700, dir_fd=dir_fd)
        try:
            rfd = os.open(REJECTED_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError:
            return None
        st = os.fstat(rfd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            os.close(rfd)
            return None
        return rfd

    def _reject(
        self, dir_fd: int, root: OutboxRoot, name: str, exc: OutboxRejection, cycle: dict
    ) -> None:
        """Quarantine one entry. Never raises (round 2 R2-01): a failure here is logged,
        and the entry is left for the stuck-entry skip rather than aborting the cycle.

        ``cycle`` carries this root's quarantine count for the cycle, read once and kept
        locally (R2-03), instead of rescanning ``.rejected`` on every rejection.
        """
        try:
            self._reject_unguarded(dir_fd, root, name, exc, cycle)
        except Exception as unexpected:
            _log.error(
                "outbox quarantine failed for %r in %r: %s: %s",
                name,
                str(root.path),
                type(unexpected).__name__,
                unexpected,
            )

    def _reject_unguarded(
        self, dir_fd: int, root: OutboxRoot, name: str, exc: OutboxRejection, cycle: dict
    ) -> None:
        self.rejected += 1
        rfd = self._quarantine_dir(dir_fd)
        if rfd is None:
            self._drop(dir_fd, root, name, f"{exc.reason}; .rejected is not a safe directory")
            return
        try:
            if "rejected" not in cycle:
                cycle["rejected"], _ = _count_rejected(dir_fd, REJECTED_LIMIT)
            if cycle["rejected"] >= REJECTED_LIMIT:
                self._drop(
                    dir_fd, root, name, f"{exc.reason}; quarantine full ({cycle['rejected']})"
                )
                return
            # rename() acts on the entry, not on what a symlink entry points at, and both
            # ends are relative to descriptors that were checked.
            os.rename(name, name, src_dir_fd=dir_fd, dst_dir_fd=rfd)
            reason_body = json.dumps(
                {
                    "reason": exc.reason,
                    "detail": exc.detail,
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            )
            reason_name = f"{name}.reason"
            # O_EXCL|O_NOFOLLOW: never truncate or write through an existing entry,
            # symlink or not. A stale one is removed (the entry, not its target) first.
            with contextlib.suppress(FileNotFoundError):
                os.unlink(reason_name, dir_fd=rfd)
            fd = os.open(
                reason_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=rfd,
            )
            with os.fdopen(fd, "w") as f:
                f.write(reason_body + "\n")
            cycle["rejected"] += 1
        except OSError as move_exc:
            _log.error("outbox could not quarantine %r (%s): %s", name, exc.reason, move_exc)
            return
        finally:
            os.close(rfd)
        # %r, not %s: the file name and the detail are producer-written, and a newline in
        # either must not forge a second log line.
        _log.warning(
            "outbox_rejected root=%r file=%r reason=%s detail=%r",
            str(root.path),
            name,
            exc.reason,
            exc.detail,
        )

    def drain_once(self) -> int:
        """Ingest up to ``max_per_cycle`` files across all roots. Returns events appended.

        The budget is shared fairly (round 2 R2-02): each root gets an equal share, and
        the root that goes first rotates every cycle, so one producer's root cannot use
        the whole cycle and starve the roots configured after it.
        """
        if not self.seeded:
            self.seed()
        appended = 0
        if not self.roots:
            self.last_cycle = datetime.now(timezone.utc).isoformat()
            return 0
        share = max(1, self.max_per_cycle // len(self.roots))
        start = self._cycle % len(self.roots)
        self._cycle += 1
        self._age_stuck()
        for root in self.roots[start:] + self.roots[:start]:
            dir_fd = open_root(root.path)
            if dir_fd is None:
                continue  # absent (status reports exists=False) or unsafe (logged)
            try:
                appended += self._drain_root(dir_fd, root, share)
            except _AppendFailed:
                break  # the log itself is unwritable: stop, keep every file for later
            finally:
                os.close(dir_fd)
        self.last_cycle = datetime.now(timezone.utc).isoformat()
        return appended

    def _age_stuck(self) -> None:
        for key in list(self._stuck):
            self._stuck[key] -= 1
            if self._stuck[key] <= 0:
                del self._stuck[key]

    def _still_there(self, dir_fd: int, root: OutboxRoot, name: str) -> bool:
        """Remember an entry that survived handling, so later cycles skip it for a while.

        Returns whether it is still there.
        """
        try:
            os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            return False
        key = (str(root.path), name)
        self._stuck[key] = self.stuck_skip_cycles
        self._stuck.move_to_end(key)
        while len(self._stuck) > self.stuck_limit:
            self._stuck.popitem(last=False)
        _log.error(
            "outbox_stuck root=%r file=%r: could not be moved or deleted; skipped for %d cycles",
            str(root.path),
            name,
            self.stuck_skip_cycles,
        )
        return True

    def _drain_root(self, dir_fd: int, root: OutboxRoot, budget: int) -> int:
        appended = 0
        cycle: dict = {}
        rkey = str(root.path)
        names, _ = list_pending(dir_fd, limit=max(budget, 1) * 4 + len(self._stuck))
        names = [n for n in names if (rkey, n) not in self._stuck]
        for name in names[:budget]:
            try:
                obj = read_and_validate_at(dir_fd, name, root.sources)
            except OutboxRejection as exc:
                self._reject(dir_fd, root, name, exc, cycle)
                self._still_there(dir_fd, root, name)
                continue
            except Exception as exc:  # any surprise is this file's problem (audit F-04)
                self._reject(
                    dir_fd, root, name, OutboxRejection("unreadable", type(exc).__name__), cycle
                )
                self._still_there(dir_fd, root, name)
                continue
            key = (rkey, name)
            if obj["id"] in self._seen or self._appended_stuck.get(key) == obj["id"]:
                self.duplicates += 1
                self._remember(obj["id"])
                # Logged, not silent (audit F-09): a duplicate is either a crash replay or
                # a producer reusing an id, and only the log can tell which.
                _log.warning(
                    "outbox_duplicate root=%r file=%r id=%s source=%r",
                    rkey,
                    name,
                    obj["id"],
                    obj["source"],
                )
                self._unlink(dir_fd, name)
                if not self._still_there(dir_fd, root, name):
                    self._appended_stuck.pop(key, None)
                continue
            event = {
                "id": obj["id"],
                # Normalised to the +00:00 form every other writer uses:
                # query_events filters `since` by string comparison.
                "ts": parse_ts(obj["ts"]).astimezone(timezone.utc).isoformat(),
                "event": obj["event"],
                "scope": "cross-agent",
                "source": obj["source"],
                "target": obj["target"],
                "artifact_path": None,
                "summary": obj["summary"],
                "hostname": self.hostname,
                "metadata": obj["metadata"],
                "ingest": "outbox",
                "ingested_at": datetime.now(timezone.utc).isoformat(),
            }
            # Append first, delete second. A crash in between leaves the file, and
            # seed() finds its id on restart. The reverse order would lose events.
            try:
                self.append(event)
            except Exception as exc:
                _log.error("outbox append failed, stopping this cycle: %s", exc)
                raise _AppendFailed from exc
            self._remember(obj["id"])
            self._unlink(dir_fd, name)
            if self._still_there(dir_fd, root, name):
                # Appended, but the file could not be deleted. Its id is pinned here, out of
                # _seen's FIFO eviction, for as long as the file stays (round 3 R3-01): a
                # later retry must read it as a duplicate however many events came between.
                self._appended_stuck[key] = obj["id"]
                while len(self._appended_stuck) > self.stuck_limit:
                    self._appended_stuck.popitem(last=False)
            appended += 1
            self.drained += 1
            try:
                self.fanout(event)
            except Exception as exc:  # fan-out never undoes an append
                _log.warning("outbox fanout failed for %s: %s", obj["id"], exc)
        return appended

    @staticmethod
    def _unlink(dir_fd: int, name: str) -> None:
        try:
            os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            _log.error("outbox could not delete ingested %r: %s", name, exc)

    def stats(self) -> dict:
        return {
            "drained": self.drained,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "dropped": self.dropped,
            "dropped_by_root": dict(self.dropped_by_root),
            "stuck": len(self._stuck),
            "last_cycle": self.last_cycle,
        }
