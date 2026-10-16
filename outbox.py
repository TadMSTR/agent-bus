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

        /home/ted/.claude/task-queue/.outbox=task-queue-mcp;
        /home/ted/.claude/comms/outbox/scoped-mcp=scoped-mcp

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
    if len(json.dumps(metadata, ensure_ascii=False).encode()) > MAX_METADATA_BYTES:
        raise OutboxRejection("bad_metadata", "over 4 KiB")
    return obj


def read_and_validate(path: Path, allowed_sources: frozenset[str] | set[str]) -> dict:
    """Read one outbox file without following a symlink, and validate it."""
    try:
        st = path.lstat()
    except OSError as exc:
        raise OutboxRejection("unreadable", exc.__class__.__name__) from None
    if not stat.S_ISREG(st.st_mode):
        raise OutboxRejection("not_regular_file")
    if st.st_uid != os.getuid():
        raise OutboxRejection("foreign_owner", f"uid {st.st_uid}")
    if not NAME_RE.match(path.name):
        raise OutboxRejection("bad_name", path.name[:80])
    if st.st_size > MAX_FILE_BYTES:
        raise OutboxRejection("too_large", f"{st.st_size} bytes")
    try:
        # The lstat above can be raced: the path can be swapped between it and this
        # open. O_NOFOLLOW refuses a swapped-in symlink. O_NONBLOCK stops a swapped-in
        # FIFO from blocking the open until a writer appears, which would hang the
        # drain thread and with it every root. The type and owner checks are then
        # repeated on the descriptor itself, which is what is actually read.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise OutboxRejection("unreadable", exc.__class__.__name__) from None
    with os.fdopen(fd, "rb") as f:
        fst = os.fstat(f.fileno())
        if not stat.S_ISREG(fst.st_mode):
            raise OutboxRejection("not_regular_file", "changed after lstat")
        if fst.st_uid != os.getuid():
            raise OutboxRejection("foreign_owner", f"uid {fst.st_uid}, changed after lstat")
        try:
            raw = f.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            raise OutboxRejection("unreadable", exc.__class__.__name__) from None
    if len(raw) > MAX_FILE_BYTES:
        raise OutboxRejection("too_large", "grew after stat")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise OutboxRejection("not_json") from None
    return validate_event(obj, path.name, allowed_sources)


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
    """Entries waiting in a root, in name (= time) order. Dot-entries are not pending."""
    try:
        return sorted(p for p in root.iterdir() if not p.name.startswith("."))
    except OSError:
        return []


def root_status(root: OutboxRoot, now: datetime | None = None) -> dict:
    """Live counts for one root, read from disk so any process can report them."""
    now = now or datetime.now(timezone.utc)
    out: dict = {"path": str(root.path), "sources": sorted(root.sources)}
    if not root.path.is_dir():
        out.update(exists=False, pending=0, oldest_pending_age_s=None, rejected=0)
        return out
    pending = pending_files(root.path)
    oldest = None
    if pending:
        m = NAME_RE.match(pending[0].name)
        if m:
            with_tz = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S.%f").replace(tzinfo=timezone.utc)
            oldest = round((now - with_tz).total_seconds(), 1)
    rej = root.path / REJECTED_DIR
    try:
        rejected = sum(1 for p in rej.iterdir() if not p.name.endswith(".reason"))
    except OSError:
        rejected = 0
    out.update(exists=True, pending=len(pending), oldest_pending_age_s=oldest, rejected=rejected)
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

    def _reject(self, root: OutboxRoot, path: Path, exc: OutboxRejection) -> None:
        self.rejected += 1
        rej = root.path / REJECTED_DIR
        try:
            rej.mkdir(mode=0o700, exist_ok=True)
            os.rename(path, rej / path.name)
            (rej / f"{path.name}.reason").write_text(
                json.dumps(
                    {
                        "reason": exc.reason,
                        "detail": exc.detail,
                        "at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                + "\n"
            )
        except OSError as move_exc:
            _log.error("outbox could not quarantine %s (%s): %s", path, exc.reason, move_exc)
            return
        _log.warning("outbox_rejected root=%s file=%s reason=%s", root.path, path.name, exc)

    def drain_once(self) -> int:
        """Ingest up to ``max_per_cycle`` files across all roots. Returns events appended."""
        if not self.seeded:
            self.seed()
        appended = 0
        budget = self.max_per_cycle
        for root in self.roots:
            if budget <= 0:
                break
            try:
                rst = root.path.lstat()
            except OSError:
                continue  # not created yet; status reports exists=False
            if not stat.S_ISDIR(rst.st_mode) or rst.st_uid != os.getuid():
                _log.error("outbox root %s is not a directory owned by this uid", root.path)
                continue
            for path in pending_files(root.path)[:budget]:
                budget -= 1
                try:
                    obj = read_and_validate(path, root.sources)
                except OutboxRejection as exc:
                    self._reject(root, path, exc)
                    continue
                if obj["id"] in self._seen:
                    self.duplicates += 1
                    self._unlink(path)
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
                self.append(event)
                self._remember(obj["id"])
                self._unlink(path)
                appended += 1
                self.drained += 1
                try:
                    self.fanout(event)
                except Exception as exc:  # fan-out never undoes an append
                    _log.warning("outbox fanout failed for %s: %s", obj["id"], exc)
        self.last_cycle = datetime.now(timezone.utc).isoformat()
        return appended

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            _log.error("outbox could not delete ingested %s: %s", path, exc)

    def stats(self) -> dict:
        return {
            "drained": self.drained,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "last_cycle": self.last_cycle,
        }
