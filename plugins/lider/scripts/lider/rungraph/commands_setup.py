"""Mutating ledger commands: init, target, units, criteria, questions, spec, assign, check."""
import hashlib
import json
import os
import sys
import time

from .constants import (
    KIND_CONSTRUCTION,
    KIND_INCEPTION,
    KIND_OPERATIONS,
    KINDS,
    OK,
    REFUSED,
    SCHEMA_VERSION,
    UNDETERMINED,
    USAGE,
    family_of,
)
from .model import csv_ids, find_by_id, find_unit, new_unit, unblocked
from .storage import commit, env_strict, load, need, save

def cmd_init(args):
    root, rid = args.dir, args.run
    if load(root, rid) and not args.force:
        print("rungraph: run '%s' already exists (use --force to reset)" % rid, file=sys.stderr)
        return REFUSED
    kind = getattr(args, "kind", None) or KIND_CONSTRUCTION
    if kind not in KINDS:
        print("rungraph: --kind must be one of: %s" % ", ".join(KINDS), file=sys.stderr)
        return USAGE
    strict = bool(getattr(args, "strict", False) or env_strict())
    state = {
        "schema_version": SCHEMA_VERSION,
        "run_id": rid,
        "title": args.title,
        "kind": kind,
        "strict": strict,
        "created_at": int(time.time()),
        "node": "init",
        "path": ["init"],
        "spec": None,
        "roles": {},
        "checks": {},
        "findings": [],
        "rounds": [],
        "units": [],
        "criteria": [],
        "questions": [],
        "max_rounds": args.max_rounds,
        "events": [],
        "handoff": None,       # construction: imported sealed handoff ref
        "handoff_out": None,   # inception: path written by seal
        "target": None,        # operations: env / ref / surfaces under change
    }
    save(root, rid, state)
    # The ledger is working state, not source. Keep it out of the repo by
    # construction rather than by asking every user to edit .gitignore.
    ignore = os.path.join(root, ".lider", ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8") as fh:
            fh.write("*\n")
    print("run '%s' initialised at node 'init' (kind=%s%s)"
          % (rid, kind, ", STRICT" if strict else ""))
    if kind == KIND_INCEPTION:
        print("inception: discovery only - pin a frame (`spec --file`), declare "
              "criteria/questions/units, optional challenge, then `enter sealed`.")
        if not strict:
            print("inception: challenge is OPTIONAL (warns at sealed). "
                  "Strict mode requires it: init --strict or LIDER_STRICT=1.")
    elif kind == KIND_OPERATIONS:
        print("operations: pin target, preflight, act, prove, soak, close; "
              "on failure: incident -> rollback|act -> prove. "
              "Use /preflight and /verify for how to check.")
        print("operations: RECOMMENDED around deploys/merges to shared envs; not required "
              "for pure local work. Construction promote->effect remains for feature ship.")
        if strict:
            print("STRICT: preflight ok before act; effect|prove ok before closed; "
                  "incident needs not-ok|undetermined signal; rollback needs "
                  "preflight|rollback-preflight ok + previous_ref.")
        else:
            print("operations: preflight/effect/incident checks OPTIONAL (warn if missing). "
                  "Strict: init --strict or LIDER_STRICT=1.")
    else:
        print("construction: a sealed inception handoff is RECOMMENDED "
              "(`import --handoff .lider/handoffs/<id>.json`), not required. "
              "Flat path (init -> spec -> implement) still works.")
        if strict:
            print("STRICT: `import --handoff` is required before `enter implement`.")
    return OK


def cmd_target(args):
    """Operations: pin what environment / ref / surface is under change.

    Checkable fields only - the ledger does not SSH into prod. Evidence of
    what is live stays in `check` rows; this is the declared target.
    """
    root, (rid, state) = args.dir, need(args.dir, args.run)
    if state.get("kind") != KIND_OPERATIONS and not args.force:
        print("rungraph: target is for operations runs (`init --kind operations`)",
              file=sys.stderr)
        return REFUSED
    if not args.env or not args.ref:
        print("rungraph: target needs --env and --ref (e.g. --env prod --ref abc1234)",
              file=sys.stderr)
        return USAGE
    prev = getattr(args, "previous_ref", None) or None
    state["target"] = {
        "env": args.env,
        "ref": args.ref,
        "previous_ref": prev,
        "url": args.url or None,
        "surfaces": csv_ids(args.surfaces),
        "notes": args.notes or None,
        "construction_run": args.construction_run or None,
        "at": int(time.time()),
    }
    commit(root, rid, state, "target", env=args.env, ref=args.ref,
           previous_ref=prev)
    print("target pinned: env=%s ref=%s%s%s%s"
          % (args.env, args.ref,
             (" previous=%s" % prev) if prev else "",
             (" url=%s" % args.url) if args.url else "",
             (" surfaces=%s" % ",".join(state["target"]["surfaces"]))
             if state["target"]["surfaces"] else ""))
    return OK


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


def cmd_unit(args):
    """Declare a unit of work, or list them."""
    root, (rid, state) = args.dir, need(args.dir, args.run)
    if args.action == "add" and not args.id:
        print("rungraph: unit add needs --id", file=sys.stderr)
        return USAGE
    if args.action == "list":
        if not state.get("units"):
            print("no units declared; this run is a single flat unit")
            return OK
        for unit in state["units"]:
            blocked = unblocked(state, unit)
            print("  %-14s %-11s %-28s %s"
                  % (unit["id"], unit["node"], unit["title"][:28],
                     ("blocked by " + ", ".join(blocked)) if blocked else ""))
        return OK

    existing = find_unit(state, args.id)
    if existing and not args.force:
        print("rungraph: unit '%s' already exists" % args.id, file=sys.stderr)
        return REFUSED
    depends = csv_ids(args.depends_on)
    unknown = [d for d in depends if not find_unit(state, d)]
    if unknown and not args.force:
        # Declaring a dependency on something that does not exist would make the
        # barrier unsatisfiable in a way nobody notices until the end.
        print("rungraph: unit '%s' depends on undeclared unit(s): %s. Declare them "
              "first." % (args.id, ", ".join(unknown)), file=sys.stderr)
        return REFUSED
    if args.id in depends:
        print("rungraph: unit '%s' cannot depend on itself" % args.id, file=sys.stderr)
        return REFUSED

    covers = csv_ids(args.covers)
    unknown_covers = [c for c in covers if not find_criterion(state, c)]
    if unknown_covers and not args.force:
        print("rungraph: unit '%s' claims undeclared criteri(a): %s. Declare them with "
              "`criterion add` first." % (args.id, ", ".join(unknown_covers)), file=sys.stderr)
        return REFUSED
    if state.get("criteria") and not covers and not args.force:
        # A unit that maps to nothing is unplanned scope. Only enforced once
        # criteria exist, so a run that declares none is unaffected.
        print("rungraph: unit '%s' covers no acceptance criterion. Pass --covers, or "
              "--force if it is deliberately unmapped." % args.id, file=sys.stderr)
        return REFUSED

    if existing:
        # MERGE, do not rebuild. Appending a duplicate left find_unit returning the
        # first while unfinished_units counted both, so the join barrier could never
        # open - but replacing with a fresh new_unit() was worse: a mid-flight unit
        # snapped back to `pending` and lost its findings, rounds and roles. Update
        # only what `unit add` is actually declaring.
        unit = existing
        unit["title"] = args.title or unit.get("title", "")
        unit["depends_on"] = depends
        unit["covers"] = covers
        if args.max_rounds:
            unit["max_rounds"] = args.max_rounds
    else:
        unit = new_unit(args.id, args.title, depends, args.max_rounds or state["max_rounds"])
        unit["covers"] = covers
        state.setdefault("units", []).append(unit)
    commit(root, rid, state, "unit", unit=args.id, depends_on=depends, covers=covers, replaced=bool(existing))
    print("unit '%s' %s%s%s" % (args.id, "replaced" if existing else "declared",
                                (" (after %s)" % ", ".join(depends)) if depends else "",
                                (" covering %s" % ", ".join(covers)) if covers else ""))
    return OK


def find_criterion(state, cid):
    return find_by_id(state.get("criteria"), cid)


def cmd_criterion(args):
    """Acceptance criteria as ledger objects, declared - never parsed from prose.

    Parsing them out of the spec would be fragile and format-coupled, and would
    make the ledger's view of the criteria depend on how the architect happened to
    format a heading.
    """
    root, (rid, state) = args.dir, need(args.dir, args.run)
    if args.action == "list":
        if not state.get("criteria"):
            print("no acceptance criteria declared")
            return OK
        claimed = {}
        for unit in state.get("units", []):
            for cid in unit.get("covers") or []:
                claimed.setdefault(cid, []).append(unit["id"])
        for crit in state["criteria"]:
            by = ", ".join(claimed.get(crit["id"], [])) or "NOT COVERED"
            print("  %-8s %-9s %-40s %s" % (crit["id"], crit["status"],
                                            crit["text"][:40], by))
        return OK

    if args.action == "add":
        if not args.id or not args.text:
            print("rungraph: criterion add needs --id and --text", file=sys.stderr)
            return USAGE
        if find_criterion(state, args.id) and not args.force:
            print("rungraph: criterion '%s' already exists" % args.id, file=sys.stderr)
            return REFUSED
        state.setdefault("criteria", []).append({
            "id": args.id, "text": args.text, "status": "required",
            "reason": None, "at": int(time.time())})
        commit(root, rid, state, "criterion", criterion=args.id)
        print("criterion '%s' declared (required)" % args.id)
        return OK

    # defer
    crit = find_criterion(state, args.id)
    if crit is None:
        print("rungraph: no criterion '%s'" % args.id, file=sys.stderr)
        return USAGE
    if not args.reason:
        # Same rule as a dropped unit: descoping is legitimate, and must be
        # VISIBLE rather than silent.
        print("rungraph: deferring a criterion requires --reason", file=sys.stderr)
        return REFUSED
    crit["status"] = "deferred"
    crit["reason"] = args.reason
    commit(root, rid, state, "criterion", criterion=args.id, deferred=True)
    print("criterion '%s' deferred: %s" % (args.id, args.reason))
    return OK


def cmd_question(args):
    """Open questions, with the same three outcomes as everything else."""
    root, (rid, state) = args.dir, need(args.dir, args.run)
    if args.action == "list":
        if not state.get("questions"):
            print("no open questions")
            return OK
        for q in state["questions"]:
            print("  %-6s %-9s %-52s %s" % (q["id"], q["status"], q["text"][:52],
                                            q.get("answer") or ""))
        return OK

    if args.action == "add":
        if not args.text:
            print("rungraph: question add needs --text", file=sys.stderr)
            return USAGE
        qid = "q%d" % (len(state.setdefault("questions", [])) + 1)
        state["questions"].append({"id": qid, "text": args.text, "status": "open",
                                   "answer": None, "unit": args.unit,
                                   "at": int(time.time())})
        commit(root, rid, state, "question", question=qid)
        print("question %s recorded (open)" % qid)
        return OK

    # resolve
    q = next((x for x in state.get("questions", []) if x["id"] == args.id), None)
    if q is None:
        print("rungraph: no question '%s'" % args.id, file=sys.stderr)
        return USAGE
    if args.status == "assumed" and not args.answer:
        # You may proceed on an assumption - but only one that is written down.
        print("rungraph: --status assumed requires --answer: an assumption nobody "
              "recorded is indistinguishable from a fact nobody checked.", file=sys.stderr)
        return REFUSED
    q["status"] = args.status
    q["answer"] = args.answer
    commit(root, rid, state, "question", question=q["id"], status=args.status)
    print("%s -> %s" % (q["id"], args.status))
    return OK


def cmd_spec(args):
    root, (rid, state) = args.dir, need(args.dir, args.run)
    if state.get("kind") == KIND_OPERATIONS and not args.force:
        print("rungraph: operations runs pin a `target`, not a build spec. "
              "Use `target --env ... --ref ...`.", file=sys.stderr)
        return REFUSED
    with open(args.file, encoding="utf-8") as fh:
        text = fh.read()
    inception = state.get("kind") == KIND_INCEPTION
    # Construction build specs need checkable sections. An inception FRAME is a
    # discovery doc - scope/constraints still help; "verification" is often N/A.
    if inception:
        missing = [s for s in ("scope", "constraint") if s not in text.lower()]
    else:
        missing = [s for s in ("scope", "constraint", "verification")
                   if s not in text.lower()]
    if missing and not args.force:
        # The closed spec is the flow's most important deliverable; a spec with no
        # verification section cannot produce a checkable outcome later.
        print("rungraph: spec appears to be missing section(s): %s "
              "(use --force to accept anyway)" % ", ".join(missing), file=sys.stderr)
        return REFUSED
    # Store the TEXT, not just a pointer to it. The spec is the phase's most
    # important deliverable and the only record of what was decided before any
    # code existed - and a ledger that holds a path is one `git mv` away from
    # pointing at nothing. Measured: moving a spec into docs/specs/ orphaned it.
    state["spec"] = {
        "path": os.path.abspath(args.file),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "bytes": len(text.encode("utf-8")),
        "text": text,
        "at": int(time.time()),
    }
    commit(root, rid, state, "spec", sha256=state["spec"]["sha256"][:12],
           role="frame" if inception else "build")
    label = "frame" if inception else "spec"
    print("%s pinned (%s, %d bytes)" % (label, state["spec"]["sha256"][:12],
                                        state["spec"]["bytes"]))
    return OK


def cmd_assign(args):
    root, (rid, state) = args.dir, need(args.dir, args.run)
    fam = family_of(args.engine)
    if args.role == "reviewer":
        impl = state["roles"].get("implementer")
        if impl:
            ifam = impl.get("family")
            # Unknown family is NOT proof of difference. Refusing here is the same
            # rule preflight applies to evidence: we could not establish it.
            if ifam is None or fam is None:
                print("rungraph: cannot establish that reviewer '%s' (%s) differs in family "
                      "from implementer '%s' (%s). Same-family review shares blind spots; "
                      "name a known engine or pass --force."
                      % (args.engine, fam or "unknown", impl["engine"], ifam or "unknown"),
                      file=sys.stderr)
                if not args.force:
                    return UNDETERMINED
            elif ifam == fam:
                print("rungraph: reviewer '%s' and implementer '%s' are both %s - "
                      "a same-family review shares the implementer's blind spots."
                      % (args.engine, impl["engine"], fam), file=sys.stderr)
                if not args.force:
                    return REFUSED
    state["roles"][args.role] = {
        "engine": args.engine, "model": args.model, "family": fam,
        "at": int(time.time()), "forced": bool(args.force),
    }
    commit(root, rid, state, "assign", role=args.role, engine=args.engine, family=fam)
    print("%s = %s (%s%s)" % (args.role, args.engine, fam or "unknown family",
                              ", forced" if args.force else ""))
    return OK


def cmd_check(args):
    root, (rid, state) = args.dir, need(args.dir, args.run)
    state["checks"][args.name] = {
        "verdict": args.verdict, "evidence": args.evidence or "",
        "at": int(time.time()), "node": state["node"],
    }
    commit(root, rid, state, "check", name=args.name, verdict=args.verdict)
    print("%s: %s" % (args.name, args.verdict))
    # The caller's own control flow gets the same three outcomes as the ledger.
    return {"ok": OK, "not-ok": REFUSED, "undetermined": UNDETERMINED}[args.verdict]
