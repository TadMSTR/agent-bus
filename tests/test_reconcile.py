"""reconcile.py — the artifact scanner, and its share of the hash chain.

It appends to the same JSONL as the server from a PM2 cron, so it is one of the two
writers that previously chained nothing and held no lock.
"""

import json
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
