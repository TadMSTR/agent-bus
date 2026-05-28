import sys
from pathlib import Path

# Add repo root so tests can import server directly
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import server as ab


@pytest.fixture
def comms_dir(tmp_path, monkeypatch):
    """Isolated LOGS_DIR / COMMS_DIR for each test."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setattr(ab, "COMMS_DIR", tmp_path)
    monkeypatch.setattr(ab, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(ab, "CURSOR_FILE", tmp_path / "federation-cursor.json")
    monkeypatch.setattr(ab, "KEY_REGISTRY_PATH", tmp_path / "agent-keys.json")
    # Disable ntfy / nats / webhook side-effects
    monkeypatch.setattr(ab, "NTFY_URL", "")
    monkeypatch.setattr(ab, "NATS_URL", "")
    monkeypatch.setattr(ab, "WEBHOOK_URL", "")
    return tmp_path
