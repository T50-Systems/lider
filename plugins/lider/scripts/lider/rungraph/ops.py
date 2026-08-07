"""Operations-kind guards (preflight / incident / rollback / closed)."""
from .constants import OK, REFUSED, UNDETERMINED
from .storage import check_named, check_verdict_ok, is_strict

def check_ops_scope(state, force):
    if force:
        return OK, None
    if not state.get("target"):
        return UNDETERMINED, (
            "cannot enter scope work without a target. "
            "`target --env <name> --ref <sha|tag>` first.")
    return OK, None


def check_ops_act(state, force):
    """May we act on shared state? Preflight check is RECOMMENDED; STRICT requires ok."""
    if force:
        return OK, None
    if not state.get("target"):
        return UNDETERMINED, (
            "cannot act - no target pinned. `target --env ... --ref ...` first.")
    if is_strict(state):
        if not check_verdict_ok(state, "preflight"):
            return REFUSED, (
                "STRICT: cannot act without `check --name preflight --verdict ok "
                "--evidence ...`. Run the /preflight skill and record the GO, or --force.")
    return OK, None


def ops_incident_signal(state):
    """A recorded failure or uncertainty — not a war-room narrative.

    Accepts not-ok or undetermined on incident|effect|prove|health. ok alone is
    not a signal to open an incident.
    """
    for name in ("incident", "effect", "prove", "health"):
        chk = check_named(state, name)
        if chk and chk.get("verdict") in ("not-ok", "undetermined"):
            return True, name, chk.get("verdict")
    return False, None, None


def check_ops_incident(state, force):
    """May we enter incident? Need a target; STRICT needs a ternary failure signal."""
    if force:
        return OK, None
    if not state.get("target"):
        return UNDETERMINED, (
            "cannot open incident - no target pinned. "
            "`target --env ... --ref ...` first.")
    if is_strict(state):
        ok, name, verdict = ops_incident_signal(state)
        if not ok:
            return REFUSED, (
                "STRICT: cannot open incident without a recorded signal. "
                "`check --name incident|effect|health --verdict not-ok|undetermined "
                "--evidence ...` (what failed or could not be established), or --force.")
    return OK, None


def check_ops_rollback(state, force):
    """May we roll back? Same spirit as act: preflight the revert.

    STRICT prefers rollback-preflight, accepts preflight. Non-strict warns in enter.
    previous_ref on target is RECOMMENDED so prove knows what 'good' is.
    """
    if force:
        return OK, None
    if not state.get("target"):
        return UNDETERMINED, (
            "cannot rollback - no target pinned.")
    if is_strict(state):
        if not check_verdict_ok(state, "rollback-preflight", "preflight"):
            return REFUSED, (
                "STRICT: cannot rollback without `check --name rollback-preflight` "
                "(or preflight) --verdict ok --evidence ...`. Re-run /preflight for "
                "the revert, or --force.")
        if not (state.get("target") or {}).get("previous_ref"):
            return REFUSED, (
                "STRICT: cannot rollback without target.previous_ref (last known good). "
                "`target --env ... --ref <bad> --previous-ref <good>`, or --force.")
    return OK, None


def check_ops_closed(state, force):
    """May we close the ops run? Effect proof is RECOMMENDED; STRICT requires ok.

    After rollback, effect/prove must show the *recovered* state (usually previous_ref
    is live) — same check names, new evidence line.
    """
    if force:
        return OK, None
    if is_strict(state):
        if not check_verdict_ok(state, "effect", "prove"):
            return REFUSED, (
                "STRICT: cannot close without `check --name effect` (or prove) "
                "--verdict ok --evidence ...`. Run /verify against the live surface "
                "(post-rollback: prove previous_ref is what is served), or --force.")
        # Do not close while an open incident signal still says not-ok without a
        # later ok on effect — the ok above is sufficient if they re-checked effect.
    return OK, None

