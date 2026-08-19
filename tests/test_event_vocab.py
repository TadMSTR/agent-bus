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


def test_both_writers_route_an_unknown_type_to_session(comms_dir):
    server_result = ab.log_event(
        event_type="memory.written", source="s", summary="x", scope="session"
    )
    client_event = agent_bus_client.log_event(
        event_type="memory.written", source="s", summary="x", scope="session"
    )

    assert server_result["scope"] == "session"
    assert client_event["scope"] == "session"


def test_the_vocabulary_has_exactly_one_definition():
    """A re-introduced local copy would rebind these to a different object."""
    assert ab.CROSS_AGENT_EVENTS is event_vocab.CROSS_AGENT_EVENTS
    assert agent_bus_client.CROSS_AGENT_EVENTS is event_vocab.CROSS_AGENT_EVENTS


def test_vocabulary_is_immutable():
    """A frozenset cannot be mutated by one importer on behalf of the others."""
    with pytest.raises(AttributeError):
        event_vocab.CROSS_AGENT_EVENTS.add("nope")
