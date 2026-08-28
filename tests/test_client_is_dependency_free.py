"""The client import path must not touch the server's dependencies.

WHY THIS EXISTS

``agent_bus_client`` is the write path for code that cannot call MCP — PM2 cron jobs,
the task dispatcher. Those callers install this distribution purely to get ``log_event``.
Until v0.3.1 ``[project] dependencies`` hard-required ``fastmcp``, ``cryptography`` and
``nats-py``, none of which the client path imports, so every such caller dragged in a
web framework to append a line to a JSONL file.

v0.3.1 moved those three into the ``server`` extra and left ``dependencies = []``. That
is a *claim* about the import graph — ``agent_bus_client`` -> ``event_log`` ->
``event_vocab``, stdlib only — and it is one import statement away from being false at
any time. Someone adding ``from cryptography...`` to ``event_log`` for signing would not
break any existing test: the dev environment installs the server extra, so it would
import fine here and fail only on a caller's machine, at runtime, inside the
``except ImportError`` that task-dispatcher wraps the import in. That failure mode is
silent by construction — the dispatcher degrades to a no-op logger and reports success.
This is the third emitter on forge to die that way (vikunja#444, #436, #550).

So the check cannot be "is fastmcp installed" — in CI it is. It runs the import in a
child process with a meta-path finder that makes the three names unimportable, which
tests the graph rather than the environment, and then goes on to actually write and read
back an event so a client that imports cleanly but cannot log still fails.

The complementary check lives in CI: the `build` job installs the bare wheel with no
extras and imports the client modules, which tests the packaging metadata rather than
the import graph. Both are needed — this file cannot catch a dependency reappearing in
``[project] dependencies``, and that job cannot catch a new import of something already
installed for another reason.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# The three names the server needs and the client must not. `nats-py` imports as `nats`.
FORBIDDEN = ("fastmcp", "cryptography", "nats")

# Runs in a child with the forbidden names blocked at the finder level, so an import
# anywhere in the client's transitive graph raises rather than silently succeeding off
# the dev environment's site-packages.
_CHILD = '''
import sys, json

FORBIDDEN = {forbidden!r}

class Blocker:
    """Refuse the server's dependencies, at any depth, before any other finder sees them."""
    def find_module(self, name, path=None):
        return self.find_spec(name, path)
    def find_spec(self, name, path=None, target=None):
        root = name.split(".")[0]
        if root in FORBIDDEN:
            raise ImportError(f"blocked by test: {{name}}")
        return None

sys.meta_path.insert(0, Blocker())

# Anything already imported would bypass the finder entirely.
for mod in list(sys.modules):
    if mod.split(".")[0] in FORBIDDEN:
        del sys.modules[mod]

from agent_bus_client import log_event

# Two events, so the caller can assert the SECOND chains onto the first. One event
# proves only that a line was written; `prev_hash` is absent on the first line of a
# file by construction, so a single-event check cannot tell a chained append from a
# bare one.
events = [
    log_event(
        event_type="task.dispatched",
        source="test-client-dep-free",
        summary=f"written with the server deps unimportable ({{i}})",
        target="nobody",
        metadata={{"probe": True}},
    )
    for i in range(2)
]

leaked = sorted(m for m in sys.modules if m.split(".")[0] in FORBIDDEN)
print(json.dumps({{
    "event_ids": [e["id"] for e in events],
    "scope": events[0]["scope"],
    "leaked": leaked,
}}))
'''


def _run_client_child(tmp_path):
    """Import and use the client in a process where the server's deps cannot be imported."""
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(REPO_ROOT),
        # agent_bus_client resolves COMMS_DIR from this at import time.
        "AGENT_BUS_COMMS_DIR": str(tmp_path / "comms"),
        # Not tmp_path itself: the client writes to <COMMS_DIR>/logs and we assert on that
        # path, so a stray file elsewhere in tmp_path must not be able to satisfy it.
    }
    return subprocess.run(
        [sys.executable, "-c", _CHILD.format(forbidden=FORBIDDEN)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )


def test_client_imports_and_logs_without_the_server_dependencies(tmp_path):
    r = _run_client_child(tmp_path)
    assert r.returncode == 0, (
        "the client path imported one of the server's dependencies.\n"
        f"stdout: {r.stdout}\nstderr: {r.stderr}"
    )

    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["leaked"] == [], f"client pulled in server deps: {out['leaked']}"
    assert out["scope"] == "cross-agent"

    # The import succeeding and the write working are two different claims.
    logs_dir = tmp_path / "comms" / "logs"
    written = list(logs_dir.glob("*-cross-agent.jsonl"))
    assert written, f"no cross-agent log written under {logs_dir}"

    lines = [ln for ln in written[0].read_text().splitlines() if ln.strip()]
    records = [json.loads(ln) for ln in lines]
    assert [r_["id"] for r_ in records] == out["event_ids"]
    assert records[0]["source"] == "test-client-dep-free"
    assert records[0]["event"] == "task.dispatched"  # the wire field is `event`, not `event_type`

    # Written through event_log rather than appended raw: the second line chains onto
    # the first. This is the property that made the client worth keeping on the shared
    # append path, and it is checked here because a "just write a JSON line" rewrite
    # would satisfy every other assertion in this test.
    assert "prev_hash" not in records[0], "first line of a file has nothing to chain onto"
    assert records[1]["prev_hash"] == hashlib.sha256(lines[0].encode()).hexdigest()


def test_the_blocker_actually_blocks(tmp_path):
    """A finder that quietly stopped matching would make the test above vacuous.

    Without this, the whole file passes just as green if `Blocker.find_spec` were
    returning None for everything — which is exactly what a rename of the meta-path
    hook API, or a typo in FORBIDDEN, would produce.
    """
    child = _CHILD.format(forbidden=FORBIDDEN) + "\nimport fastmcp\n"
    r = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(REPO_ROOT),
            "AGENT_BUS_COMMS_DIR": str(tmp_path / "comms"),
        },
        cwd=str(tmp_path),
    )
    assert r.returncode != 0, "the blocker let a forbidden import through"
    assert "blocked by test: fastmcp" in r.stderr, r.stderr
