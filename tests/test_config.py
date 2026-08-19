"""Environment parsing.

ecosystem.config.js passes every configured variable through, so an unset one arrives
as an empty string rather than being absent. `int("")` raises, and it would raise at
import time — putting the PM2 service into a restart loop and taking agent-bus down for
all seven agents at once.
"""

import subprocess
import sys
from pathlib import Path

import server as ab

REPO_ROOT = Path(__file__).parent.parent

NUMERIC_VARS = [
    "AGENT_BUS_FEDERATION_INTERVAL",
    "AGENT_BUS_FEDERATION_MAX_EVENTS",
    "AGENT_BUS_FEDERATION_MAX_SECONDS",
    "AGENT_BUS_SIG_MAX_AGE",
]


def _import_server_with(env_overrides, expression):
    """Import server.py in a clean process and evaluate an expression against it."""
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(REPO_ROOT),
        "HOME": str(Path.home()),
        **env_overrides,
    }
    result = subprocess.run(
        [sys.executable, "-c", f"import server; print({expression})"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_env_num_reads_a_value(monkeypatch):
    monkeypatch.setenv("X_NUM", "42")
    assert ab._env_num("X_NUM", 7, int) == 42


def test_env_num_falls_back_when_unset(monkeypatch):
    monkeypatch.delenv("X_NUM", raising=False)
    assert ab._env_num("X_NUM", 7, int) == 7


def test_env_num_falls_back_on_empty_string(monkeypatch):
    """The case PM2 actually produces."""
    monkeypatch.setenv("X_NUM", "")
    assert ab._env_num("X_NUM", 7, int) == 7


def test_env_num_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("X_NUM", "not-a-number")
    assert ab._env_num("X_NUM", 7, int) == 7


def test_env_num_handles_floats(monkeypatch):
    monkeypatch.setenv("X_NUM", "2.5")
    assert ab._env_num("X_NUM", 1.0, float) == 2.5


def test_server_imports_with_every_numeric_var_empty():
    """The regression, end to end: importing must not raise."""
    empty = {**dict.fromkeys(NUMERIC_VARS, ""), "AGENT_BUS_VERIFY_SIGNATURES": ""}

    assert _import_server_with(empty, "server.FEDERATION_INTERVAL") == "30"


def test_defaults_are_used_when_every_var_is_empty():
    empty = dict.fromkeys(NUMERIC_VARS, "")
    values = _import_server_with(
        empty,
        "(server.FEDERATION_MAX_EVENTS_PER_CYCLE, "
        "server.FEDERATION_MAX_SECONDS_PER_CYCLE, "
        "server.SIG_MAX_AGE_SECONDS)",
    )
    assert values == "(500, 10.0, 300)"


def test_federation_stays_enabled_when_the_flag_is_empty():
    assert _import_server_with({"AGENT_BUS_FEDERATION": ""}, "server.FEDERATION_ENABLED") == "True"


def test_federation_can_still_be_disabled():
    result = _import_server_with({"AGENT_BUS_FEDERATION": "0"}, "server.FEDERATION_ENABLED")
    assert result == "False"


def test_explicit_values_override_the_defaults():
    overrides = {"AGENT_BUS_FEDERATION_INTERVAL": "5", "AGENT_BUS_SIG_MAX_AGE": "60"}
    values = _import_server_with(
        overrides, "(server.FEDERATION_INTERVAL, server.SIG_MAX_AGE_SECONDS)"
    )
    assert values == "(5, 60)"
