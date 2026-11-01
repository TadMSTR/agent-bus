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


def test_the_skip_is_never_silent(recon):
    """OV-40: a marker anywhere, the root included, is itself on the bus."""
    _, artifacts, logs = recon
    (artifacts / "a.md").write_text("x")
    (artifacts / reconcile.IGNORE_MARKER).write_text("")

    reconcile.main()

    events = _events(logs)
    assert [e["summary"] for e in events] == ["Ignored tree: ."]


def test_a_dangling_link_marker_is_logged(recon):
    _, artifacts, logs = recon
    d = artifacts / "scratch"
    d.mkdir()
    (d / "a.md").write_text("x")
    (d / reconcile.IGNORE_MARKER).symlink_to(d / "nowhere")

    reconcile.main()

    assert [e["summary"] for e in _events(logs)] == ["Ignored tree: scratch"]


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
    d = artifacts / "scratch"
    d.mkdir()
    (d / "a.md").write_text("x")
    _backdate(d / "a.md")
    (d / reconcile.IGNORE_MARKER).write_text("")
    _backdate(d / reconcile.IGNORE_MARKER)

    reconcile.main()

    assert [e["summary"] for e in _events(logs)] == ["Ignored tree: scratch"]


def test_an_old_file_renamed_onto_the_marker_is_announced(recon):
    _, artifacts, logs = recon
    d = artifacts / "scratch"
    d.mkdir()
    old = d / "old.md"
    old.write_text("x")
    reconcile.main()  # logs old.md, sets the cursor
    _backdate(old)
    old.rename(d / reconcile.IGNORE_MARKER)

    reconcile.main()

    assert [e["summary"] for e in _events(logs)][-1] == "Ignored tree: scratch"


def test_a_replaced_marker_is_announced_again(recon):
    _, artifacts, logs = recon
    d = artifacts / "scratch"
    d.mkdir()
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
    d = artifacts / "scratch"
    d.mkdir()
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


def test_one_bad_item_does_not_stop_the_scan(recon, capsys):
    """OV-33: a non-UTF-8 name can't be encoded into the log. It is reported, the rest is
    logged, and the run fails loudly with the cursor held."""
    _, artifacts, logs = recon
    os.close(os.open(os.fsencode(artifacts) + b"/bad\xff.md", os.O_CREAT | os.O_WRONLY, 0o600))
    (artifacts / "zz-good.md").write_text("x")

    with pytest.raises(SystemExit) as e:
        reconcile.main()

    assert e.value.code == 1
    assert [x["artifact_path"] for x in _events(logs)] == [str(artifacts / "zz-good.md")]
    err = capsys.readouterr().err
    assert "could not log" in err and "bad\\xff.md" in err
    assert not reconcile.CURSOR_FILE.exists()


def test_a_file_that_vanishes_mid_scan_is_skipped(recon, monkeypatch):
    _, artifacts, logs = recon
    (artifacts / "a.md").write_text("x")
    real = reconcile.artifact_files

    def files(errors):
        yield artifacts / "gone.md", False
        yield artifacts / "gone" / reconcile.IGNORE_MARKER, True
        yield from real(errors)

    monkeypatch.setattr(reconcile, "artifact_files", files)
    reconcile.main()  # no SystemExit

    assert [x["artifact_path"] for x in _events(logs)] == [str(artifacts / "a.md")]
    assert reconcile.CURSOR_FILE.exists()


def test_a_marker_whose_event_failed_is_announced_next_run(recon, monkeypatch):
    _, artifacts, logs = recon
    d = artifacts / "scratch"
    d.mkdir()
    (d / reconcile.IGNORE_MARKER).write_text("")
    real = reconcile.append_event

    def failing(event, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(reconcile, "append_event", failing)
    with pytest.raises(SystemExit):
        reconcile.main()
    monkeypatch.setattr(reconcile, "append_event", real)
    reconcile.main()

    assert [x["summary"] for x in _events(logs)] == ["Ignored tree: scratch"]
