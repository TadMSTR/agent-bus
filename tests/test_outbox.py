"""The producer outbox: file contract, drainer, dedupe, lock and status.

Producers that cannot reach the chained log (task-queue-mcp in its read-only container,
scoped-mcp, coderabbit-review-assert.py) write one file per event; the drainer is the
single gate between those files and the log. Every rejection class has its own test
because a class with no test is a class that will quietly start passing.
"""

import asyncio
import fcntl
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

import outbox as ob
import server as ab

REPO_ROOT = Path(__file__).parent.parent
SRC = "task-queue-mcp"


# ── helpers ─────────────────────────────────────────────────────────────────────


def _name(eid: str | None = None) -> tuple[str, str]:
    eid = eid or str(uuid.uuid4())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")
    return f"{stamp}-{eid}.json", eid


def _body(eid: str, **over) -> dict:
    body = {
        "id": eid,
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "task.submitted",
        "source": SRC,
        "target": "developer",
        "summary": "a task",
        "metadata": {"task_id": "abc", "schema": 1},
    }
    body.update(over)
    return body


def _put(root: Path, body=None, raw: bytes | None = None, name: str | None = None) -> Path:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if name is None:
        name, eid = _name()
    else:
        eid = ob.NAME_RE.match(name).group(2) if ob.NAME_RE.match(name) else str(uuid.uuid4())
    if raw is None:
        raw = json.dumps(_body(eid) if body is None else body(eid)).encode()
    p = root / name
    p.write_bytes(raw)
    return p


class Recorder:
    def __init__(self, logs_dir: Path):
        self.logs_dir = logs_dir
        self.appended: list[dict] = []
        self.fanned: list[dict] = []

    def append(self, e):
        self.appended.append(e)
        ab.event_log.append_event(e, "cross-agent", self.logs_dir)

    def fanout(self, e):
        self.fanned.append(e)


def _drainer(root: Path, logs: Path, sources=frozenset({SRC})):
    rec = Recorder(logs)
    d = ob.Drainer(
        roots=[ob.OutboxRoot(root, frozenset(sources))],
        logs_dir=logs,
        hostname="testhost",
        append=rec.append,
        fanout=rec.fanout,
    )
    return d, rec


def _logged(logs: Path) -> list[dict]:
    out = []
    for p in sorted(logs.glob("*-cross-agent.jsonl")):
        out += [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    return out


# ── AGENT_BUS_OUTBOXES ──────────────────────────────────────────────────────────


def test_parse_outboxes_reads_roots_and_allowlists():
    roots = ob.parse_outboxes(" /a/b=task-queue-mcp ; /c=scoped-mcp,coderabbit-review-assert ;")
    assert roots == [
        ob.OutboxRoot(Path("/a/b"), frozenset({"task-queue-mcp"})),
        ob.OutboxRoot(Path("/c"), frozenset({"scoped-mcp", "coderabbit-review-assert"})),
    ]


@pytest.mark.parametrize("spec", [None, "", " ; "])
def test_parse_outboxes_empty_is_no_roots(spec):
    assert ob.parse_outboxes(spec) == []


@pytest.mark.parametrize(
    "spec",
    [
        "/a/b",  # no allowlist
        "relative=task-queue-mcp",
        "/a=",  # empty allowlist
        "/a=Task_Queue",  # not kebab
        "/a=x;/a=y",  # duplicate root
    ],
)
def test_parse_outboxes_refuses_malformed(spec):
    with pytest.raises(ValueError):
        ob.parse_outboxes(spec)


def test_a_malformed_spec_disables_draining_but_not_the_server(tmp_path):
    """A typo must not take log_event down for every agent, and must not be silent."""
    env = dict(os.environ, AGENT_BUS_OUTBOXES="nope", AGENT_BUS_COMMS_DIR=str(tmp_path))
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, server; s = server.get_status()['outboxes'];"
            "print(json.dumps([server.OUTBOXES == [], s['config_error'] is not None]))",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [True, True]


# ── the contract ────────────────────────────────────────────────────────────────


def test_reference_writer_output_passes_the_validator(tmp_path):
    """The pattern each producer copies: write with your writer, validate with ours."""
    p = ob.write_event(tmp_path / "ob", "task.submitted", SRC, "x" * 400, metadata={"a": 1})
    obj = ob.read_and_validate(p, {SRC})
    assert obj["summary"] == "x" * 300
    assert not list((tmp_path / "ob").glob(".tmp-*"))
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_the_validator_module_needs_no_server_dependency():
    """Producers import validate_event in tests; it must not drag in fastmcp et al."""
    child = (
        "import sys\n"
        "class B:\n"
        "    def find_spec(self, n, p=None, t=None):\n"
        "        if n.split('.')[0] in ('fastmcp','cryptography','nats'): raise ImportError(n)\n"
        "sys.meta_path.insert(0, B())\n"
        "import outbox\n"
    )
    subprocess.run([sys.executable, "-c", child], cwd=REPO_ROOT, check=True)


def _reject_case(tmp_path, monkeypatch, case):
    root = tmp_path / "ob"
    name, _eid = _name()
    if case == "not_regular_file":
        target = _put(tmp_path / "elsewhere")
        root.mkdir(mode=0o700)
        (root / name).symlink_to(target)
        return root / name
    if case == "foreign_owner":
        p = _put(root, name=name)
        real = os.getuid()
        monkeypatch.setattr(ob.os, "getuid", lambda: real + 1)
        return p
    if case == "bad_name":
        return _put(root, name="hello.json")
    if case == "too_large":
        return _put(root, name=name, raw=b"{" + b" " * ob.MAX_FILE_BYTES + b"}")
    if case == "not_json":
        return _put(root, name=name, raw=b"{nope")
    if case == "not_object":
        return _put(root, name=name, raw=b"[1, 2]")
    if case == "bad_fields":
        return _put(root, name=name, body=lambda e: {**_body(e), "prev_hash": "x"})
    if case == "id_mismatch":
        return _put(root, name=name, body=lambda e: _body(str(uuid.uuid4())))
    if case == "bad_ts":
        return _put(root, name=name, body=lambda e: _body(e, ts="2026-10-16T00:00:00"))
    if case == "unknown_event":
        return _put(root, name=name, body=lambda e: _body(e, event="build.pre_audit_submitted"))
    if case == "source_not_allowed":
        return _put(root, name=name, body=lambda e: _body(e, source="scoped-mcp"))
    if case == "bad_target":
        return _put(root, name=name, body=lambda e: _body(e, target=7))
    if case == "bad_summary":
        return _put(root, name=name, body=lambda e: _body(e, summary="s" * 301))
    if case == "bad_metadata":
        return _put(root, name=name, body=lambda e: _body(e, metadata={"k": "v" * 4100}))
    raise AssertionError(case)


DRAIN_CASES = [r for r in ob.REASONS if r != "unreadable"]


@pytest.mark.parametrize("case", DRAIN_CASES)
def test_each_rejection_class_is_quarantined_with_its_reason(tmp_path, monkeypatch, case):
    p = _reject_case(tmp_path, monkeypatch, case)
    root, logs = p.parent, tmp_path / "logs"
    if case == "foreign_owner":
        # The root check would refuse the whole directory under the faked uid; this
        # class is about one file, so check it at the file level.
        with pytest.raises(ob.OutboxRejection) as ei:
            ob.read_and_validate(p, {SRC})
        assert ei.value.reason == case
        return
    d, rec = _drainer(root, logs)
    assert d.drain_once() == 0
    assert rec.appended == []
    assert not p.exists() and not p.is_symlink()
    moved = root / ob.REJECTED_DIR / p.name
    assert moved.exists() or moved.is_symlink()
    reason = json.loads((root / ob.REJECTED_DIR / f"{p.name}.reason").read_text())
    assert reason["reason"] == case
    assert d.stats()["rejected"] == 1


def test_unknown_event_is_refused_even_with_strict_vocab_off(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "STRICT_VOCAB", "off")
    monkeypatch.setenv("AGENT_BUS_STRICT_VOCAB", "off")
    p = _put(tmp_path / "ob", body=lambda e: _body(e, event="nobody.declared"))
    d, rec = _drainer(p.parent, tmp_path / "logs")
    d.drain_once()
    assert rec.appended == []


def test_unreadable_is_reported_for_a_vanished_file(tmp_path):
    with pytest.raises(ob.OutboxRejection) as ei:
        ob.read_and_validate(tmp_path / "gone.json", {SRC})
    assert ei.value.reason == "unreadable"


def test_dotfiles_are_not_read(tmp_path):
    root = tmp_path / "ob"
    root.mkdir()
    (root / ".tmp-half-written").write_text("{")
    d, _rec = _drainer(root, tmp_path / "logs")
    assert d.drain_once() == 0
    assert (root / ".tmp-half-written").exists()
    assert not (root / ob.REJECTED_DIR).exists()


def test_a_root_owned_by_someone_else_is_skipped(tmp_path, monkeypatch):
    p = _put(tmp_path / "ob")
    real = os.getuid()
    monkeypatch.setattr(ob.os, "getuid", lambda: real + 1)
    d, _rec = _drainer(p.parent, tmp_path / "logs")
    assert d.drain_once() == 0
    assert p.exists()


def test_a_missing_root_is_not_an_error(tmp_path):
    d, _ = _drainer(tmp_path / "not-yet", tmp_path / "logs")
    assert d.drain_once() == 0


# ── draining ────────────────────────────────────────────────────────────────────


def test_a_valid_event_is_appended_with_its_own_id_and_deleted(tmp_path):
    p = _put(tmp_path / "ob")
    eid = json.loads(p.read_text())["id"]
    d, rec = _drainer(p.parent, tmp_path / "logs")
    assert d.drain_once() == 1
    assert not p.exists()
    [e] = _logged(tmp_path / "logs")
    assert e["id"] == eid
    assert e["scope"] == "cross-agent"
    assert e["ingest"] == "outbox"
    assert e["hostname"] == "testhost"
    assert [f["id"] for f in rec.fanned] == [eid]


def test_files_drain_in_name_order(tmp_path):
    root = tmp_path / "ob"
    ids = []
    for i in range(5):
        eid = str(uuid.uuid4())
        name = f"20261016T00000{i}.000000-{eid}.json"
        _put(root, name=name)
        ids.append(eid)
    d, _ = _drainer(root, tmp_path / "logs")
    d.drain_once()
    assert [e["id"] for e in _logged(tmp_path / "logs")] == ids


def test_max_per_cycle_bounds_one_cycle(tmp_path):
    root = tmp_path / "ob"
    for _ in range(7):
        _put(root)
    d, _ = _drainer(root, tmp_path / "logs")
    d.max_per_cycle = 3
    assert d.drain_once() == 3
    assert len(ob.pending_files(root)) == 4


def test_a_crash_between_append_and_delete_does_not_double_log(tmp_path):
    root, logs = tmp_path / "ob", tmp_path / "logs"
    p = _put(root)
    saved = p.read_bytes()
    d, _ = _drainer(root, logs)
    d.drain_once()
    # Simulate the crash: the append landed, the unlink did not.
    p.write_bytes(saved)
    fresh, rec = _drainer(root, logs)  # a restarted process, empty memory
    assert fresh.drain_once() == 0
    assert rec.appended == []
    assert not p.exists()
    assert fresh.stats()["duplicates"] == 1
    assert len(_logged(logs)) == 1


def test_fanout_failure_does_not_undo_the_append(tmp_path):
    p = _put(tmp_path / "ob")
    d, _ = _drainer(p.parent, tmp_path / "logs")

    def boom(e):
        raise RuntimeError("nats down")

    d.fanout = boom
    assert d.drain_once() == 1
    assert not p.exists()
    assert len(_logged(tmp_path / "logs")) == 1


# ── through the server ──────────────────────────────────────────────────────────


@pytest.fixture
def served(comms_dir, monkeypatch):
    root = comms_dir / "queue-outbox"
    root.mkdir(mode=0o700)
    roots = [ob.OutboxRoot(root, frozenset({SRC}))]
    monkeypatch.setattr(ab, "OUTBOXES", roots)
    monkeypatch.setattr(ab, "OUTBOX_LOCK_FILE", comms_dir / ".outbox-drain.lock")
    monkeypatch.setattr(ab, "OUTBOX_STATUS_FILE", comms_dir / "outbox-status.json")
    monkeypatch.setattr(ab, "_drainer", None)
    yield root
    ab.release_outbox_lock()


def test_verify_chain_is_clean_after_a_1000_event_drain(served, comms_dir):
    for i in range(1000):
        ob.write_event(served, "task.completed", SRC, f"task {i}", metadata={"i": i})
    ab.log_event("task.approved", "dispatcher", "interleaved hand write")
    d = ab._make_drainer()
    while ob.pending_files(served):
        d.drain_once()
    r = ab.verify_chain()
    assert r["total_events"] == 1001
    assert r["chain_breaks"] == 0
    assert r["verified"] == 1001


def test_high_priority_outbox_events_fan_out_like_hand_logged_ones(served, publisher, monkeypatch):
    sent = []
    monkeypatch.setattr(ab, "emit_ntfy", lambda e: sent.append(e["event"]))
    ob.write_event(served, "task.dead-lettered", SRC, "dead")
    ob.write_event(served, "task.submitted", SRC, "quiet")
    ab._make_drainer().drain_once()
    assert sent == ["task.dead-lettered"]
    assert [e["event"] for e in publisher.published] == ["task.dead-lettered", "task.submitted"]


def test_only_the_lock_holder_drains(served, comms_dir):
    holder = open(comms_dir / ".outbox-drain.lock", "w")  # noqa: SIM115
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    ob.write_event(served, "task.submitted", SRC, "waiting")
    try:

        async def run():
            task = asyncio.create_task(ab.outbox_drain_loop())
            await asyncio.sleep(0.3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        assert len(ob.pending_files(served)) == 1
        assert ab._outbox_lock_fd is None
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_the_lock_is_held_across_processes(served, comms_dir):
    """A second process cannot take the lock while this one holds it."""
    assert ab.acquire_outbox_lock() is True
    child = (
        "import fcntl, sys\n"
        f"f = open({str(comms_dir / '.outbox-drain.lock')!r}, 'w')\n"
        "try:\n"
        "    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except OSError:\n"
        "    sys.exit(3)\n"
    )
    assert subprocess.run([sys.executable, "-c", child]).returncode == 3


def test_the_loop_drains_and_writes_a_heartbeat(served, comms_dir, monkeypatch):
    monkeypatch.setattr(ab, "OUTBOX_INTERVAL", 0.05)
    ob.write_event(served, "task.submitted", SRC, "one")

    async def run():
        task = asyncio.create_task(ab.outbox_drain_loop())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if not ob.pending_files(served) and (comms_dir / "outbox-status.json").exists():
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert ob.pending_files(served) == []
    hb = json.loads((comms_dir / "outbox-status.json").read_text())
    assert hb["pid"] == os.getpid()
    assert hb["stats"]["drained"] == 1


def test_the_loop_is_inert_without_configuration(comms_dir, monkeypatch):
    monkeypatch.setattr(ab, "OUTBOXES", [])
    asyncio.run(asyncio.wait_for(ab.outbox_drain_loop(), 1))


def test_get_status_counts_pending_age_and_rejected_from_disk(served, comms_dir):
    old = f"20200101T000000.000000-{uuid.uuid4()}.json"
    _put(served, name=old)
    _put(served, name=f"20200101T000001.000000-{uuid.uuid4()}.json", raw=b"{bad")
    ab._make_drainer().drain_once()  # ingests the first, rejects the second
    _put(served, name=old.replace("000000-", "000002-", 1))
    st = ab.get_status()["outboxes"]
    [root] = st["roots"]
    assert root["pending"] == 1
    assert root["rejected"] == 1
    assert root["oldest_pending_age_s"] > 86400
    assert st["configured_in_this_process"] is True


def test_a_non_draining_process_reads_roots_from_the_heartbeat(served, comms_dir, monkeypatch):
    """Every stdio child answers get_status without AGENT_BUS_OUTBOXES of its own."""
    ab.write_outbox_status()
    ob.write_event(served, "task.submitted", SRC, "pending")
    monkeypatch.setattr(ab, "OUTBOXES", [])
    st = ab.get_status()["outboxes"]
    assert st["configured_in_this_process"] is False
    assert st["drainer_heartbeat_age_s"] is not None
    assert [r["pending"] for r in st["roots"]] == [1]


def test_ts_is_normalised_to_utc_offset_form(tmp_path):
    p = _put(tmp_path / "ob", body=lambda e: _body(e, ts="2026-10-16T18:00:00-04:00"))
    d, _ = _drainer(p.parent, tmp_path / "logs")
    d.drain_once()
    [e] = _logged(tmp_path / "logs")
    assert e["ts"] == "2026-10-16T22:00:00+00:00"
