"""reconcile.py — the artifact scanner, and its share of the hash chain.

It appends to the same JSONL as the server from a PM2 cron, so it is one of the two
writers that previously chained nothing and held no lock.
"""

import json
import os
from datetime import datetime, timezone

import pytest

import event_log
import reconcile


@pytest.fixture
def recon(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    logs = tmp_path / "logs"
    artifacts.mkdir()
    logs.mkdir()
    monkeypatch.setattr(reconcile, "COMMS_DIR", tmp_path)
    monkeypatch.setattr(reconcile, "ARTIFACTS_DIR", artifacts)
    monkeypatch.setattr(reconcile, "LOGS_DIR", logs)
    monkeypatch.setattr(reconcile, "CURSOR_FILE", tmp_path / ".reconcile-cursor")
    monkeypatch.setattr(reconcile, "MARKERS_FILE", tmp_path / ".reconcile-markers.json")
    return tmp_path, artifacts, logs


def _events(logs):
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = logs / f"{date}-cross-agent.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_logs_an_untracked_artifact(recon, capsys):
    _, artifacts, logs = recon
    (artifacts / "plan.md").write_text("x")

    reconcile.main()

    events = _events(logs)
    assert len(events) == 1
    assert events[0]["event"] == "artifact.untracked"
    assert events[0]["source"] == "reconciliation"
    assert events[0]["artifact_path"].endswith("plan.md")
    assert "logged 1 untracked artifact" in capsys.readouterr().out


def test_recurses_into_subdirectories(recon):
    _, artifacts, logs = recon
    nested = artifacts / "build-plans" / "x"
    nested.mkdir(parents=True)
    (nested / "handoff.md").write_text("x")

    reconcile.main()

    assert len(_events(logs)) == 1


def test_directories_are_not_logged(recon):
    _, artifacts, logs = recon
    (artifacts / "empty-dir").mkdir()

    reconcile.main()

    assert _events(logs) == []


def test_already_logged_artifacts_are_not_relogged(recon):
    _, artifacts, logs = recon
    (artifacts / "plan.md").write_text("x")

    reconcile.main()
    # Cursor now exists; reset its mtime so only the dedup check can save us.
    reconcile.CURSOR_FILE.unlink()
    reconcile.main()

    assert len(_events(logs)) == 1


def test_artifacts_older_than_the_cursor_are_skipped(recon):
    _, artifacts, logs = recon
    (artifacts / "old.md").write_text("x")
    reconcile.CURSOR_FILE.touch()

    reconcile.main()

    assert _events(logs) == []


def test_cursor_is_created(recon):
    _, artifacts, _ = recon
    (artifacts / "a.md").write_text("x")

    reconcile.main()

    assert reconcile.CURSOR_FILE.exists()


def test_reconcile_events_chain(recon):
    """Its appends must chain like every other writer's."""
    _, artifacts, logs = recon
    for name in ("a.md", "b.md", "c.md"):
        (artifacts / name).write_text("x")

    reconcile.main()

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        ln for ln in (logs / f"{date}-cross-agent.jsonl").read_text().splitlines() if ln.strip()
    ]
    assert len(lines) == 3
    for i, line in enumerate(lines[1:], start=1):
        assert json.loads(line)["prev_hash"] == event_log.sha256(lines[i - 1])


def test_known_artifact_paths_is_empty_before_any_log(recon):
    assert reconcile.known_artifact_paths() == set()


def test_known_artifact_paths_tolerates_a_corrupt_line(recon):
    reconcile.log_path().write_text('not json\n{"artifact_path": "/x"}\n')
    assert reconcile.known_artifact_paths() == {"/x"}


def test_log_path_targets_the_cross_agent_file(recon):
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert reconcile.log_path().name == f"{date}-cross-agent.jsonl"


def test_no_output_when_nothing_found(recon, capsys):
    reconcile.main()
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# .reconcile-ignore (audit-workflow-2026-11 p1)
# ---------------------------------------------------------------------------


def _prep_tree(root):
    """A security-check-prep.py-shaped scratch dir, nested two levels."""
    prep = root / "audit-requests" / "some-build" / "prep"
    (prep / "batches").mkdir(parents=True)
    (prep / "views").mkdir()
    (prep / "returns").mkdir()
    for rel in (
        "manifest.json",
        "diff.patch",
        "batches/b01.md",
        "views/v01.md",
        "returns/b01.jsonl",
    ):
        (prep / rel).write_text("x")
    return prep


def _marker_events(events):
    return [e for e in events if e["summary"].startswith("Ignored tree: ")]


def test_a_marked_tree_collapses_to_one_event(recon):
    _, artifacts, logs = recon
    prep = _prep_tree(artifacts)
    (prep / reconcile.IGNORE_MARKER).write_text("")
    (artifacts / "audit-requests" / "some-build" / "request.md").write_text("x")

    reconcile.main()

    events = _events(logs)
    assert sorted(e["artifact_path"] for e in events) == sorted(
        [
            str(artifacts / "audit-requests" / "some-build" / "request.md"),
            str(prep / reconcile.IGNORE_MARKER),
        ]
    )
    [m] = _marker_events(events)
    assert m["summary"] == "Ignored tree: audit-requests/some-build/prep"
    assert m["event"] == "artifact.untracked"


@pytest.mark.parametrize("where", [(), ("audit-requests",), ("audit-requests", "b0")])
def test_a_marker_at_the_root_a_category_or_a_build_is_not_honoured(recon, where):
    """Audit F-01 (and round-2 OV-44 for a build's own dir): a marker that shallow would end
    reconciliation of a whole category or a build's requests. Its tree is scanned as usual,
    including files written after it, and the marker is logged."""
    _, artifacts, logs = recon
    top = artifacts.joinpath(*where)
    (top / "b1").mkdir(parents=True)
    (top / reconcile.IGNORE_MARKER).write_text("")
    (top / "b1" / "request.md").write_text("x")

    reconcile.main()
    _backdate(reconcile.CURSOR_FILE, by=10)  # so the next file is newer than the cursor
    (top / "b1" / "request-round2.md").write_text("x")
    reconcile.main()

    rel = "/".join(where) or "."
    assert sorted(e["summary"] for e in _events(logs)) == [
        f"Marker not honoured: {rel}",
        "Untracked artifact: request-round2.md",
        "Untracked artifact: request.md",
    ]


def test_a_marker_not_honoured_is_logged_once_a_day(recon):
    _, artifacts, logs = recon
    (artifacts / reconcile.IGNORE_MARKER).write_text("")
    reconcile.main()
    reconcile.main()
    assert len(_events(logs)) == 1
    (logs / next(logs.iterdir()).name).unlink()  # a new day's log

    reconcile.main()

    assert [e["summary"] for e in _events(logs)] == ["Marker not honoured: ."]


def test_a_shallow_marker_does_not_stop_a_deeper_one(recon):
    _, artifacts, logs = recon
    (artifacts / reconcile.IGNORE_MARKER).write_text("")
    prep = _prep_tree(artifacts)
    (prep / reconcile.IGNORE_MARKER).write_text("")

    reconcile.main()

    assert sorted(e["summary"] for e in _events(logs)) == [
        "Ignored tree: audit-requests/some-build/prep",
        "Marker not honoured: .",
    ]


def test_a_dangling_link_marker_is_logged(recon):
    _, artifacts, logs = recon
    d = artifacts / "work" / "b1" / "scratch"
    d.mkdir(parents=True)
    (d / "a.md").write_text("x")
    (d / reconcile.IGNORE_MARKER).symlink_to(d / "nowhere")

    reconcile.main()

    assert [e["summary"] for e in _events(logs)] == ["Ignored tree: work/b1/scratch"]


def test_a_marker_is_logged_once(recon):
    _, artifacts, logs = recon
    prep = _prep_tree(artifacts)
    (prep / reconcile.IGNORE_MARKER).write_text("")

    reconcile.main()
    reconcile.CURSOR_FILE.unlink()  # not the cursor, not the day's log: the state decides
    (logs / next(logs.iterdir()).name).unlink()
    reconcile.main()

    assert _marker_events(_events(logs)) == []


def _backdate(p, by=10_000):
    t = reconcile.CURSOR_FILE.stat().st_mtime - by
    os.utime(p, (t, t), follow_symlinks=False)


def test_a_backdated_marker_is_still_announced(recon):
    """OV-40/OV-01 round 2: the marker is not subject to the mtime cursor."""
    _, artifacts, logs = recon
    reconcile.main()  # sets the cursor
    d = artifacts / "work" / "b1" / "scratch"
    d.mkdir(parents=True)
    (d / "a.md").write_text("x")
    _backdate(d / "a.md")
    (d / reconcile.IGNORE_MARKER).write_text("")
    _backdate(d / reconcile.IGNORE_MARKER)

    reconcile.main()

    assert [e["summary"] for e in _events(logs)] == ["Ignored tree: work/b1/scratch"]


def test_an_old_file_renamed_onto_the_marker_is_announced(recon):
    _, artifacts, logs = recon
    d = artifacts / "work" / "b1" / "scratch"
    d.mkdir(parents=True)
    old = d / "old.md"
    old.write_text("x")
    reconcile.main()  # logs old.md, sets the cursor
    _backdate(old)
    old.rename(d / reconcile.IGNORE_MARKER)

    reconcile.main()

    assert [e["summary"] for e in _events(logs)][-1] == "Ignored tree: work/b1/scratch"


def test_a_replaced_marker_is_announced_again(recon):
    _, artifacts, logs = recon
    d = artifacts / "work" / "b1" / "scratch"
    d.mkdir(parents=True)
    m = d / reconcile.IGNORE_MARKER
    m.write_text("")
    reconcile.main()
    tmp = d / "new"
    tmp.write_text("")
    m.unlink()
    tmp.rename(m)  # new inode at the same path
    (logs / next(logs.iterdir()).name).unlink()  # and not saved by the day's dedup

    reconcile.main()

    assert len(_marker_events(_events(logs))) == 1


def test_a_corrupt_state_file_re_announces(recon):
    _, artifacts, logs = recon
    d = artifacts / "work" / "b1" / "scratch"
    d.mkdir(parents=True)
    (d / reconcile.IGNORE_MARKER).write_text("")
    reconcile.main()
    (logs / next(logs.iterdir()).name).unlink()
    for bad in ("{nope", "{}", "[1, 2]"):
        reconcile.MARKERS_FILE.write_text(bad)
        reconcile.main()
    assert len(_marker_events(_events(logs))) == 3  # noisy, never silent


def test_a_marker_added_after_the_files_still_hides_them(recon):
    _, artifacts, logs = recon
    prep = _prep_tree(artifacts)
    # Cursor older than the files, so only the marker can keep them out; the files are
    # older than the marker, which is what a marker written after the fact looks like.
    reconcile.CURSOR_FILE.touch()
    t = reconcile.CURSOR_FILE.stat().st_mtime
    os.utime(reconcile.CURSOR_FILE, (t - 100, t - 100))
    for f in prep.rglob("*"):
        if f.is_file():
            os.utime(f, (t - 50, t - 50))
    (prep / reconcile.IGNORE_MARKER).write_text("")

    reconcile.main()

    assert [e["summary"] for e in _events(logs)] == ["Ignored tree: audit-requests/some-build/prep"]


def test_without_the_marker_the_same_tree_is_logged(recon):
    """The control: it is the marker, not the layout, that hides the tree."""
    _, artifacts, logs = recon
    _prep_tree(artifacts)

    reconcile.main()

    assert len(_events(logs)) == 5


def test_a_marker_hides_only_its_own_tree(recon):
    _, artifacts, logs = recon
    prep = _prep_tree(artifacts)
    (prep / reconcile.IGNORE_MARKER).write_text("")
    sibling = artifacts / "audit-requests" / "some-build" / "other"
    sibling.mkdir()
    (sibling / "note.md").write_text("x")

    reconcile.main()

    assert [
        e["artifact_path"] for e in _events(logs) if not e["summary"].startswith("Ignored")
    ] == [str(sibling / "note.md")]


def test_a_directory_named_like_the_marker_does_not_hide_anything(recon):
    _, artifacts, logs = recon
    d = artifacts / "x"
    (d / reconcile.IGNORE_MARKER).mkdir(parents=True)
    (d / "a.md").write_text("x")

    reconcile.main()

    assert [e["artifact_path"] for e in _events(logs)] == [str(d / "a.md")]


def test_an_unreadable_directory_fails_the_run_and_holds_the_cursor(recon, monkeypatch, capsys):
    """OV-39: a walk error is never read as an empty directory. Simulated through os.walk's
    onerror, because chmod 000 does not stop root, and CI runs as root."""
    _, artifacts, logs = recon
    (artifacts / "a.md").write_text("x")
    real_walk = os.walk

    def walk(top, onerror=None, **kw):
        onerror(PermissionError(13, "Permission denied", str(artifacts / "locked")))
        yield from real_walk(top, onerror=onerror, **kw)

    monkeypatch.setattr(reconcile.os, "walk", walk)
    with pytest.raises(SystemExit) as e:
        reconcile.main()

    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "could not read" in err and str(artifacts / "locked") in err
    assert not reconcile.CURSOR_FILE.exists()
    assert len(_events(logs)) == 1  # what it could read is still logged


def test_a_walk_error_keeps_announced_markers(recon, monkeypatch):
    _, artifacts, _ = recon
    reconcile.MARKERS_FILE.write_text('["/gone/elsewhere/.reconcile-ignore:1"]')
    real_walk = os.walk

    def walk(top, onerror=None, **kw):
        onerror(PermissionError(13, "Permission denied", str(artifacts / "locked")))
        yield from real_walk(top, onerror=onerror, **kw)

    monkeypatch.setattr(reconcile.os, "walk", walk)
    with pytest.raises(SystemExit):
        reconcile.main()
    assert "/gone/elsewhere/.reconcile-ignore:1" in reconcile.MARKERS_FILE.read_text()


def test_a_bad_byte_and_a_literal_backslash_name_do_not_collide(recon):
    """Round-2 OV-01: with backslashreplace alone, b"a\\xff" and the literal name "a\\\\xff"
    logged the same path, and the second was deduped away for good. Both are logged."""
    _, artifacts, logs = recon
    os.close(os.open(os.fsencode(artifacts) + b"/a\xff", os.O_CREAT | os.O_WRONLY, 0o600))
    reconcile.main()
    _backdate(reconcile.CURSOR_FILE, by=10)
    (artifacts / "a\\xff").write_text("x")  # five characters: a, backslash, x, f, f

    reconcile.main()

    assert sorted(e["artifact_path"] for e in _events(logs)) == sorted(
        [str(artifacts) + "/a\\xff", str(artifacts) + "/a\\\\xff"]
    )


def test_a_non_utf8_name_is_logged_escaped(recon):
    """Audit F-05: a non-UTF-8 name used to fail every run and hold the cursor until someone
    removed the file. It is logged with the bad byte escaped, and the run succeeds."""
    _, artifacts, logs = recon
    d = os.fsencode(artifacts / "work") + b"/d\xfe"
    os.makedirs(d)
    os.close(os.open(d + b"/bad\xff.md", os.O_CREAT | os.O_WRONLY, 0o600))

    reconcile.main()
    reconcile.main()  # and not again

    [e] = _events(logs)
    assert e["artifact_path"] == str(artifacts / "work") + "/d\\xfe/bad\\xff.md"
    assert e["summary"] == "Untracked artifact: bad\\xff.md"
    assert reconcile.CURSOR_FILE.exists()


def test_a_non_utf8_marker_dir_is_announced_escaped(recon):
    _, artifacts, logs = recon
    d = os.fsencode(artifacts / "work" / "b1") + b"/p\xff"
    os.makedirs(d)
    os.close(os.open(d + b"/" + reconcile.IGNORE_MARKER.encode(), os.O_CREAT | os.O_WRONLY))

    reconcile.main()
    reconcile.main()

    assert [e["summary"] for e in _events(logs)] == ["Ignored tree: work/b1/p\\xff"]


def test_one_bad_item_does_not_stop_the_scan(recon, monkeypatch, capsys):
    """OV-33: an item that can't be logged is reported, the rest is logged, and the run fails
    loudly with the cursor held."""
    _, artifacts, logs = recon
    (artifacts / "bad.md").write_text("x")
    (artifacts / "zz-good.md").write_text("x")
    real = reconcile.append_event

    def append(event, *a, **k):
        if event["artifact_path"].endswith("bad.md"):
            raise OSError("disk full")
        return real(event, *a, **k)

    monkeypatch.setattr(reconcile, "append_event", append)
    with pytest.raises(SystemExit) as e:
        reconcile.main()

    assert e.value.code == 1
    assert [x["artifact_path"] for x in _events(logs)] == [str(artifacts / "zz-good.md")]
    err = capsys.readouterr().err
    assert "could not log" in err and "bad.md" in err
    assert not reconcile.CURSOR_FILE.exists()


def test_a_file_that_vanishes_mid_scan_is_skipped(recon, monkeypatch):
    _, artifacts, logs = recon
    (artifacts / "a.md").write_text("x")
    real = reconcile.artifact_files

    def files(errors):
        yield artifacts / "gone.md", reconcile.FILE
        yield artifacts / reconcile.IGNORE_MARKER, reconcile.NOT_HONOURED
        yield from real(errors)

    monkeypatch.setattr(reconcile, "artifact_files", files)
    reconcile.main()  # no SystemExit

    assert [x["artifact_path"] for x in _events(logs)] == [str(artifacts / "a.md")]
    assert reconcile.CURSOR_FILE.exists()


def test_a_marker_that_vanishes_mid_scan_holds_the_cursor(recon, capsys):
    """Audit F-04 case 4 (IV-81): the walk has already pruned the marker's tree. If the run
    succeeded, the cursor would move past the files beside it and they would never be
    logged, although the marker is gone. The run fails, and the next one logs them."""
    _, artifacts, logs = recon
    d = artifacts / "work" / "b1" / "scratch"
    d.mkdir(parents=True)
    (d / "a.md").write_text("x")
    marker = d / reconcile.IGNORE_MARKER
    marker.write_text("")
    real = reconcile.artifact_files

    def files(errors):
        for f, kind in real(errors):
            if kind == reconcile.TREE:
                f.unlink()
            yield f, kind

    reconcile.artifact_files, saved = files, real
    try:
        with pytest.raises(SystemExit) as e:
            reconcile.main()
    finally:
        reconcile.artifact_files = saved
    assert e.value.code == 1
    assert "marker vanished" in capsys.readouterr().err
    assert not reconcile.CURSOR_FILE.exists()

    reconcile.main()

    assert [x["artifact_path"] for x in _events(logs)] == [str(d / "a.md")]


def test_a_fifo_state_file_does_not_hang_the_run(recon):
    """Audit F-06: read_text() on a FIFO blocks forever. It is refused unopened, read as no
    state (every marker re-announced), and replaced by a regular file."""
    _, artifacts, logs = recon
    d = artifacts / "work" / "b1" / "scratch"
    d.mkdir(parents=True)
    (d / reconcile.IGNORE_MARKER).write_text("")
    os.mkfifo(reconcile.MARKERS_FILE)

    reconcile.main()  # would block in read_text()

    assert [e["summary"] for e in _events(logs)] == ["Ignored tree: work/b1/scratch"]
    assert reconcile.MARKERS_FILE.is_file() and not reconcile.MARKERS_FILE.is_symlink()


def test_a_linked_or_oversized_state_file_reads_as_none(recon, tmp_path):
    real = tmp_path / "elsewhere.json"
    real.write_text('["k:1"]')
    reconcile.MARKERS_FILE.symlink_to(real)
    assert reconcile.load_markers() == set()  # a link, even to a good file
    reconcile.MARKERS_FILE.unlink()
    reconcile.MARKERS_FILE.write_text('["k:1"]')
    assert reconcile.load_markers() == {"k:1"}  # the control
    reconcile.MARKERS_FILE.write_text(" " * (reconcile.MARKERS_MAX_BYTES + 1) + "[]")
    assert reconcile.load_markers() == set()


def test_save_markers_does_not_write_through_a_planted_tmp_link(recon, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    tmp = reconcile.MARKERS_FILE.with_name(reconcile.MARKERS_FILE.name + ".tmp")
    tmp.symlink_to(victim)

    reconcile.save_markers({"k:1"})

    assert victim.read_text() == "keep"
    assert json.loads(reconcile.MARKERS_FILE.read_text()) == ["k:1"]
    assert not reconcile.MARKERS_FILE.is_symlink()


def test_a_marker_whose_event_failed_is_announced_next_run(recon, monkeypatch):
    _, artifacts, logs = recon
    d = artifacts / "work" / "b1" / "scratch"
    d.mkdir(parents=True)
    (d / reconcile.IGNORE_MARKER).write_text("")
    real = reconcile.append_event

    def failing(event, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(reconcile, "append_event", failing)
    with pytest.raises(SystemExit):
        reconcile.main()
    monkeypatch.setattr(reconcile, "append_event", real)
    reconcile.main()

    assert [x["summary"] for x in _events(logs)] == ["Ignored tree: work/b1/scratch"]
