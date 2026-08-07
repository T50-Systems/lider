"""enter / gate — guarded transitions."""
import sys
import time

from .constants import KIND_CONSTRUCTION, KIND_INCEPTION, KIND_OPERATIONS, OK
from .guards import evaluate
from .handoff import write_handoff
from .model import scope_of
from .ops import ops_incident_signal
from .storage import challenged, check_verdict_ok, commit, is_strict, need

def cmd_enter(args):
    """The guard, applied. Every transition in the flow goes through here."""
    root, (rid, state) = args.dir, need(args.dir, args.run)
    dest = args.node
    code, message = evaluate(state, dest, args.unit, args.force)
    if code != OK:
        print("rungraph: %s" % message, file=sys.stderr)
        return code

    # Non-strict inception seal without challenge: allowed, but never silent.
    if (not args.unit and dest == "sealed" and state.get("kind") == KIND_INCEPTION
            and not challenged(state) and not args.force):
        print("rungraph: WARNING: sealing without a challenge. Optional by default; "
              "strict mode (init --strict / LIDER_STRICT=1) would refuse. "
              "High-risk work should enter challenge first.", file=sys.stderr)

    # Construction without handoff: recommended, never silent in non-strict either.
    if (not args.unit and dest == "implement"
            and state.get("kind") == KIND_CONSTRUCTION
            and not state.get("handoff") and not is_strict(state) and not args.force):
        print("rungraph: note: no inception handoff imported. RECOMMENDED: run "
              "inception, `enter sealed`, then `import --handoff .lider/handoffs/<id>.json`. "
              "Flat path is allowed; STRICT mode would refuse.", file=sys.stderr)

    # Operations: warn (non-strict) when skipping recommended checks.
    if (not args.unit and state.get("kind") == KIND_OPERATIONS
            and not is_strict(state) and not args.force):
        if dest == "act" and not check_verdict_ok(state, "preflight"):
            print("rungraph: WARNING: acting without `check --name preflight --verdict ok`. "
                  "RECOMMENDED: run /preflight and record GO. STRICT would refuse.",
                  file=sys.stderr)
        if dest == "closed" and not check_verdict_ok(state, "effect", "prove"):
            print("rungraph: WARNING: closing without `check --name effect|prove --verdict ok`. "
                  "RECOMMENDED: run /verify against the live surface. STRICT would refuse.",
                  file=sys.stderr)
        if dest == "incident" and not ops_incident_signal(state)[0]:
            print("rungraph: WARNING: opening incident without a recorded "
                  "incident|effect|health not-ok|undetermined check. "
                  "RECOMMENDED: record what failed. STRICT would refuse.",
                  file=sys.stderr)
        if dest == "rollback":
            if not check_verdict_ok(state, "rollback-preflight", "preflight"):
                print("rungraph: WARNING: rollback without rollback-preflight|preflight ok. "
                      "RECOMMENDED: /preflight the revert. STRICT would refuse.",
                      file=sys.stderr)
            if not (state.get("target") or {}).get("previous_ref"):
                print("rungraph: WARNING: rollback without target.previous_ref. "
                      "RECOMMENDED: `target ... --previous-ref <good>`. STRICT would refuse.",
                      file=sys.stderr)

    # One mutation path. scope_of(state, None) is the run itself, so the unit and
    # run cases differ only in what the event records and how the line reads.
    scope = scope_of(state, args.unit)
    before = scope["node"]
    scope["node"] = dest
    scope.setdefault("path", []).append(dest)
    extra = {"unit": args.unit} if args.unit else {}

    if not args.unit and dest == "sealed" and state.get("kind") == KIND_INCEPTION:
        path, digest = write_handoff(root, state, rid)
        state["handoff_out"] = {"path": path, "sha256": digest, "at": int(time.time())}
        extra["handoff"] = path
        extra["sha256"] = digest[:12]
        commit(root, rid, state, "enter",
               **dict(extra, **{"from": before, "to": dest, "forced": bool(args.force)}))
        print("%s -> sealed%s" % (before, "  (FORCED)" if args.force else ""))
        print("handoff written: %s (%s)" % (path, digest[:12]))
        return OK

    commit(root, rid, state, "enter",
           **dict(extra, **{"from": before, "to": dest, "forced": bool(args.force)}))
    print("%s%s -> %s%s" % (("[%s] " % args.unit) if args.unit else "", before, dest,
                            "  (FORCED)" if args.force else ""))
    return OK


def cmd_gate(args):
    """Ask whether a move would be allowed. Same codes as `enter`, and NO write.

    It used to snapshot the state, let cmd_enter commit, then restore - which
    destroyed any write that landed in between and bumped `updated_at` on a
    *query*, perturbing resolve_run's "most recently updated run". A dry run that
    mutates is not a dry run.
    """
    _root, (_rid, state) = args.dir, need(args.dir, args.run)
    code, message = evaluate(state, args.node, args.unit, False)
    if code == OK:
        if args.unit:
            print("[%s] %s -> %s: allowed"
                  % (args.unit, scope_of(state, args.unit)["node"], args.node))
        else:
            print("%s -> %s: allowed" % (state["node"], args.node))
    else:
        print("rungraph: %s" % message, file=sys.stderr)
    return code
