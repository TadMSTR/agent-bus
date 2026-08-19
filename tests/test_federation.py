"""Federation cursor, replay-storm prevention, and single-process election.

Covers finding B: the v1 cursor held one (file, offset) pair while the loop iterated
every daily file, so each cycle restarted ~70 files at offset 0 and republished the
whole ~2,700-event history.
"""

import fcntl
import json

import server as ab


def _write_log(logs_dir, date, count, start=0):
    """Write a cross-agent log file and return (path, size)."""
    path = logs_dir / f"{date}-cross-agent.jsonl"
    with open(path, "w") as f:
        for i in range(count):
            event = {"id": f"{date}-{start + i}", "ts": f"{date}T00:00:00+00:00"}
            f.write(json.dumps(event) + "\n")
    return path, path.stat().st_size


# ── the storm ─────────────────────────────────────────────────────────────────


def test_completed_files_are_not_republished(comms_dir, publisher):
    """The defect, stated directly: a full cycle must not re-emit federated files."""
    logs_dir = comms_dir / "logs"
    p1, s1 = _write_log(logs_dir, "2026-08-01", 3)
    p2, s2 = _write_log(logs_dir, "2026-08-02", 3)

    cursor = {
        "version": 2,
        "hostname": "test",
        "files": {str(p1): s1, str(p2): s2},
    }
    _, published = ab.federate_once(cursor)

    assert published == 0
    assert publisher.published == []


def test_only_unfederated_tail_is_published(comms_dir, publisher):
    logs_dir = comms_dir / "logs"
    p1, s1 = _write_log(logs_dir, "2026-08-01", 3)
    _write_log(logs_dir, "2026-08-02", 3)

    cursor = {"version": 2, "hostname": "test", "files": {str(p1): s1}}
    _, published = ab.federate_once(cursor)

    assert published == 3
    assert [e["id"] for e in publisher.published] == [
        "2026-08-02-0",
        "2026-08-02-1",
        "2026-08-02-2",
    ]


def test_second_cycle_publishes_nothing_new(comms_dir, publisher):
    logs_dir = comms_dir / "logs"
    _write_log(logs_dir, "2026-08-01", 2)

    cursor, first = ab.federate_once({"version": 2, "files": {}})
    _, second = ab.federate_once(cursor)

    assert first == 2
    assert second == 0


# ── v1 → v2 migration ─────────────────────────────────────────────────────────


def test_migration_marks_older_files_federated(comms_dir):
    logs_dir = comms_dir / "logs"
    p1, s1 = _write_log(logs_dir, "2026-08-01", 3)
    p2, s2 = _write_log(logs_dir, "2026-08-02", 3)
    _write_log(logs_dir, "2026-08-03", 3)

    v1 = {"last_federated_file": str(p2), "last_federated_offset": s2}
    migrated = ab.migrate_cursor(v1, logs_dir)

    assert migrated["version"] == 2
    assert migrated["files"][str(p1)] == s1  # older → complete
    assert migrated["files"][str(p2)] == s2  # the cursor's own file
    # Newer file is absent, so it starts at 0 — it genuinely was not federated.
    assert str(logs_dir / "2026-08-03-cross-agent.jsonl") not in migrated["files"]


def test_migration_does_not_replay_history(comms_dir, publisher):
    """Migrating must not reproduce the exact failure it exists to fix."""
    logs_dir = comms_dir / "logs"
    for date in ("2026-08-01", "2026-08-02", "2026-08-03"):
        _write_log(logs_dir, date, 5)
    last = logs_dir / "2026-08-03-cross-agent.jsonl"

    v1 = {"last_federated_file": str(last), "last_federated_offset": last.stat().st_size}
    cursor = ab.migrate_cursor(v1, logs_dir)
    _, published = ab.federate_once(cursor)

    assert published == 0
    assert publisher.published == []


def test_migration_of_fresh_cursor_federates_everything(comms_dir, publisher):
    logs_dir = comms_dir / "logs"
    _write_log(logs_dir, "2026-08-01", 4)

    cursor = ab.migrate_cursor({"last_federated_file": None}, logs_dir)
    _, published = ab.federate_once(cursor)

    assert published == 4


def test_v2_cursor_passes_through_migration_unchanged(comms_dir):
    cursor = {"version": 2, "hostname": "h", "files": {"/x": 5}}
    assert ab.migrate_cursor(cursor, comms_dir / "logs") is cursor


def test_load_cursor_migrates_v1_on_disk(comms_dir):
    logs_dir = comms_dir / "logs"
    p1, s1 = _write_log(logs_dir, "2026-08-01", 2)
    ab.CURSOR_FILE.write_text(
        json.dumps({"last_federated_file": str(p1), "last_federated_offset": s1})
    )

    cursor = ab.load_cursor()

    assert cursor["version"] == 2
    assert cursor["files"][str(p1)] == s1


def test_load_cursor_survives_corrupt_file(comms_dir):
    ab.CURSOR_FILE.write_text("{not json")
    cursor = ab.load_cursor()
    assert cursor["version"] == 2
    assert cursor["files"] == {}


# ── bounded work ──────────────────────────────────────────────────────────────


def test_cycle_stops_at_the_event_cap(comms_dir, publisher, monkeypatch):
    """A cursor reset must degrade to slower catch-up, not an 18-minute cycle."""
    monkeypatch.setattr(ab, "FEDERATION_MAX_EVENTS_PER_CYCLE", 5)
    logs_dir = comms_dir / "logs"
    _write_log(logs_dir, "2026-08-01", 50)

    cursor, published = ab.federate_once({"version": 2, "files": {}})

    assert published == 5
    # And the next cycle resumes rather than restarting.
    _, second = ab.federate_once(cursor)
    assert second == 5
    assert [e["id"] for e in publisher.published][:6] == [
        "2026-08-01-0",
        "2026-08-01-1",
        "2026-08-01-2",
        "2026-08-01-3",
        "2026-08-01-4",
        "2026-08-01-5",
    ]


def test_cursor_saved_per_file_not_after_the_whole_loop(comms_dir):
    """save_cursor() ran once after every file, so a mid-cycle restart lost progress."""
    logs_dir = comms_dir / "logs"
    _write_log(logs_dir, "2026-08-01", 2)
    _write_log(logs_dir, "2026-08-02", 2)

    saved = []
    original = ab.save_cursor

    def spy(cursor):
        saved.append(json.loads(json.dumps(cursor)))
        original(cursor)

    ab.save_cursor = spy
    try:
        ab.federate_once({"version": 2, "files": {}})
    finally:
        ab.save_cursor = original

    assert len(saved) == 2  # one per file, not one per cycle


# ── undeliverable events must not advance the cursor ──────────────────────────


def test_cursor_does_not_advance_when_publish_is_refused(comms_dir, publisher):
    """Otherwise the gap-fill guarantee is silently false."""
    logs_dir = comms_dir / "logs"
    p1, _ = _write_log(logs_dir, "2026-08-01", 4)
    publisher.accept = False

    cursor, published = ab.federate_once({"version": 2, "files": {}})

    assert published == 0
    assert cursor["files"].get(str(p1), 0) == 0

    # Once NATS is back, the same events are delivered.
    publisher.accept = True
    _, retried = ab.federate_once(cursor)
    assert retried == 4


# ── single-process election ───────────────────────────────────────────────────


def test_only_one_process_may_federate(comms_dir):
    """federation_loop is started from the lifespan, so every stdio child runs one."""
    lock_path = comms_dir / ".federation.lock"
    holder = open(lock_path, "w")  # noqa: SIM115 — must outlive the with-block below
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert ab.acquire_federation_lock(lock_path) is False
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_lock_is_acquired_when_free(comms_dir):
    lock_path = comms_dir / ".federation.lock"
    try:
        assert ab.acquire_federation_lock(lock_path) is True
        assert ab.acquire_federation_lock(lock_path) is True  # idempotent
    finally:
        ab.release_federation_lock()


def test_lock_is_released_for_the_next_process(comms_dir):
    lock_path = comms_dir / ".federation.lock"
    assert ab.acquire_federation_lock(lock_path) is True
    ab.release_federation_lock()

    other = open(lock_path, "w")  # noqa: SIM115 — flock is released explicitly below
    try:
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
    finally:
        fcntl.flock(other.fileno(), fcntl.LOCK_UN)
        other.close()
