"""
event_vocab.py — the single source of truth for the event vocabulary.

``server.py`` and ``agent_bus_client.py`` previously each carried their own copy of
``CROSS_AGENT_EVENTS``, and the client's copy went stale: it was missing the seven
types added in PR #5 (``preflight.*``, ``build.*``, ``deploy.*``, ``security.finding``).
Non-MCP callers logging ``build.completed`` therefore landed in the *session* file,
invisible to ``query_events(scope="cross-agent")`` and never federated.

Both modules now import from here. ``tests/test_event_vocab.py`` asserts the routing
behaviour agrees between the two writers, so a re-introduced local literal is caught
rather than merely a diverged constant.
"""

CROSS_AGENT_EVENTS = frozenset(
    {
        "task.dispatched",
        "task.approved",
        "task.completed",
        "task.failed",
        "task.routing-failed",
        "handoff.created",
        "handoff.picked-up",
        "handoff.completed",
        "audit.requested",
        "audit.completed",
        "build-plan.created",
        "diagnose.started",
        "diagnose.completed",
        "artifact.untracked",
        "preflight.started",
        "preflight.completed",
        "build.started",
        "build.completed",
        "deploy.started",
        "deploy.completed",
        "security.finding",
    }
)

HIGH_PRIORITY_EVENTS = frozenset(
    {
        "audit.requested",
        "task.failed",
        "task.routing-failed",
        "handoff.created",
    }
)


def resolve_scope(event_type: str, scope: str) -> str:
    """Cross-agent event types always route to the cross-agent log."""
    return "cross-agent" if event_type in CROSS_AGENT_EVENTS else scope
