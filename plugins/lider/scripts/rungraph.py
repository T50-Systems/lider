#!/usr/bin/env python3
"""rungraph.py - the run ledger: Lider's flow as an enforced state machine.

`pipeline/SKILL.md` describes a graph in prose. Prose is honoured, not enforced:
nothing counts adjudication rounds, nothing checks that the reviewer differs from
the implementer, and nothing survives the session that held the spec. This turns
that graph into data and its rules into guards.

Three things it makes real:

  1. THE GRAPH IS DATA. `GRAPH` below lists the nodes and the legal edges. A
     transition that is not an edge is refused, with the legal ones named. Change
     the flow by editing that table, not by rewriting instructions.

  2. THE ADJUDICATION LOOP IS BOUNDED AND MUST CONVERGE. Returning to the
     implementer opens a round. Rounds are capped, and each one must strictly
     reduce open BLOCKER+MAJOR findings - a loop that stops shrinking is stopped
     rather than spun. Counting rounds is not the same as converging.

  3. `could not determine` IS A TYPE, NOT A PARAGRAPH. Every check is
     ok | not-ok | undetermined; `undetermined` blocks forward edges exactly like
     a failure. Exit code 2 means "I could not establish this" everywhere, so a
     caller can never round it down to "fine".

State lives in <repo>/.lider/runs/<run-id>/run.json, written atomically. It
outlives the session that created it: a resumed orchestrator runs `show` and
knows where it is, what the spec was, and what is still open.

Exit codes:  0 ok  |  1 refused (a rule says no)  |  2 undetermined  |  3 usage
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lider import findings as fx    # noqa: E402

SCHEMA_VERSION = 1

# --- the graph -------------------------------------------------------------
# node -> what may follow it. Everything the flow is allowed to do is here; if a
# transition is not in this table it does not happen.
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
}

SEVERE = ("BLOCKER", "MAJOR")
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


# --- storage ---------------------------------------------------------------
def runs_dir(root):
    return os.path.join(root, ".lider", "runs")


def run_path(root, run_id):
    return os.path.join(runs_dir(root), run_id, "run.json")


def load(root, run_id):
    path = run_path(root, run_id)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save(root, run_id, state):
    """Atomic write: a reader never sees a half-written ledger."""
    path = run_path(root, run_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state["updated_at"] = int(time.time())
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        os.path.exists(tmp) and os.unlink(tmp)
        raise


def resolve_run(root, run_id):
    """Explicit id, else the most recently updated run, else nothing."""
    if run_id:
        return run_id
    base = runs_dir(root)
    if not os.path.isdir(base):
        return None
    candidates = []
    for name in os.listdir(base):
        path = os.path.join(base, name, "run.json")
        if os.path.exists(path):
            candidates.append((os.path.getmtime(path), name))
    return max(candidates)[1] if candidates else None


def need(root, run_id):
    rid = resolve_run(root, run_id)
    state = load(root, rid) if rid else None
    if state is None:
        print("rungraph: no run found (run `init` first, or pass --run)", file=sys.stderr)
        sys.exit(USAGE)
    return rid, state


def log_event(state, kind, **fields):
    fields.update(kind=kind, at=int(time.time()), node=state["node"])
    state["events"].append(fields)


# --- scopes ----------------------------------------------------------------
# A unit carries the same shape as the run itself - findings, rounds,
# max_rounds - so every convergence rule below works on either without knowing
# which it was handed. That is the whole reason units were modelled this way.
def new_unit(unit_id, title, depends_on, max_rounds):
    return {
        "id": unit_id, "title": title, "depends_on": list(depends_on),
        "node": "pending", "path": ["pending"],
        "findings": [], "rounds": [], "max_rounds": max_rounds,
        "roles": {}, "created_at": int(time.time()),
    }


def find_unit(state, unit_id):
    for unit in state.get("units", []):
        if unit["id"] == unit_id:
            return unit
    return None


def scope_of(state, unit_id):
    """The run, or one of its units. Raises a caller-friendly message if unknown."""
    if not unit_id:
        return state
    unit = find_unit(state, unit_id)
    if unit is None:
        known = ", ".join(u["id"] for u in state.get("units", [])) or "(none defined)"
        raise KeyError("no unit '%s'. Known units: %s" % (unit_id, known))
    return unit


def unblocked(state, unit):
    """Dependencies that are not finished yet, so this unit may not start."""
    return [dep for dep in unit.get("depends_on", [])
            if (find_unit(state, dep) or {}).get("node") not in UNIT_TERMINAL]


def unfinished_units(state):
    return [u for u in state.get("units", []) if u["node"] not in UNIT_TERMINAL]


# --- open findings ---------------------------------------------------------
def open_findings(state):
    return [f for f in state["findings"] if f.get("decision") is None]


def open_severe(state):
    return [f for f in open_findings(state) if f.get("severity") in SEVERE]


def stuck_defects(state, limit=3):
    """Severe defects reported in `limit` or more distinct rounds, worst first.

    A defect that keeps coming back after being returned is the clearest signal
    that looping again will not help - and it is invisible to any count-based
    convergence check, because the count can fall while the same defect survives.
    """
    seen = {}
    for finding in state["findings"]:
        if finding.get("severity") not in fx.SEVERE:
            continue
        defect = finding.get("defect_id", finding["id"])
        entry = seen.setdefault(defect, {"defect_id": defect, "rounds": set(),
                                         "summary": finding.get("summary", "")})
        entry["rounds"].add(finding.get("round"))
    out = [{"defect_id": d, "rounds": len(e["rounds"]), "summary": e["summary"]}
           for d, e in seen.items() if len(e["rounds"]) >= limit]
    return sorted(out, key=lambda e: -e["rounds"])


def blocking_checks(state):
    """Checks that forbid a forward move: failures AND things we could not establish."""
    bad, unknown = [], []
    for name, chk in state["checks"].items():
        if chk["verdict"] == "not-ok":
            bad.append(name)
        elif chk["verdict"] == "undetermined":
            unknown.append(name)
    return bad, unknown


# --- commands --------------------------------------------------------------
def cmd_init(args):
    root, rid = args.dir, args.run
    if load(root, rid) and not args.force:
        print("rungraph: run '%s' already exists (use --force to reset)" % rid, file=sys.stderr)
        return REFUSED
    state = {
        "schema_version": SCHEMA_VERSION,
        "run_id": rid,
        "title": args.title,
        "created_at": int(time.time()),
        "node": "init",
        "path": ["init"],
        "spec": None,
        "roles": {},
        "checks": {},
        "findings": [],
        "rounds": [],
        "units": [],
        "max_rounds": args.max_rounds,
        "events": [],
    }
    save(root, rid, state)
    # The ledger is working state, not source. Keep it out of the repo by
    # construction rather than by asking every user to edit .gitignore.
    ignore = os.path.join(root, ".lider", ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8") as fh:
            fh.write("*\n")
    print("run '%s' initialised at node 'init'" % rid)
    return OK


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

    if find_unit(state, args.id) and not args.force:
        print("rungraph: unit '%s' already exists" % args.id, file=sys.stderr)
        return REFUSED
    depends = [d.strip() for d in (args.depends_on or "").split(",") if d.strip()]
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

    state.setdefault("units", []).append(
        new_unit(args.id, args.title, depends, args.max_rounds or state["max_rounds"]))
    log_event(state, "unit", unit=args.id, depends_on=depends)
    save(root, rid, state)
    print("unit '%s' declared%s" % (args.id,
                                    (" (after %s)" % ", ".join(depends)) if depends else ""))
    return OK


def cmd_spec(args):
    root, (rid, state) = args.dir, need(args.dir, args.run)
    with open(args.file, encoding="utf-8") as fh:
        text = fh.read()
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
    log_event(state, "spec", sha256=state["spec"]["sha256"][:12])
    save(root, rid, state)
    print("spec pinned (%s, %d bytes)" % (state["spec"]["sha256"][:12], state["spec"]["bytes"]))
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
    log_event(state, "assign", role=args.role, engine=args.engine, family=fam)
    save(root, rid, state)
    print("%s = %s (%s%s)" % (args.role, args.engine, fam or "unknown family",
                              ", forced" if args.force else ""))
    return OK


def cmd_check(args):
    root, (rid, state) = args.dir, need(args.dir, args.run)
    state["checks"][args.name] = {
        "verdict": args.verdict, "evidence": args.evidence or "",
        "at": int(time.time()), "node": state["node"],
    }
    log_event(state, "check", name=args.name, verdict=args.verdict)
    save(root, rid, state)
    print("%s: %s" % (args.name, args.verdict))
    # The caller's own control flow gets the same three outcomes as the ledger.
    return {"ok": OK, "not-ok": REFUSED, "undetermined": UNDETERMINED}[args.verdict]


def cmd_findings(args):
    """Ingest a review round's findings JSON (the pair-review contract)."""
    root, (rid, state) = args.dir, need(args.dir, args.run)
    try:
        scope = scope_of(state, args.unit)
    except KeyError as exc:
        print("rungraph: %s" % exc, file=sys.stderr)
        return USAGE
    with open(args.file, encoding="utf-8") as fh:
        doc = json.load(fh)
    items = doc.get("findings", []) if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        print("rungraph: %s has no findings array" % args.file, file=sys.stderr)
        return USAGE
    round_no = len(scope["rounds"]) + 1
    added = recurring = 0
    for item in items:
        entry = {
            "id": "r%d-%d" % (round_no, added + 1),
            "round": round_no,
            "severity": item.get("severity", "MINOR"),
            "summary": item.get("summary", ""),
            "location": item.get("location"),
            "decision": None,
            "engine": doc.get("engine") if isinstance(doc, dict) else None,
        }
        # A defect keeps ONE identity across rounds. Without this, convergence
        # can only count findings - and a count cannot tell "two fixed, one new"
        # from "the same BLOCKER came back for the third time".
        # Only PRIOR rounds: two reviewers describing one defect inside the same
        # round is the reducer's job, and counting it as a recurrence mislabels a
        # first sighting as a defect that came back.
        prior = fx.match(entry, [f for f in scope["findings"]
                                 if f.get("round") != round_no])
        if prior is not None:
            entry["defect_id"] = prior.get("defect_id", prior["id"])
            entry["recurrence_of"] = prior["id"]
            recurring += 1
        else:
            entry["defect_id"] = entry["id"]
        scope["findings"].append(entry)
        added += 1
    fresh = [f for f in scope["findings"] if f["round"] == round_no]
    severe = len([f for f in fresh if f["severity"] in SEVERE])
    scope["rounds"].append({
        "round": round_no, "at": int(time.time()),
        "ingested": added, "severe": severe, "recurring": recurring,
        # The identities, not just the count: this is what convergence reads.
        "severe_defects": sorted({f["defect_id"] for f in fresh
                                  if f["severity"] in SEVERE}),
        "verdict": doc.get("verdict") if isinstance(doc, dict) else None,
        "engine": doc.get("engine") if isinstance(doc, dict) else None,
    })
    log_event(state, "findings", round=round_no, count=added, severe=severe,
              recurring=recurring, unit=args.unit)
    save(root, rid, state)
    print("%sround %d: %d findings (%d BLOCKER/MAJOR, %d recurring)"
          % (("[%s] " % args.unit) if args.unit else "", round_no, added, severe, recurring))
    return OK


def cmd_adjudicate(args):
    root, (rid, state) = args.dir, need(args.dir, args.run)
    try:
        scope = scope_of(state, args.unit)
    except KeyError as exc:
        print("rungraph: %s" % exc, file=sys.stderr)
        return USAGE
    target = next((f for f in scope["findings"] if f["id"] == args.finding), None)
    if target is None:
        print("rungraph: no finding '%s'" % args.finding, file=sys.stderr)
        return USAGE
    target["decision"] = args.decision
    target["rationale"] = args.rationale or ""
    target["decided_at"] = int(time.time())
    log_event(state, "adjudicate", finding=args.finding, decision=args.decision,
              unit=args.unit)
    save(root, rid, state)
    print("%s -> %s" % (args.finding, args.decision))
    return OK


def adjudication_guard(scope, label, force):
    """The loop rules, applied to a run or to one unit - they have one shape.

    Convergence is about WHICH defects are still open, not how many. Counting
    cannot distinguish "fixed two, introduced one" from "the same BLOCKER is back
    for the third time" - both read as progress.
    """
    if len(scope["rounds"]) >= scope["max_rounds"] and not force:
        print("rungraph: %sadjudication round limit reached (%d). Escalate to a human "
              "instead of looping; `enter escalated`." % (label, scope["max_rounds"]),
              file=sys.stderr)
        return REFUSED
    stuck = stuck_defects(scope)
    if stuck and not force:
        worst = stuck[0]
        print("rungraph: %sdefect %s has survived %d rounds (%s). The implementer is not "
              "fixing it; another pass is not the answer. Change the spec or "
              "`enter escalated`."
              % (label, worst["defect_id"], worst["rounds"], worst["summary"][:60]),
              file=sys.stderr)
        return REFUSED
    if len(scope["rounds"]) >= 2:
        prev = set(scope["rounds"][-2].get("severe_defects") or [])
        last = set(scope["rounds"][-1].get("severe_defects") or [])
        if prev and not (prev - last) and not force:
            print("rungraph: %snot converging - every BLOCKER/MAJOR open in round %d is "
                  "STILL open in round %d (%s). Nothing was resolved, so another pass "
                  "is unlikely to help; change the spec or escalate."
                  % (label, len(scope["rounds"]) - 1, len(scope["rounds"]),
                     ", ".join(sorted(last & prev)[:4])), file=sys.stderr)
            return REFUSED
    return OK


def enter_unit(state, unit, dest, force):
    """Move one unit through its own subgraph."""
    cur = unit["node"]
    label = "[%s] " % unit["id"]

    if dest not in UNIT_GRAPH:
        print("rungraph: %sunknown unit node '%s'. Known: %s"
              % (label, dest, ", ".join(sorted(UNIT_GRAPH))), file=sys.stderr)
        return USAGE
    if dest not in UNIT_GRAPH[cur]:
        print("rungraph: %s'%s' -> '%s' is not an edge. From '%s' you may go to: %s"
              % (label, cur, dest, cur, ", ".join(UNIT_GRAPH[cur]) or "(nowhere - terminal)"),
              file=sys.stderr)
        return REFUSED

    # A unit may not start before what it depends on has finished. Without this
    # the dependency is a comment, and the work lands in an order nobody chose.
    if dest == "implement" and cur == "pending":
        blocked = unblocked(state, unit)
        if blocked and not force:
            print("rungraph: %scannot start - depends on unfinished unit(s): %s"
                  % (label, ", ".join(blocked)), file=sys.stderr)
            return REFUSED

    if dest in ("done",):
        still = open_severe(unit)
        if still and not force:
            print("rungraph: %scannot finish - %d undecided BLOCKER/MAJOR finding(s): %s"
                  % (label, len(still), ", ".join(f["id"] for f in still)), file=sys.stderr)
            return REFUSED

    if dest == "adjudicate" and not force:
        impl = unit["roles"].get("implementer") or state["roles"].get("implementer")
        rev = unit["roles"].get("reviewer") or state["roles"].get("reviewer")
        if impl and rev and impl.get("family") and impl["family"] == rev.get("family"):
            print("rungraph: %simplementer and reviewer are both %s - adjudicating a "
                  "same-family review." % (label, impl["family"]), file=sys.stderr)
            return REFUSED

    if cur == "adjudicate" and dest == "implement":
        code = adjudication_guard(unit, label, force)
        if code != OK:
            return code

    unit["node"] = dest
    unit.setdefault("path", []).append(dest)
    return OK


def cmd_enter(args):
    """The guard. Every transition in the flow goes through here."""
    root, (rid, state) = args.dir, need(args.dir, args.run)

    # --- a unit's own subgraph -------------------------------------------
    if args.unit:
        try:
            unit = scope_of(state, args.unit)
        except KeyError as exc:
            print("rungraph: %s" % exc, file=sys.stderr)
            return USAGE
        before = unit["node"]
        code = enter_unit(state, unit, args.node, args.force)
        if code == OK:
            log_event(state, "enter", unit=args.unit, **{"from": before, "to": args.node,
                                                         "forced": bool(args.force)})
            save(root, rid, state)
            print("[%s] %s -> %s%s" % (args.unit, before, args.node,
                                       "  (FORCED)" if args.force else ""))
        return code

    # --- the run's own graph ---------------------------------------------
    cur, dest = state["node"], args.node

    if dest not in GRAPH:
        print("rungraph: unknown node '%s'. Known: %s"
              % (dest, ", ".join(sorted(GRAPH))), file=sys.stderr)
        return USAGE

    if dest not in GRAPH[cur]:
        print("rungraph: '%s' -> '%s' is not an edge. From '%s' you may go to: %s"
              % (cur, dest, cur, ", ".join(GRAPH[cur]) or "(nowhere - terminal)"),
              file=sys.stderr)
        return REFUSED

    # Gate 1: unresolved evidence. Undetermined blocks exactly like not-ok - that
    # equivalence is the whole point, and it is enforced here once instead of
    # being restated in every skill.
    if dest in GATED:
        bad, unknown = blocking_checks(state)
        if bad and not args.force:
            print("rungraph: cannot enter '%s' - failing check(s): %s"
                  % (dest, ", ".join(bad)), file=sys.stderr)
            return REFUSED
        if unknown and not args.force:
            print("rungraph: cannot enter '%s' - check(s) UNDETERMINED: %s. "
                  "This is not a pass. Establish them or say why you could not."
                  % (dest, ", ".join(unknown)), file=sys.stderr)
            return UNDETERMINED

    # Gate 2: the JOIN barrier. A decomposed phase does not proceed while any unit
    # is still open - that is what makes the decomposition mean something rather
    # than being a list in a document.
    if dest == "join" and not args.force:
        pending = unfinished_units(state)
        if not state.get("units"):
            print("rungraph: cannot join - no units were declared. Use `unit add`, or "
                  "run the flat path (spec -> implement).", file=sys.stderr)
            return REFUSED
        if pending:
            print("rungraph: cannot join - %d unit(s) still open: %s"
                  % (len(pending), ", ".join("%s(%s)" % (u["id"], u["node"])
                                             for u in pending)), file=sys.stderr)
            return REFUSED

    # Gate 3: open severe findings must not walk past judgment. `verify` is in
    # this list because leaving adjudication with undecided BLOCKER/MAJOR findings
    # is exactly the half-finished adjudication the node exists to prevent -
    # without it, a run reaches verification with its worst findings unread.
    # Units are included: a phase must not verify over a unit's unread findings.
    if dest in ("verify", "commit", "promote", "effect", "done"):
        still = open_severe(state)
        for unit in state.get("units", []):
            still += [dict(f, id="%s/%s" % (unit["id"], f["id"])) for f in open_severe(unit)]
        if still and not args.force:
            print("rungraph: cannot enter '%s' - %d undecided BLOCKER/MAJOR finding(s): %s"
                  % (dest, len(still), ", ".join(f["id"] for f in still)), file=sys.stderr)
            return REFUSED

    # Gate 4: the review must have been done by a different engine family.
    if dest == "adjudicate" and not args.force:
        impl, rev = state["roles"].get("implementer"), state["roles"].get("reviewer")
        if impl and rev and impl.get("family") and impl["family"] == rev.get("family"):
            print("rungraph: implementer and reviewer are both %s - adjudicating a "
                  "same-family review." % impl["family"], file=sys.stderr)
            return REFUSED

    # Gate 5: the adjudication loop. Bounded AND required to converge.
    if cur == "adjudicate" and dest in ("implement", "spec"):
        code = adjudication_guard(state, "", args.force)
        if code != OK:
            return code

    state["node"] = dest
    state["path"].append(dest)
    log_event(state, "enter", **{"from": cur, "to": dest, "forced": bool(args.force)})
    save(root, rid, state)
    print("%s -> %s%s" % (cur, dest, "  (FORCED)" if args.force else ""))
    return OK


def cmd_gate(args):
    """Ask whether a move is allowed without making it. Same codes as `enter`."""
    root, (rid, state) = args.dir, need(args.dir, args.run)
    snapshot = json.loads(json.dumps(state))
    args.force = False
    code = cmd_enter(args)
    save(root, rid, snapshot)   # `gate` never advances the run
    return code


def cmd_show(args):
    root, (rid, state) = args.dir, need(args.dir, args.run)
    if args.json:
        json.dump(state, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return OK

    bad, unknown = blocking_checks(state)
    severe = open_severe(state)
    print("run %s - %s" % (rid, state["title"]))
    print("node: %s      path: %s" % (state["node"], " -> ".join(state["path"])))
    print("next: %s" % (", ".join(GRAPH[state["node"]]) or "(terminal)"))
    spec = state["spec"]
    print("spec: %s" % ("%s (%s)" % (spec["sha256"][:12], spec["path"]) if spec else "NOT PINNED"))
    if state["roles"]:
        print("roles:")
        for role, info in state["roles"].items():
            print("  %-12s %s / %s [%s]%s" % (role, info["engine"], info["model"] or "-",
                                              info["family"] or "unknown family",
                                              "  FORCED" if info.get("forced") else ""))
    if state["checks"]:
        print("checks:")
        for name, chk in state["checks"].items():
            mark = {"ok": "ok ", "not-ok": "NOT", "undetermined": "?? "}[chk["verdict"]]
            print("  [%s] %-24s %s" % (mark, name, chk["evidence"][:60]))
    if state.get("units"):
        pending = unfinished_units(state)
        print("units: %d, %d still open" % (len(state["units"]), len(pending)))
        for unit in state["units"]:
            blocked = unblocked(state, unit)
            severe = len(open_severe(unit))
            notes = []
            if blocked:
                notes.append("blocked by " + ", ".join(blocked))
            if unit["rounds"]:
                notes.append("%d round(s)" % len(unit["rounds"]))
            if severe:
                notes.append("%d open BLOCKER/MAJOR" % severe)
            print("  %-14s %-11s %-24s %s" % (unit["id"], unit["node"],
                                              unit["title"][:24], "; ".join(notes)))
    if state["rounds"]:
        print("rounds: %d/%d" % (len(state["rounds"]), state["max_rounds"]))
        for rnd in state["rounds"]:
            print("  round %d: %d findings, %d BLOCKER/MAJOR, %d recurring  (%s via %s)"
                  % (rnd["round"], rnd["ingested"], rnd["severe"],
                     rnd.get("recurring", 0), rnd["verdict"] or "?", rnd["engine"] or "?"))
    stuck = stuck_defects(state)
    if stuck:
        print("STUCK (same defect across rounds - looping will not fix these):")
        for entry in stuck:
            print("  %-8s %d rounds  %s" % (entry["defect_id"], entry["rounds"],
                                            entry["summary"][:56]))
    if severe:
        print("OPEN BLOCKER/MAJOR (%d):" % len(severe))
        for f in severe:
            print("  %-8s %-8s %s" % (f["id"], f["severity"], f["summary"][:64]))
    if bad:
        print("FAILING: %s" % ", ".join(bad))
    if unknown:
        print("UNDETERMINED (not a pass): %s" % ", ".join(unknown))
    return OK


def build_parser():
    # --dir/--run are accepted on BOTH sides of the subcommand. argparse defaults
    # to global-only, which silently rejects the placement most people reach for
    # first (`... enter spec --run demo`).
    # SUPPRESS matters: without it the subparser's unset default (None) would
    # overwrite a value already given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default=argparse.SUPPRESS,
                        help="repo root holding .lider/ (default: cwd)")
    common.add_argument("--run", default=argparse.SUPPRESS,
                        help="run id (default: the most recently updated run)")

    p = argparse.ArgumentParser(prog="rungraph.py", description=__doc__.split("\n")[0],
                                parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=lambda **kw:
                           argparse.ArgumentParser(parents=[common], **kw))

    q = sub.add_parser("init", help="start a run")
    q.add_argument("--title", required=True)
    q.add_argument("--max-rounds", type=int, default=3)
    q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_init)

    q = sub.add_parser("spec", help="pin the closed spec")
    q.add_argument("--file", required=True)
    q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_spec)

    q = sub.add_parser("assign", help="record who plays a role")
    q.add_argument("--role", required=True,
                   choices=["architect", "implementer", "reviewer", "challenger"])
    q.add_argument("--engine", required=True)
    q.add_argument("--model")
    q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_assign)

    q = sub.add_parser("check", help="record a check with a three-valued verdict")
    q.add_argument("--name", required=True)
    q.add_argument("--verdict", required=True, choices=list(VERDICTS))
    q.add_argument("--evidence")
    q.set_defaults(fn=cmd_check)

    q = sub.add_parser("unit", help="declare a unit of work, or list them")
    q.add_argument("action", choices=["add", "list"])
    q.add_argument("--id")
    q.add_argument("--title", default="")
    q.add_argument("--depends-on", dest="depends_on",
                   help="comma-separated ids that must finish first")
    q.add_argument("--max-rounds", type=int)
    q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_unit)

    q = sub.add_parser("findings", help="ingest a review round's findings JSON")
    q.add_argument("--file", required=True)
    q.add_argument("--unit", help="scope this round to a unit of work")
    q.set_defaults(fn=cmd_findings)

    q = sub.add_parser("adjudicate", help="decide one finding")
    q.add_argument("--finding", required=True)
    q.add_argument("--decision", required=True, choices=list(DECISIONS))
    q.add_argument("--rationale")
    q.add_argument("--unit", help="scope this decision to a unit of work")
    q.set_defaults(fn=cmd_adjudicate)

    q = sub.add_parser("enter", help="move to a node (guarded)")
    q.add_argument("node")
    q.add_argument("--unit", help="move a UNIT through its own subgraph instead of the run")
    q.add_argument("--force", action="store_true", help="override a guard, recorded in the ledger")
    q.set_defaults(fn=cmd_enter)

    q = sub.add_parser("gate", help="ask whether a move would be allowed, without moving")
    q.add_argument("node")
    q.add_argument("--unit")
    q.set_defaults(fn=cmd_gate)

    q = sub.add_parser("show", help="what a resumed orchestrator reads first")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_show)
    return p


def main():
    args = build_parser().parse_args()
    args.dir = getattr(args, "dir", None) or "."
    args.run = getattr(args, "run", None)
    try:
        return args.fn(args)
    except FileNotFoundError as exc:
        print("rungraph: %s" % exc, file=sys.stderr)
        return USAGE
    except (ValueError, KeyError) as exc:
        print("rungraph: %s" % exc, file=sys.stderr)
        return USAGE


if __name__ == "__main__":
    sys.exit(main())
