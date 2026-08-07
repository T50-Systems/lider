"""Transition guards: edges, implement prereqs, evaluate run/unit."""
from .constants import (
    GATED,
    GRAPH,
    INCEPTION_GRAPH,
    KIND_INCEPTION,
    KIND_OPERATIONS,
    OK,
    REFUSED,
    UNDETERMINED,
    UNIT_GRAPH,
    USAGE,
)
from .handoff import check_seal
from .model import (
    blocking_checks,
    open_questions,
    open_severe,
    scope_of,
    spec_drift,
    stuck_defects,
    unblocked,
    unfinished_units,
    uncovered_criteria,
)
from .ops import (
    check_ops_act,
    check_ops_closed,
    check_ops_incident,
    check_ops_rollback,
    check_ops_scope,
)
from .storage import is_strict
from .constants import graph_for

def adjudication_guard(scope, label, force):  # -> (code, message)
    """The loop rules, applied to a run or to one unit - they have one shape.

    Convergence is about WHICH defects are still open, not how many. Counting
    cannot distinguish "fixed two, introduced one" from "the same BLOCKER is back
    for the third time" - both read as progress.
    """
    if len(scope["rounds"]) >= scope["max_rounds"] and not force:
        return REFUSED, ("%sadjudication round limit reached (%d). Escalate to a human "
                         "instead of looping; `enter escalated`."
                         % (label, scope["max_rounds"]))
    stuck = stuck_defects(scope)
    if stuck and not force:
        worst = stuck[0]
        return REFUSED, ("%sdefect %s has survived %d rounds (%s). The implementer is not "
                         "fixing it; another pass is not the answer. Change the spec or "
                         "`enter escalated`."
                         % (label, worst["defect_id"], worst["rounds"], worst["summary"][:60]))
    if len(scope["rounds"]) >= 2:
        prev = set(scope["rounds"][-2].get("severe_defects") or [])
        last = set(scope["rounds"][-1].get("severe_defects") or [])
        if prev and not (prev - last) and not force:
            return REFUSED, ("%snot converging - every BLOCKER/MAJOR open in round %d is "
                             "STILL open in round %d (%s). Nothing was resolved, so another "
                             "pass is unlikely to help; change the spec or escalate."
                             % (label, len(scope["rounds"]) - 1, len(scope["rounds"]),
                                ", ".join(sorted(last & prev)[:4])))
    return OK, None


def check_edge(graph, cur, dest, label="", kind="node"):
    """Is this transition an edge at all? Shared by the run graph and the unit graph."""
    if dest not in graph:
        return USAGE, ("%sunknown %s '%s'. Known: %s"
                       % (label, kind, dest, ", ".join(sorted(graph))))
    if dest not in graph[cur]:
        return REFUSED, ("%s'%s' -> '%s' is not an edge. From '%s' you may go to: %s"
                         % (label, cur, dest, cur, ", ".join(graph[cur]) or "(nowhere - terminal)"))
    return OK, None


def check_implement_prereqs(state, force, label="", unit_id=None):
    """Everything that must hold before ANY implementer starts.

    This lived in two copies - one in evaluate_unit, one in evaluate_run - and
    they drifted: a cross-family review found the unit copy silently missing the
    check gate, so in the decomposed path (which is THE path for a multi-unit
    phase) a failing preflight did not stop a unit from starting. Duplicated
    policy is not a line-count problem, it is a correctness one. One copy now.
    """
    if force:
        return OK, None

    bad, unknown = blocking_checks(state)
    if bad:
        return REFUSED, "%scannot start - failing check(s): %s" % (label, ", ".join(bad))
    if unknown:
        return UNDETERMINED, ("%scannot start - check(s) UNDETERMINED: %s. This is not a pass."
                              % (label, ", ".join(unknown)))

    pending = open_questions(state, unit_id)
    if pending:
        return UNDETERMINED, (
            "%s%d open question(s): %s. An unanswered input is an UNESTABLISHED one - answer "
            "it, or record the assumption with `question resolve --status assumed --answer ...`."
            % (label, len(pending), ", ".join(q["id"] for q in pending)))

    drift, detail = spec_drift(state)
    if drift == "unpinned":
        return UNDETERMINED, ("%sno spec is pinned, so there is nothing to verify the work "
                              "against. Pin it with `spec --file`." % label)
    if drift == "unreadable":
        return UNDETERMINED, ("%scannot read the pinned spec (%s). This is not a pass - re-pin "
                              "it with `spec --file`." % (label, detail))
    if drift == "changed":
        return REFUSED, ("%sthe spec changed since it was pinned (%s). The implementer would be "
                         "building from something the ledger never recorded a decision about. "
                         "Re-pin with `spec --file`, or --force." % (label, detail))
    return OK, None


def check_same_family(impl, rev, label=""):
    """A reviewer from the implementer's own family shares its blind spots."""
    if impl and rev and impl.get("family") and impl["family"] == rev.get("family"):
        return REFUSED, ("%simplementer and reviewer are both %s - adjudicating a same-family "
                         "review." % (label, impl["family"]))
    return OK, None


def evaluate_unit(state, unit, dest, force):
    """Would this unit transition be allowed? Returns (code, message). Mutates NOTHING."""
    cur = unit["node"]
    label = "[%s] " % unit["id"]

    code, message = check_edge(UNIT_GRAPH, cur, dest, label, "unit node")
    if code != OK:
        return code, message

    if dest == "implement":
        code, message = check_implement_prereqs(state, force, label, unit["id"])
        if code != OK:
            return code, message
        # Unit-only: a unit may not start before what it depends on has finished.
        # Otherwise the dependency is a comment and the work lands in an order
        # nobody chose.
        if cur == "pending" and not force:
            blocked = unblocked(state, unit)
            if blocked:
                return REFUSED, ("%scannot start - depends on unfinished unit(s): %s"
                                 % (label, ", ".join(blocked)))

    if dest == "done" and not force:
        still = open_severe(unit)
        if still:
            return REFUSED, ("%scannot finish - %d undecided BLOCKER/MAJOR finding(s): %s"
                             % (label, len(still), ", ".join(f["id"] for f in still)))

    if dest == "adjudicate" and not force:
        # A unit may name its own pair; otherwise the run's stands.
        return check_same_family(unit["roles"].get("implementer")
                                 or state["roles"].get("implementer"),
                                 unit["roles"].get("reviewer")
                                 or state["roles"].get("reviewer"), label)

    if cur == "adjudicate" and dest == "implement":
        return adjudication_guard(unit, label, force)
    return OK, None


def evaluate_run(state, dest, force):
    """Would this run transition be allowed? Returns (code, message). Mutates NOTHING."""
    cur = state["node"]
    graph = graph_for(state)

    # --- inception-only destinations ---------------------------------------
    if state.get("kind") == KIND_INCEPTION:
        # Construction-only nodes: refuse with a clear reason (not "unknown node").
        if dest not in graph and dest in GRAPH:
            return REFUSED, (
                "inception run cannot enter '%s' - discovery only (no implement/promote). "
                "Finish with `enter sealed`, then start a construction run and "
                "`import --handoff`." % dest)
        code, message = check_edge(graph, cur, dest, "", "node")
        if code != OK:
            return code, message
        if dest == "sealed":
            return check_seal(state, force)
        return OK, None

    # --- operations-only destinations --------------------------------------
    if state.get("kind") == KIND_OPERATIONS:
        foreign = set(GRAPH) | set(INCEPTION_GRAPH) | set(UNIT_GRAPH)
        if dest not in graph and dest in foreign:
            return REFUSED, (
                "operations run cannot enter '%s' - use "
                "scope/preflight/act/prove/soak/incident/rollback/closed. "
                "Feature build stays on a construction run." % dest)
        code, message = check_edge(graph, cur, dest, "", "node")
        if code != OK:
            return code, message
        if dest == "scope":
            return OK, None
        if dest == "preflight":
            return check_ops_scope(state, force)
        if dest == "act":
            return check_ops_act(state, force)
        if dest == "incident":
            return check_ops_incident(state, force)
        if dest == "rollback":
            return check_ops_rollback(state, force)
        if dest == "closed":
            return check_ops_closed(state, force)
        # prove/soak: undetermined still blocks (could not look). not-ok does NOT —
        # a failed effect is the signal to enter incident, not a stuck gate.
        if dest in ("prove", "soak") and not force:
            _bad, unknown = blocking_checks(state)
            if unknown:
                return UNDETERMINED, (
                    "cannot enter '%s' - check(s) UNDETERMINED: %s. Not a pass. "
                    "If the environment is broken, record not-ok and enter incident."
                    % (dest, ", ".join(unknown)))
        return OK, None

    code, message = check_edge(graph, cur, dest, "", "node")
    if code != OK:
        return code, message

    # --- construction ------------------------------------------------------
    if dest in GATED and not force:
        bad, unknown = blocking_checks(state)
        if bad:
            return REFUSED, ("cannot enter '%s' - failing check(s): %s"
                             % (dest, ", ".join(bad)))
        if unknown:
            return UNDETERMINED, ("cannot enter '%s' - check(s) UNDETERMINED: %s. This is "
                                  "not a pass. Establish them or say why you could not."
                                  % (dest, ", ".join(unknown)))

    if dest == "implement":
        if is_strict(state) and not state.get("handoff") and not force:
            return REFUSED, (
                "STRICT: cannot implement without a sealed inception handoff. "
                "`import --handoff .lider/handoffs/<id>.json` first, or init without "
                "--strict / unset LIDER_STRICT for the recommended-but-optional path.")
        code, message = check_implement_prereqs(state, force)
        if code != OK:
            return code, message

    if dest == "plan" and not force:
        # Coverage. Bookkeeping only - see uncovered_criteria() - and the refusal
        # says so, because a form check must never read as a substance check.
        missing = uncovered_criteria(state)
        if missing:
            return REFUSED, (
                "cannot plan - %d required criterion/criteria covered by no unit: %s. "
                "Declare a unit with --covers, or `criterion defer --reason ...`. NOTE: "
                "this checks the MAPPING only, not that any unit implements anything."
                % (len(missing), ", ".join(c["id"] for c in missing)))

    if dest == "join" and not force:
        if not state.get("units"):
            return REFUSED, ("cannot join - no units were declared. Use `unit add`, or run "
                             "the flat path (spec -> implement).")
        pending = unfinished_units(state)
        if pending:
            return REFUSED, ("cannot join - %d unit(s) still open: %s"
                             % (len(pending), ", ".join("%s(%s)" % (u["id"], u["node"])
                                                        for u in pending)))

    if dest in ("verify", "commit", "promote", "effect", "done") and not force:
        # Run-only: a unit's undecided findings must block the phase too.
        still = open_severe(state)
        for unit in state.get("units", []):
            still += [dict(f, id="%s/%s" % (unit["id"], f["id"])) for f in open_severe(unit)]
        if still:
            return REFUSED, ("cannot enter '%s' - %d undecided BLOCKER/MAJOR finding(s): %s"
                             % (dest, len(still), ", ".join(f["id"] for f in still)))

    if dest == "adjudicate" and not force:
        return check_same_family(state["roles"].get("implementer"),
                                 state["roles"].get("reviewer"))

    if cur == "adjudicate" and dest in ("implement", "spec"):
        return adjudication_guard(state, "", force)
    return OK, None


def evaluate(state, dest, unit_id, force):
    """The single guard chain. Pure: no writes, no side effects, safe to ask twice."""
    if unit_id:
        try:
            unit = scope_of(state, unit_id)
        except KeyError as exc:
            return USAGE, str(exc)
        return evaluate_unit(state, unit, dest, force)
    return evaluate_run(state, dest, force)
