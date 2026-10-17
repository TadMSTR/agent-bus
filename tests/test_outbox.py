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


@pytest.mark.parametrize("ts", ["2026-10-16T22:00:00Z", "2026-10-16T22:00:00.123456z"])
def test_a_trailing_z_is_accepted_on_every_python(tmp_path, ts):
    p = _put(tmp_path / "ob", body=lambda e: _body(e, ts=ts))
    d, _ = _drainer(p.parent, tmp_path / "logs")
    assert d.drain_once() == 1
    [e] = _logged(tmp_path / "logs")
    assert e["ts"].startswith("2026-10-16T22:00:00") and e["ts"].endswith("+00:00")


def test_a_fifo_swapped_in_after_lstat_does_not_block(tmp_path, monkeypatch):
    """The race: the stat sees a regular file, the open meets a FIFO.

    Hooked at os.stat (the drainer stats relative to the pinned root, not by Path) so the
    swap lands exactly in the window between the check and the open.
    """
    root = tmp_path / "ob"
    p = _put(root)
    real_stat = os.stat
    seen = {}

    def stat_then_swap(name, *a, **kw):
        st = real_stat(name, *a, **kw)
        if name == p.name and kw.get("dir_fd") is not None and not seen:
            seen["done"] = True
            os.unlink(name, dir_fd=kw["dir_fd"])
            os.mkfifo(name, dir_fd=kw["dir_fd"])
        return st

    monkeypatch.setattr(ob.os, "stat", stat_then_swap)
    with pytest.raises(ob.OutboxRejection) as ei:
        ob.read_and_validate(p, {SRC})
    assert seen, "the swap never ran: the test exercised nothing"
    assert ei.value.reason == "not_regular_file"


def test_reason_file_is_0600_and_the_log_line_cannot_be_split(tmp_path, caplog):
    p = _put(tmp_path / "ob", body=lambda e: _body(e, source="evil\ninjected=1"))
    d, _ = _drainer(p.parent, tmp_path / "logs")
    d.drain_once()
    reason = p.parent / ob.REJECTED_DIR / f"{p.name}.reason"
    assert oct(reason.stat().st_mode & 0o777) == "0o600"
    assert all("\n" not in r.getMessage() for r in caplog.records)


def test_heartbeat_file_is_0600(served, comms_dir):
    ab.write_outbox_status()
    assert oct((comms_dir / "outbox-status.json").stat().st_mode & 0o777) == "0o600"


# ── audit 2026-10-16 F-01..F-04: the drainer must not be steerable by its producer ──


def test_f01_a_symlinked_rejected_dir_is_not_followed(tmp_path):
    """Security's reproduction (a): `.rejected -> victim/` plus a pending file named
    `authorized_keys` overwrote victim/authorized_keys."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "authorized_keys").write_text("ORIGINAL\n")
    root = tmp_path / "ob"
    root.mkdir(mode=0o700)
    (root / ob.REJECTED_DIR).symlink_to(victim)
    (root / "authorized_keys").write_text("ssh-ed25519 ATTACKER\n")
    d, rec = _drainer(root, tmp_path / "logs")
    d.drain_once()
    assert (victim / "authorized_keys").read_text() == "ORIGINAL\n"
    assert not (victim / "authorized_keys.reason").exists()
    assert rec.appended == []
    assert d.stats()["dropped"] == 1
    assert not (root / "authorized_keys").exists()


def test_f01_a_planted_reason_symlink_is_not_written_through(tmp_path):
    """Security's reproduction (b): a `.reason` symlink to a shell rc file, plus a file
    whose name carries a command substitution, produced a poisoned rc file."""
    rc = tmp_path / "bashrc"
    rc.write_text("ORIGINAL\n")
    root = tmp_path / "ob"
    rej = root / ob.REJECTED_DIR
    rej.mkdir(parents=True, mode=0o700)
    name = "$(id>PWNED)"
    (rej / f"{name}.reason").symlink_to(rc)
    (root / name).write_text("x")
    d, _ = _drainer(root, tmp_path / "logs")
    d.drain_once()
    assert rc.read_text() == "ORIGINAL\n"
    reason = rej / f"{name}.reason"
    assert not reason.is_symlink()
    assert json.loads(reason.read_text())["reason"] == "bad_name"


def test_f02_a_root_that_is_a_symlink_is_not_drained(tmp_path):
    """The root swapped for a symlink (to ~/.ssh, say): refused, nothing moved."""
    target = tmp_path / "ssh"
    target.mkdir()
    (target / "id_ed25519").write_text("KEY\n")
    root = tmp_path / "ob"
    root.symlink_to(target)
    d, _ = _drainer(root, tmp_path / "logs")
    assert d.drain_once() == 0
    assert (target / "id_ed25519").read_text() == "KEY\n"
    assert not (target / ob.REJECTED_DIR).exists()
    assert ob.pending_files(root) == []


def test_f02_status_does_not_follow_a_symlinked_root(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "x").write_text("x")
    root = tmp_path / "ob"
    root.symlink_to(target)
    st = ob.root_status(ob.OutboxRoot(root, frozenset({SRC})))
    assert st["pending"] == 0


def test_f01_a_rejected_dir_owned_by_someone_else_is_not_used(tmp_path, monkeypatch):
    root = tmp_path / "ob"
    root.mkdir(mode=0o700)
    (root / ob.REJECTED_DIR).mkdir()
    p = _put(root, name="nonsense.json")
    real_fstat = os.fstat
    rej_ino = (root / ob.REJECTED_DIR).stat().st_ino

    def fstat(fd):
        st = real_fstat(fd)
        if st.st_ino == rej_ino:
            fake = list(st)
            fake[4] = st.st_uid + 1  # st_uid
            return os.stat_result(fake)
        return st

    monkeypatch.setattr(ob.os, "fstat", fstat)
    d, _ = _drainer(root, tmp_path / "logs")
    d.drain_once()
    assert not p.exists()
    assert list((root / ob.REJECTED_DIR).iterdir()) == []
    assert d.stats()["dropped"] == 1


def test_f03_quarantine_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(ob, "REJECTED_LIMIT", 3)
    root = tmp_path / "ob"
    for i in range(5):
        _put(root, name=f"bad-{i}.json")
    d, _ = _drainer(root, tmp_path / "logs")
    d.drain_once()
    kept = [p for p in (root / ob.REJECTED_DIR).iterdir() if not p.name.endswith(".reason")]
    assert len(kept) == 3
    assert d.stats()["dropped"] == 2
    assert ob.pending_files(root) == []


def test_f03_scan_and_status_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(ob, "SCAN_LIMIT", 4)
    root = tmp_path / "ob"
    for _ in range(9):
        _put(root)
    st = ob.root_status(ob.OutboxRoot(root, frozenset({SRC})))
    assert st["pending"] == 4 and st["pending_capped"] is True


def test_f04_an_unexpected_error_quarantines_that_file_and_the_next_one_drains(
    tmp_path, monkeypatch
):
    root = tmp_path / "ob"
    first = _put(root, name=f"20200101T000000.000000-{uuid.uuid4()}.json")
    _put(root, name=f"20200101T000001.000000-{uuid.uuid4()}.json")
    real = ob.validate_event
    calls = {"n": 0}

    def flaky(obj, name, sources):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("surprise")
        return real(obj, name, sources)

    monkeypatch.setattr(ob, "validate_event", flaky)
    d, rec = _drainer(root, tmp_path / "logs")
    assert d.drain_once() == 1
    assert len(rec.appended) == 1
    reason = json.loads((root / ob.REJECTED_DIR / f"{first.name}.reason").read_text())
    assert reason == reason | {"reason": "unreadable", "detail": "RuntimeError"}


def test_f04_non_finite_constants_are_refused(tmp_path):
    name, eid = _name()
    raw = json.dumps(_body(eid)).replace('"schema": 1', '"schema": NaN').encode()
    p = _put(tmp_path / "ob", name=name, raw=raw)
    with pytest.raises(ob.OutboxRejection) as ei:
        ob.read_and_validate(p, {SRC})
    assert ei.value.reason == "not_json"


def test_f04_deep_metadata_is_refused_without_recursing(tmp_path):
    deep: dict = {}
    cur = deep
    for _ in range(40):
        cur["k"] = {}
        cur = cur["k"]
    p = _put(tmp_path / "ob", body=lambda e: _body(e, metadata=deep))
    with pytest.raises(ob.OutboxRejection) as ei:
        ob.read_and_validate(p, {SRC})
    assert ei.value.reason == "bad_metadata"


def test_f04_an_unwritable_log_stops_the_cycle_and_keeps_the_files(tmp_path):
    root = tmp_path / "ob"
    a = _put(root)
    b = _put(root)
    d, _ = _drainer(root, tmp_path / "logs")

    def boom(e):
        raise OSError("disk full")

    d.append = boom
    assert d.drain_once() == 0
    assert a.exists() and b.exists()
    assert not (root / ob.REJECTED_DIR).exists()


# ── round 2 (2026-10-17): one producer must not stop the others ─────────────


def _two_roots(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(mode=0o700)
    b.mkdir(mode=0o700)
    rec = Recorder(tmp_path / "logs")
    d = ob.Drainer(
        roots=[ob.OutboxRoot(a, frozenset({SRC})), ob.OutboxRoot(b, frozenset({SRC}))],
        logs_dir=tmp_path / "logs",
        hostname="testhost",
        append=rec.append,
        fanout=rec.fanout,
    )
    return a, b, d, rec


def test_r2_01_a_read_only_root_does_not_abort_the_cycle(tmp_path):
    """Security's reproduction: root `a` 0555 with no .rejected and a junk entry raised
    PermissionError out of drain_once, and root `b` drained nothing."""
    a, b, d, rec = _two_roots(tmp_path)
    (a / "junk").write_text("x")
    _put(b)
    a.chmod(0o555)
    try:
        assert d.drain_once() == 1
        assert len(rec.appended) == 1
        assert d.stats()["stuck"] == 1  # junk could not be moved or deleted
    finally:
        a.chmod(0o700)


def test_r2_02_unmovable_entries_cannot_starve_later_roots(tmp_path):
    """Security's reproduction: max_per_cycle=5; five bad files in `a`, each blocked by a
    non-empty directory of the same name under .rejected/; `b` drained 0 in 3 cycles."""
    a, b, d, rec = _two_roots(tmp_path)
    d.max_per_cycle = 5
    rej = a / ob.REJECTED_DIR
    rej.mkdir(mode=0o700)
    for i in range(5):
        (a / f"bad-{i}").write_text("x")
        (rej / f"bad-{i}").mkdir()
        (rej / f"bad-{i}" / "keep").write_text("x")
    _put(b)
    for _ in range(3):
        d.drain_once()
    assert len(rec.appended) == 1
    assert d.stats()["stuck"] == 5


def test_r2_02_stuck_entries_are_skipped_then_retried(tmp_path):
    a, _b, d, _ = _two_roots(tmp_path)
    d.stuck_skip_cycles = 2
    (a / "junk").write_text("x")
    a.chmod(0o555)
    try:
        d.drain_once()
        assert d.stats()["stuck"] == 1
        rejected_before = d.stats()["rejected"]
        d.drain_once()  # skipped: not attempted again
        assert d.stats()["rejected"] == rejected_before
    finally:
        a.chmod(0o700)
    d.drain_once()  # skip expired and the root is writable again: quarantined
    assert d.stats()["stuck"] == 0
    assert (a / ob.REJECTED_DIR / "junk").exists()


def test_r2_02_the_first_root_rotates(tmp_path):
    a, b, d, rec = _two_roots(tmp_path)
    d.max_per_cycle = 2  # one each
    for _ in range(2):
        _put(a, body=lambda e: _body(e, summary="from-a"))
        _put(b, body=lambda e: _body(e, summary="from-b"))
    d.drain_once()
    d.drain_once()
    order = [e["summary"] for e in rec.appended]
    # Each cycle serves both roots (equal shares), and cycle 2 starts with the root
    # cycle 1 served second. Pre-fix, `a` took the whole budget first: a, a, b, b.
    assert order == ["from-a", "from-b", "from-b", "from-a"], order


def test_r2_03_the_quarantine_is_counted_once_per_root_per_cycle(tmp_path, monkeypatch):
    calls = []
    real = ob._count_rejected
    monkeypatch.setattr(ob, "_count_rejected", lambda *a: calls.append(1) or real(*a))
    root = tmp_path / "ob"
    for i in range(6):
        _put(root, name=f"bad-{i}.json")
    d, _ = _drainer(root, tmp_path / "logs")
    d.drain_once()
    assert len(calls) == 1
    assert len([p for p in (root / ob.REJECTED_DIR).iterdir() if p.suffix != ".reason"]) == 6


def test_r2_03_drops_are_reported_per_root(tmp_path, monkeypatch):
    monkeypatch.setattr(ob, "REJECTED_LIMIT", 1)
    root = tmp_path / "ob"
    for i in range(3):
        _put(root, name=f"bad-{i}.json")
    d, _ = _drainer(root, tmp_path / "logs")
    d.drain_once()
    assert d.stats()["dropped_by_root"] == {str(root): 2}


def test_r3_01_a_stuck_valid_file_is_appended_once_however_long_it_stays(tmp_path):
    """Security's probe: a valid event in a read-only root (append ok, unlink fails),
    seen_limit forced small, 10 other events per cycle: appended 6x at 86caf84."""
    a, b, d, rec = _two_roots(tmp_path)
    d.seen_limit = 5
    d.stuck_skip_cycles = 1
    stuck = _put(a)
    stuck_id = json.loads(stuck.read_text())["id"]
    a.chmod(0o555)
    try:
        for _ in range(12):
            for _ in range(10):
                _put(b)
            d.drain_once()
    finally:
        a.chmod(0o700)
    assert [e["id"] for e in rec.appended].count(stuck_id) == 1


def test_cr03_a_large_backlog_drains_oldest_first(tmp_path, monkeypatch):
    """CodeRabbit CR-03 on #13: with a backlog larger than the per-cycle window, the
    window must be the oldest names, not whatever scandir returned first."""
    monkeypatch.setattr(ob, "SCAN_LIMIT", 1000)
    root = tmp_path / "ob"
    root.mkdir(mode=0o700)
    names = [f"20260101T00{i:04d}.000000-{uuid.uuid4()}.json" for i in range(200)]
    for n in reversed(names):  # newest created first, so directory order is not name order
        _put(root, name=n)
    fd = ob.open_root(root)
    try:
        got, more = ob.list_pending(fd, limit=10)
    finally:
        os.close(fd)
    assert got == names[:10]
    assert more is True
