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

THE UNKNOWN-TYPE DEFECT (vikunja#560, plan Phase 5.1)
-----------------------------------------------------
``log_event()`` never validated ``event_type``. An unknown type kept the caller's
``scope`` argument, so it landed wherever that happened to point — for the default it
was the cross-agent log, which is why ``task.workflow_started`` reached the right file
for months despite being undeclared. That was luck, not routing: one caller passing an
explicit ``scope="session"`` would have diverted it to the session log, where no
``query_events(scope="cross-agent")`` and no federation would ever see it again.

The fix has two halves, and shipping only the second would have been theatre:

1. **An unknown type is never silently diverted.** ``resolve_scope`` now sends an
   undeclared type to the CROSS-AGENT log regardless of what the caller asked for.
   Unknown means "nobody has decided where this belongs", and the answer to that is
   the visible file, not the quiet one.
2. **It is reported.** ``AGENT_BUS_STRICT_VOCAB`` selects what happens on top of the
   routing, mirroring ``AGENT_BUS_VERIFY_SIGNATURES`` in both spelling and default.

``SESSION_EVENTS`` is what makes half 1 safe rather than catastrophic. It is not
decoration: ``tool.called`` alone accounts for 17,145 of the 17,671 events in the live
session corpus, and ``workspace.drift`` / ``workspace.healed`` are emitted by
``agent-workspace-scan.py`` through ``agent_bus_client`` with an explicit
``scope="session"``. Without a declared session vocabulary, "unknown routes
cross-agent" would empty the session log into the cross-agent one — fixing a silent
divert by adding a loud one.

A DECLARED TYPE IS NOT A BLESSED SPELLING
------------------------------------------
These sets were reconciled against the live corpus on 2026-08-29, which found roughly
forty types in the cross-agent log that are not declared here. Only the ones with a
*sanctioned emitter* — a skill, an agent CLAUDE.md, or repo code that names the string
— were added. The rest are left undeclared ON PURPOSE, because several are drift
rather than vocabulary: ``build.complete`` beside ``build.completed``,
``build.pre_audit`` beside ``build.pre_audit_dispatched``, ``plane.ticket.*`` from a
tracker retired in July. Declaring those would bless a typo and turn this gate into a
rubber stamp. ``warn`` mode exists to surface them; the fix for each is a decision
about its emitter, not an edit here.
"""

import os

CROSS_AGENT_EVENTS = frozenset(
    {
        "task.dispatched",
        "task.approved",
        "task.completed",
        "task.failed",
        "task.routing-failed",
        # Emitted by task-dispatcher's Temporal branch since v0.9.x and absent from this
        # set until now. It still reached the cross-agent log, but only because every
        # caller happens to leave `scope` at its "cross-agent" default and resolve_scope
        # returns that default for unknown types — the routing was incidental, not
        # declared. One caller passing an explicit scope would have diverted it silently.
        # (Underscore rather than the hyphen the rest of task.* uses; renaming it would
        # break consumers querying by event type and 3 months of records already on disk,
        # so it needs a migration rather than a drive-by rename — vikunja#553.)
        "task.workflow_started",
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
        # ── Declared 2026-08-29, from the corpus reconciliation described above. Each
        # has an emitter that names the string; the greps that found them are recorded
        # in the Phase 5 build report. They were reaching the cross-agent log already,
        # by the same accident `task.workflow_started` was — this makes the routing a
        # decision rather than a side effect of every caller using the default scope.
        "tracker.ticket.created",  # every agent CLAUDE.md, "Ticket Auto-Filing"
        "config.proposal.countersign_requested",  # steward-propose-change skill
        "config.proposal.countersigned",  # security-config-countersign skill
        "agent-workflow.changed",  # host-forge-runbooks/agent-workflow-change.md
        "update.cycle.completed",  # host-forge-runbooks/update-cycle.md
        "compact_qc.complete",  # ~/scripts/memory-compact-qc.sh
        "rollover_qc.complete",  # ~/scripts/harlock-rollover-qc.sh
    }
)

# Types that legitimately belong in the per-session log. Declared so that routing an
# UNKNOWN type to the cross-agent log cannot swallow them — see the module docstring.
#
# `tool.called` is listed for completeness of the vocabulary, not because this module
# gates it: scoped_mcp/audit.py builds the event dict and appends to the session JSONL
# itself rather than calling through agent_bus_client, so it is a writer that bypasses
# every check here. Declaring it keeps the vocabulary an honest description of the
# corpus. The bypass is tracked separately.
SESSION_EVENTS = frozenset(
    {
        "tool.called",  # scoped-mcp audit trail (direct writer — see above)
        "workspace.drift",  # ~/scripts/agent-workspace-scan.py
        "workspace.healed",  # ~/scripts/agent-workspace-scan.py
    }
)

KNOWN_EVENTS = CROSS_AGENT_EVENTS | SESSION_EVENTS

HIGH_PRIORITY_EVENTS = frozenset(
    {
        "audit.requested",
        "task.failed",
        "task.routing-failed",
        "handoff.created",
    }
)

# What to do about an event type that is in neither set. Mirrors
# AGENT_BUS_VERIFY_SIGNATURES — same three-value shape, same `warn` default, so the two
# policy knobs read the same way in ecosystem.config.js and in get_status().
#
#   off      — no check at all. The escape hatch, not the default.
#   warn     — log it and accept it. Routing is corrected either way.
#   enforce  — reject it; log_event returns {"logged": False, "error": ...}.
#
# SHIPPED AS `warn` DELIBERATELY. `enforce` is implemented and tested, but it is not
# reachable on this fleet yet: the corpus reconciliation above found ~40 undeclared
# types still in active use, and flipping to `enforce` today would start dropping real
# events from real agents. Watch what `warn` logs, resolve the emitters, then flip.
VOCAB_MODES = ("off", "warn", "enforce")
STRICT_VOCAB: str = os.environ.get("AGENT_BUS_STRICT_VOCAB") or "warn"


def is_known(event_type: str) -> bool:
    """True if the type is declared in either vocabulary."""
    return event_type in KNOWN_EVENTS


def unknown_event_reason(event_type: str, mode: str) -> str | None:
    """Why this event type should be reported, or None if there is nothing to say.

    Returns a reason string for an undeclared type whenever the mode is `warn` or
    `enforce`; the CALLER decides whether that reason becomes a log line or a
    rejection. Splitting it this way keeps one definition of "unknown" behind two
    behaviours, rather than two nearly-identical checks that can drift apart.

    `mode` is passed in rather than read from STRICT_VOCAB here, because both callers
    hold their own module-level copy that tests monkeypatch — reading the global would
    make this function ignore the mode its caller believes it is running under.

    AN UNRECOGNISED MODE IS TREATED AS `warn`, NOT AS `off`. A typo in a policy
    variable must not silently disable the policy; that is the same class of failure
    this module exists to close.
    """
    if mode == "off":
        return None
    if is_known(event_type):
        return None
    return (
        f"unknown event_type {event_type!r} — not in agent-bus's declared vocabulary. "
        f"Routed cross-agent so it stays visible. Add it to CROSS_AGENT_EVENTS or "
        f"SESSION_EVENTS in event_vocab.py, or fix the emitter's spelling."
    )


def resolve_scope(event_type: str, scope: str) -> str:
    """Where this event is written.

    Cross-agent types always route to the cross-agent log. Declared session types take
    the caller's scope — they are the ones whose owner has already decided. Anything
    UNDECLARED routes cross-agent no matter what the caller asked for: an unknown type
    is exactly the one nobody should be able to file away quietly.
    """
    if event_type in CROSS_AGENT_EVENTS:
        return "cross-agent"
    if event_type in SESSION_EVENTS:
        return scope
    return "cross-agent"
