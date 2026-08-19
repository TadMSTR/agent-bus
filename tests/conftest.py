import sys
from pathlib import Path

# Add repo root so tests can import server directly
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import agent_bus_client
import server as ab


class StubPublisher:
    """Stands in for NatsPublisher — records instead of connecting.

    The real publisher is inert without a credential, but relying on that would make
    every test depend on the ambient environment. This makes the behaviour explicit and
    lets individual tests choose to refuse, disconnect, or raise.
    """

    def __init__(self, enabled=True, connected=True, accept=True, raises=False):
        self._enabled = enabled
        self._connected = connected
        self.accept = accept
        self.raises = raises
        self.published: list[dict] = []

    @property
    def enabled(self):
        return self._enabled

    @property
    def connected(self):
        return self._connected

    def publish(self, event):
        if self.raises:
            raise RuntimeError("nats exploded")
        if not self.accept:
            return False
        self.published.append(event)
        return True

    def start(self):
        return True

    def close(self, timeout=5.0):
        pass

    def stats(self):
        return {"enabled": self._enabled, "connected": self._connected}


@pytest.fixture
def publisher(monkeypatch):
    """Replace the module-level NATS publisher with a controllable stub."""
    stub = StubPublisher()
    monkeypatch.setattr(ab, "_publisher", stub)
    return stub


@pytest.fixture
def comms_dir(tmp_path, monkeypatch, publisher):
    """Isolated LOGS_DIR / COMMS_DIR for each test."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setattr(ab, "COMMS_DIR", tmp_path)
    monkeypatch.setattr(ab, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(ab, "CURSOR_FILE", tmp_path / "federation-cursor.json")
    monkeypatch.setattr(ab, "FEDERATION_LOCK_FILE", tmp_path / ".federation.lock")
    monkeypatch.setattr(ab, "KEY_REGISTRY_PATH", tmp_path / "agent-keys.json")
    # agent_bus_client resolves its own paths — point it at the same tmpdir so
    # mixed-writer tests exercise one file rather than two.
    monkeypatch.setattr(agent_bus_client, "LOGS_DIR", logs_dir)
    # Disable ntfy / webhook side-effects. NATS goes through the stub publisher.
    monkeypatch.setattr(ab, "NTFY_URL", "")
    monkeypatch.setattr(ab, "WEBHOOK_URL", "")
    # Replay state is module-global; a leaked nonce would couple tests together.
    ab._seen_nonces.clear()
    yield tmp_path
    # The federation lock fd is a module global held for the process lifetime — a test
    # that acquires it would otherwise hand the next test a false "I am the leader".
    ab.release_federation_lock()
