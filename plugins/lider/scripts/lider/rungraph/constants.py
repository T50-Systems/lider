"""Graph tables, kinds, families, exit codes for the run ledger."""

SCHEMA_VERSION = 1

# --- the graph -------------------------------------------------------------
# node -> what may follow it. Everything the flow is allowed to do is here; if a
# transition is not in this table it does not happen.
# Construction (the default). Building and shipping one feature.
GRAPH = {
    "init":       ["spec"],
    # `implement` is the flat path, kept for a phase that is a single unit.
    # `plan` is the decomposed path: units run their own subgraphs and `join` is
    # the barrier that will not open until every one of them is terminal.
    "spec":       ["challenge", "preflight", "plan", "implement"],
    "challenge":  ["spec", "preflight", "plan", "implement"],
    "preflight":  ["plan", "implement", "blocked"],
    "plan":       ["join", "spec", "escalated"],
    "join":       ["verify", "plan", "escalated"],
    "implement":  ["review"],
    "review":     ["adjudicate"],
    "adjudicate": ["implement", "spec", "verify", "escalated"],  # the two loop-backs
    "verify":     ["commit", "adjudicate"],             # failed verification re-opens judgment
    "commit":     ["promote"],
    "promote":    ["effect"],
    "effect":     ["done"],
    "blocked":    ["spec", "plan", "preflight"],
    "escalated":  ["spec", "implement", "done"],
    "done":       [],
}

# Inception (optional separate run). Discovery only: pin a frame, declare
# criteria/questions/units, optionally challenge, then seal a handoff under
# .lider/handoffs/. No implement, no promote. Construction imports the handoff.
# RECOMMENDED before construction; required only in strict mode.
INCEPTION_GRAPH = {
    "init":      ["spec"],
    "spec":      ["challenge", "sealed"],
    "challenge": ["spec", "sealed"],
    "sealed":    [],
}

# Operations (optional separate run). Touch shared / deployed state: pin a target,
# record preflight (ternary), act, prove effect, optional soak, close — plus a
# checkable incident → rollback path when prove/soak fails.
# Complements construction's promote→effect leg: use this when the action is not
# "finish this feature ledger" but "may I touch prod / did it arrive / do we roll back".
# RECOMMENDED before/after shared-state changes; STRICT requires preflight ok
# before act, effect/prove ok before closed, and an incident signal before incident.
OPERATIONS_GRAPH = {
    "init":      ["scope"],
    "scope":     ["preflight", "blocked", "incident"],
    "preflight": ["act", "blocked"],
    "act":       ["prove", "blocked", "escalated", "incident"],
    "prove":     ["soak", "closed", "act", "escalated", "incident"],
    "soak":      ["closed", "act", "escalated", "incident"],
    # Incident is declared failure of effect/health — not a prose war room.
    # rollback = revert toward previous_ref; act = forward fix (hot patch).
    "incident":  ["rollback", "act", "escalated", "blocked"],
    "rollback":  ["prove", "blocked", "escalated", "incident"],
    "blocked":   ["scope", "preflight", "incident"],
    "escalated": ["scope", "incident", "closed"],
    # Post-close discovery: reopen as incident without inventing a new run.
    "closed":    ["incident"],
}

KIND_CONSTRUCTION = "construction"
KIND_INCEPTION = "inception"
KIND_OPERATIONS = "operations"
KINDS = (KIND_CONSTRUCTION, KIND_INCEPTION, KIND_OPERATIONS)
HANDOFF_KIND = "lider.inception.handoff"
HANDOFF_VERSION = 1

# The subgraph one UNIT OF WORK walks. A phase's spec is required to "split the
# feature into implementable units", and until now that sentence had no
# representation: a phase with three units was one flat run, and the ledger could
# not say which unit was stuck.
#
# Each unit runs this on its own, with its own findings, rounds and convergence.
# The phase does not advance past `join` until every unit is terminal.
UNIT_GRAPH = {
    "pending":    ["implement", "dropped"],
    "implement":  ["review"],
    "review":     ["adjudicate"],
    "adjudicate": ["implement", "done", "escalated"],
    "escalated":  ["implement", "done", "dropped"],
    "done":       [],
    "dropped":    [],
}

# A unit is terminal when it will not be worked on again - done, or deliberately
# abandoned. `dropped` is not a failure: descoping a unit is a legitimate
# architect decision, and one that must be visible rather than silent.
UNIT_TERMINAL = ("done", "dropped")

# Nodes that must not be entered while any check is failing or undetermined.
# Judgment nodes are deliberately absent: adjudicating an undetermined result is
# exactly what you are supposed to do with one.
GATED = {"implement", "commit", "promote", "effect", "done"}

# Which family an engine belongs to, for the reviewer != implementer rule.
# Same family = same blind spots, which is the entire reason for a second engine.
FAMILIES = {
    "codex": "openai", "gpt": "openai", "terra": "openai", "sol": "openai", "luna": "openai",
    "claude": "anthropic", "opus": "anthropic", "sonnet": "anthropic",
    "haiku": "anthropic", "fable": "anthropic",
    "grok": "xai",
    "calvoproxy": "openrouter",
    # Runtime families (cross-engine rule is about the adapter/runtime, not the
    # underlying model vendor — opencode/pi can host many model brands).
    "opencode": "opencode",
    "pi": "pi",
}

DECISIONS = ("accept", "fix", "return", "respec", "reject", "escalate")
VERDICTS = ("ok", "not-ok", "undetermined")

OK, REFUSED, UNDETERMINED, USAGE = 0, 1, 2, 3


def family_of(engine):
    """Engine -> family, or None when we cannot tell. None is not 'different'."""
    if not engine:
        return None
    low = engine.lower()
    for key, fam in FAMILIES.items():
        if key in low:
            return fam
    return None


def graph_for(state):
    """Which edge table this run walks. Kind defaults to construction for old ledgers."""
    kind = state.get("kind") or KIND_CONSTRUCTION
    if kind == KIND_INCEPTION:
        return INCEPTION_GRAPH
    if kind == KIND_OPERATIONS:
        return OPERATIONS_GRAPH
    return GRAPH
