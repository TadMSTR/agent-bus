"""Signature policy, replay rejection, and credential redaction.

Covers findings E (enforce was a no-op), F (replay), and H (userinfo leak).
"""

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import server as ab


def _keypair():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, base64.b64encode(public).decode()


def _register(comms_dir, source, pubkey_b64):
    path = comms_dir / "agent-keys.json"
    registry = json.loads(path.read_text()) if path.exists() else {}
    registry[source] = {"pubkey": pubkey_b64}
    path.write_text(json.dumps(registry))


def _sign(private, *, event_type, source, summary, scope="cross-agent", metadata=None):
    """Mirror of scoped-mcp's signing_hook canonicalization."""
    metadata = dict(metadata or {})
    payload = {
        "event_type": event_type,
        "source": source,
        "summary": summary,
        "scope": scope,
        "target": None,
        "artifact_path": None,
        "metadata": {k: v for k, v in metadata.items() if k not in ("sig", "prev_hash")},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    metadata["sig"] = base64.b64encode(private.sign(canonical.encode())).decode()
    return metadata


# ── enforce actually enforces ─────────────────────────────────────────────────


def test_unsigned_event_rejected_in_enforce_mode(comms_dir, monkeypatch):
    _, pubkey = _keypair()
    _register(comms_dir, "research", pubkey)
    monkeypatch.setattr(ab, "VERIFY_SIGNATURES", "enforce")

    result = ab.log_event(event_type="task.completed", source="research", summary="s")

    assert result["logged"] is False
    assert result["error"] == "event_unsigned"
    # Rejected means not written — not written-and-flagged.
    assert not list((comms_dir / "logs").glob("*.jsonl"))


def test_unregistered_source_rejected_in_enforce_mode(comms_dir, monkeypatch):
    _, pubkey = _keypair()
    _register(comms_dir, "research", pubkey)
    monkeypatch.setattr(ab, "VERIFY_SIGNATURES", "enforce")

    result = ab.log_event(event_type="task.completed", source="attacker", summary="s")

    assert result["logged"] is False
    assert result["error"] == "source_not_registered"
    assert not list((comms_dir / "logs").glob("*.jsonl"))


def test_registry_entry_without_pubkey_rejected_in_enforce_mode(comms_dir, monkeypatch):
    (comms_dir / "agent-keys.json").write_text(json.dumps({"research": {"added": "x"}}))
    monkeypatch.setattr(ab, "VERIFY_SIGNATURES", "enforce")

    result = ab.log_event(event_type="task.completed", source="research", summary="s")

    assert result["logged"] is False
    assert result["error"] == "registry_entry_has_no_pubkey"


def test_validly_signed_event_accepted_in_enforce_mode(comms_dir, monkeypatch):
    private, pubkey = _keypair()
    _register(comms_dir, "research", pubkey)
    monkeypatch.setattr(ab, "VERIFY_SIGNATURES", "enforce")

    metadata = _sign(private, event_type="task.completed", source="research", summary="s")
    result = ab.log_event(
        event_type="task.completed", source="research", summary="s", metadata=metadata
    )

    assert result["logged"] is True


def test_tampered_signature_rejected_in_enforce_mode(comms_dir, monkeypatch):
    private, pubkey = _keypair()
    _register(comms_dir, "research", pubkey)
    monkeypatch.setattr(ab, "VERIFY_SIGNATURES", "enforce")

    metadata = _sign(private, event_type="task.completed", source="research", summary="s")
    result = ab.log_event(
        event_type="task.completed",
        source="research",
        summary="TAMPERED",  # not what was signed
        metadata=metadata,
    )

    assert result["logged"] is False
    assert result["error"] == "signature_invalid"


# ── warn stays permissive ─────────────────────────────────────────────────────


@pytest.mark.parametrize("source", ["research", "attacker"])
def test_warn_mode_accepts_unsigned_and_unregistered(comms_dir, monkeypatch, source):
    _, pubkey = _keypair()
    _register(comms_dir, "research", pubkey)
    monkeypatch.setattr(ab, "VERIFY_SIGNATURES", "warn")

    result = ab.log_event(event_type="task.completed", source=source, summary="s")

    assert result["logged"] is True


def test_warn_mode_still_rejects_a_bad_signature(comms_dir, monkeypatch):
    """An invalid signature is an assertion that failed, not a missing one."""
    private, pubkey = _keypair()
    _register(comms_dir, "research", pubkey)
    monkeypatch.setattr(ab, "VERIFY_SIGNATURES", "warn")

    metadata = _sign(private, event_type="task.completed", source="research", summary="s")
    result = ab.log_event(
        event_type="task.completed", source="research", summary="different", metadata=metadata
    )

    assert result["logged"] is False


# ── replay ────────────────────────────────────────────────────────────────────


def _signed_v2(private, *, summary="s", sig_ts=None, nonce=None, omit=()):
    metadata = {"sig_v": 2}
    if "sig_ts" not in omit:
        metadata["sig_ts"] = sig_ts or datetime.now(timezone.utc).isoformat()
    if "sig_nonce" not in omit:
        metadata["sig_nonce"] = nonce or str(uuid.uuid4())
    return _sign(
        private,
        event_type="task.completed",
        source="research",
        summary=summary,
        metadata=metadata,
    )


def test_replayed_event_rejected(comms_dir, monkeypatch):
    """The same signed event, captured and re-sent verbatim, must not be accepted twice."""
    private, pubkey = _keypair()
    _register(comms_dir, "research", pubkey)
    monkeypatch.setattr(ab, "VERIFY_SIGNATURES", "enforce")

    metadata = _signed_v2(private)
    kwargs = {
        "event_type": "task.completed",
        "source": "research",
        "summary": "s",
        "metadata": metadata,
    }

    first = ab.log_event(**kwargs)
    replay = ab.log_event(**kwargs)  # byte-identical capture

    assert first["logged"] is True
    assert replay["logged"] is False
    assert replay["error"] == "nonce_reused"


def test_stale_signature_rejected(comms_dir, monkeypatch):
    private, pubkey = _keypair()
    _register(comms_dir, "research", pubkey)
    monkeypatch.setattr(ab, "VERIFY_SIGNATURES", "enforce")

    old = (datetime.now(timezone.utc) - timedelta(seconds=ab.SIG_MAX_AGE_SECONDS + 60)).isoformat()
    metadata = _signed_v2(private, sig_ts=old)

    result = ab.log_event(
        event_type="task.completed", source="research", summary="s", metadata=metadata
    )

    assert result["logged"] is False
    assert result["error"] == "sig_expired"


def test_v2_signature_missing_replay_fields_rejected(comms_dir, monkeypatch):
    private, pubkey = _keypair()
    _register(comms_dir, "research", pubkey)
    monkeypatch.setattr(ab, "VERIFY_SIGNATURES", "enforce")

    metadata = _signed_v2(private, omit=("sig_nonce",))
    result = ab.log_event(
        event_type="task.completed", source="research", summary="s", metadata=metadata
    )

    assert result["logged"] is False
    assert result["error"] == "replay_fields_missing"


def test_v1_signature_still_accepted(comms_dir, monkeypatch):
    """Existing signers predate replay fields and must keep working."""
    private, pubkey = _keypair()
    _register(comms_dir, "research", pubkey)
    monkeypatch.setattr(ab, "VERIFY_SIGNATURES", "enforce")

    metadata = _sign(private, event_type="task.completed", source="research", summary="s")
    assert "sig_v" not in metadata

    result = ab.log_event(
        event_type="task.completed", source="research", summary="s", metadata=metadata
    )
    assert result["logged"] is True


# ── credential redaction ──────────────────────────────────────────────────────


def test_get_status_strips_url_userinfo(comms_dir, monkeypatch):
    monkeypatch.setattr(ab, "NATS_URL", "nats://agent-bus:hunter2@nats.example.com:4222")

    status = ab.get_status()
    url = status["integrations"]["nats"]["url"]

    assert "hunter2" not in url
    assert "agent-bus:" not in url
    assert url == "nats://nats.example.com:4222"


def test_strip_credentials_keeps_host_port_and_drops_query():
    assert ab._strip_credentials("https://u:p@h.example:8443/x?token=abc#f") == (
        "https://h.example:8443/x"
    )


def test_strip_credentials_handles_ipv6():
    assert ab._strip_credentials("nats://u:p@[::1]:4222") == "nats://[::1]:4222"


def test_strip_credentials_passes_through_none():
    assert ab._strip_credentials(None) is None
    assert ab._strip_credentials("") is None


# ── verify_chain arithmetic ───────────────────────────────────────────────────


def _write_raw(comms_dir, events):
    from datetime import datetime as _dt

    date = _dt.now(timezone.utc).strftime("%Y-%m-%d")
    path = comms_dir / "logs" / f"{date}-cross-agent.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return date


def test_verified_count_does_not_double_subtract(comms_dir):
    """An event that is both unsigned *and* chain-broken is one bad event, not two.

    `verified = total - chain_breaks - sig_failures - unsigned_events` subtracted such
    an event twice and could go negative.
    """
    _, pubkey = _keypair()
    _register(comms_dir, "research", pubkey)

    date = _write_raw(
        comms_dir,
        [
            {"id": "1", "ts": "t", "source": "research", "summary": "a", "metadata": {}},
            {
                "id": "2",
                "ts": "t",
                "source": "research",
                "summary": "b",
                "metadata": {},
                "prev_hash": "deadbeef",  # wrong
            },
        ],
    )

    result = ab.verify_chain(scope="cross-agent", date=date)

    assert result["total_events"] == 2
    assert result["chain_breaks"] == 1
    assert result["unsigned_events"] == 2
    assert result["verified"] == 0
    assert result["verified"] >= 0  # the old formula produced -1


def test_verified_counts_clean_events(comms_dir):
    ab.log_event(event_type="task.completed", source="dev", summary="a")
    ab.log_event(event_type="task.completed", source="dev", summary="b")

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = ab.verify_chain(scope="cross-agent", date=date)

    assert result["verified"] == 2
    assert result["chain_breaks"] == 0


def test_unparseable_line_counts_once(comms_dir):
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = comms_dir / "logs" / f"{date}-cross-agent.jsonl"
    path.write_text('{"id": "1", "ts": "t", "source": "x"}\nnot json at all\n')

    result = ab.verify_chain(scope="cross-agent", date=date)

    assert result["total_events"] == 2
    assert result["verified"] == 1
