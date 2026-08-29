"""Vocabulary parity between the two writers.

Covers finding G. ``agent_bus_client.py`` carried its own copy of CROSS_AGENT_EVENTS
and it had gone stale — missing ``preflight.*``, ``build.*``, ``deploy.*`` and
``security.finding``. Non-MCP callers logging ``build.completed`` therefore landed in
the *session* file: invisible to ``query_events(scope="cross-agent")``, never
federated, and silently breaking the "On Session Resume" query that asks for exactly
those types.

A note on what these can and cannot catch, because it is easy to overclaim here.

Routing is now decided in one place — ``event_log.build_event`` — so the two writers
*cannot* disagree about scope any more, and a test that logs through both and compares
the result will pass even against a deliberately stale local copy (verified by
reintroducing one). Those parametrized cases are a specification of intended routing,
not a divergence detector.

The two guards that do bite are the identity assertion (a reintroduced literal is a
different object, since frozenset literals are not interned) and the explicit content
check for the seven types whose absence caused the original bug.
"""

import logging
import re

import pytest

import agent_bus_client
import event_vocab
import server as ab

# The types whose absence from the client's copy caused the silent breakage.
REGRESSION_TYPES = [
    "preflight.started",
    "preflight.completed",
    "build.started",
    "build.completed",
    "deploy.started",
    "deploy.completed",
    "security.finding",
]


@pytest.mark.parametrize("event_type", sorted(event_vocab.CROSS_AGENT_EVENTS))
def test_both_writers_route_every_cross_agent_type_identically(comms_dir, event_type):
    server_result = ab.log_event(event_type=event_type, source="s", summary="x")
    client_event = agent_bus_client.log_event(event_type=event_type, source="s", summary="x")

    assert server_result["scope"] == "cross-agent"
    assert client_event["scope"] == "cross-agent"


@pytest.mark.parametrize("event_type", REGRESSION_TYPES)
def test_the_previously_missing_types_are_in_the_vocabulary(event_type):
    """The durable guard: these seven are what the client's stale copy lacked.

    Deleting one here is the regression, and this is what catches it.
    """
    assert event_type in event_vocab.CROSS_AGENT_EVENTS


@pytest.mark.parametrize("event_type", REGRESSION_TYPES)
def test_client_routes_the_previously_missing_types_to_cross_agent(comms_dir, event_type):
    """These are the exact types that used to land in the session file."""
    event = agent_bus_client.log_event(
        event_type=event_type, source="dispatcher", summary="x", scope="session"
    )

    assert event["scope"] == "cross-agent"
    assert list((comms_dir / "logs").glob("*-cross-agent.jsonl"))
    assert not list((comms_dir / "logs").glob("*-session.jsonl"))


def test_both_writers_route_an_unknown_type_to_cross_agent(comms_dir):
    """Retargeted, not deleted — this used to assert the vikunja#560 defect.

    The old version asserted that an unknown type diverted to the SESSION log when the
    caller asked for it. That is the behaviour Phase 5.1 removes: it is how an
    undeclared type disappears from every `query_events(scope="cross-agent")` and from
    federation, with nothing anywhere reporting a problem. The parity property the test
    exists for — the two writers agree — is preserved; only the expected destination
    changed, and it changed because the destination was wrong.
    """
    server_result = ab.log_event(
        event_type="memory.written", source="s", summary="x", scope="session"
    )
    client_event = agent_bus_client.log_event(
        event_type="memory.written", source="s", summary="x", scope="session"
    )

    assert server_result["scope"] == "cross-agent"
    assert client_event["scope"] == "cross-agent"
    assert not list((comms_dir / "logs").glob("*-session.jsonl"))


@pytest.mark.parametrize("event_type", sorted(event_vocab.SESSION_EVENTS))
def test_both_writers_honour_session_scope_for_a_declared_session_type(comms_dir, event_type):
    """SESSION_EVENTS is what keeps "unknown routes cross-agent" from being a flood.

    Without it, `tool.called` — 17,145 of the 17,671 events in the live session corpus
    — would be undeclared, and so would be redirected into the cross-agent log. That
    would fix a silent divert by adding a loud one.
    """
    server_result = ab.log_event(event_type=event_type, source="s", summary="x", scope="session")
    client_event = agent_bus_client.log_event(
        event_type=event_type, source="s", summary="x", scope="session"
    )

    assert server_result["scope"] == "session"
    assert client_event["scope"] == "session"


# The session types by name, and WHY each one has to be here. Spelled out rather than
# derived from SESSION_EVENTS, because a test parametrized over the set cannot notice a
# member leaving it — deleting an entry just deletes a test case and the suite stays
# green. This is the same reason REGRESSION_TYPES above is a literal list.
SESSION_TYPES_AND_THEIR_EMITTERS = [
    # 17,145 of the 17,671 events in the live session corpus. Undeclared, every one of
    # them would be redirected into the cross-agent log by the unknown-type rule.
    ("tool.called", "scoped_mcp/audit.py"),
    ("workspace.drift", "~/scripts/agent-workspace-scan.py, scope='session'"),
    ("workspace.healed", "~/scripts/agent-workspace-scan.py, scope='session'"),
]


@pytest.mark.parametrize("event_type,emitter", SESSION_TYPES_AND_THEIR_EMITTERS)
def test_each_known_session_emitter_is_declared(event_type, emitter):
    """Removing one of these is what turns the #560 fix into a cross-agent flood."""
    assert event_type in event_vocab.SESSION_EVENTS, (
        f"{event_type} is emitted by {emitter} with scope='session'; undeclared, the "
        f"unknown-type rule would route every one of them cross-agent instead"
    )


def test_the_two_vocabularies_do_not_overlap():
    """A type in both sets would have two contradictory routing rules, and
    resolve_scope's ordering would silently pick one."""
    assert not (event_vocab.CROSS_AGENT_EVENTS & event_vocab.SESSION_EVENTS)


# ── AGENT_BUS_STRICT_VOCAB (Phase 5.1) ────────────────────────────────────────
#
# THE POINT OF `warn` IS THAT IT STILL FIXES THE ROUTING. A mode that warned about the
# divert while still diverting would report the problem and leave it in place, which is
# strictly worse than silence — it makes the log look supervised. Every case below
# asserts the destination, not just the verdict.


@pytest.mark.parametrize("mode", ["off", "warn", "enforce"])
def test_a_declared_type_is_accepted_in_every_mode(comms_dir, monkeypatch, mode):
    monkeypatch.setattr(ab, "STRICT_VOCAB", mode)
    monkeypatch.setattr(agent_bus_client, "STRICT_VOCAB", mode)

    assert ab.log_event(event_type="task.completed", source="s", summary="x")["logged"] is True
    assert agent_bus_client.log_event(event_type="task.completed", source="s", summary="x")


def test_warn_accepts_an_unknown_type_and_still_routes_it_cross_agent(
    comms_dir, monkeypatch, caplog
):
    monkeypatch.setattr(ab, "STRICT_VOCAB", "warn")

    with caplog.at_level(logging.WARNING, logger="agent-bus"):
        result = ab.log_event(
            event_type="not.a.real.type", source="s", summary="x", scope="session"
        )

    assert result["logged"] is True
    assert result["scope"] == "cross-agent"
    assert any("not.a.real.type" in r.getMessage() for r in caplog.records)
    assert list((comms_dir / "logs").glob("*-cross-agent.jsonl"))


def test_warn_warns_on_the_client_path_too(comms_dir, monkeypatch, caplog):
    """The dispatcher writes through the client, so a gate only in server.py would
    leave the very emitter this was built for ungated."""
    monkeypatch.setattr(agent_bus_client, "STRICT_VOCAB", "warn")

    with caplog.at_level(logging.WARNING, logger="agent-bus.client"):
        event = agent_bus_client.log_event(
            event_type="not.a.real.type", source="dispatcher", summary="x", scope="session"
        )

    assert event["scope"] == "cross-agent"
    assert any("not.a.real.type" in r.getMessage() for r in caplog.records)


def test_enforce_rejects_an_unknown_type_on_the_server(comms_dir, monkeypatch):
    monkeypatch.setattr(ab, "STRICT_VOCAB", "enforce")

    result = ab.log_event(event_type="not.a.real.type", source="s", summary="x")

    assert result["logged"] is False
    assert "not.a.real.type" in result["error"]
    assert not list((comms_dir / "logs").glob("*.jsonl"))


def test_enforce_raises_on_the_client(comms_dir, monkeypatch):
    """A sentinel return would be ignorable; log_event's contract is "the event I
    wrote", so the only honest way to say "I wrote nothing" is to raise."""
    monkeypatch.setattr(agent_bus_client, "STRICT_VOCAB", "enforce")

    with pytest.raises(agent_bus_client.VocabularyError, match=re.escape("not.a.real.type")):
        agent_bus_client.log_event(event_type="not.a.real.type", source="s", summary="x")

    assert not list((comms_dir / "logs").glob("*.jsonl"))


def test_off_is_silent_but_still_routes_cross_agent(comms_dir, monkeypatch, caplog):
    """`off` disables the REPORT, not the routing fix. The divert was the bug."""
    monkeypatch.setattr(ab, "STRICT_VOCAB", "off")

    with caplog.at_level(logging.WARNING, logger="agent-bus"):
        result = ab.log_event(
            event_type="not.a.real.type", source="s", summary="x", scope="session"
        )

    assert result["logged"] is True
    assert result["scope"] == "cross-agent"
    assert not [r for r in caplog.records if "vocabulary_policy" in r.getMessage()]


def test_an_unrecognised_mode_is_treated_as_warn_not_as_off(comms_dir, monkeypatch):
    """A typo in a policy variable must not silently disable the policy.

    `AGENT_BUS_STRICT_VOCAB=enfroce` failing open would be the same shape of defect as
    the one this gate closes: a check that reports nothing whether or not it ran.
    """
    assert event_vocab.unknown_event_reason("not.a.real.type", "enfroce") is not None
    assert event_vocab.unknown_event_reason("task.completed", "enfroce") is None


def test_the_default_mode_is_warn():
    """Shipping `enforce` would drop live events from ~40 still-undeclared types."""
    assert event_vocab.STRICT_VOCAB == "warn"
    assert ab.STRICT_VOCAB == "warn"
    assert agent_bus_client.STRICT_VOCAB == "warn"


def test_the_vocabulary_has_exactly_one_definition():
    """A re-introduced local copy would rebind these to a different object."""
    assert ab.CROSS_AGENT_EVENTS is event_vocab.CROSS_AGENT_EVENTS
    assert agent_bus_client.CROSS_AGENT_EVENTS is event_vocab.CROSS_AGENT_EVENTS


def test_vocabulary_is_immutable():
    """A frozenset cannot be mutated by one importer on behalf of the others."""
    with pytest.raises(AttributeError):
        event_vocab.CROSS_AGENT_EVENTS.add("nope")
