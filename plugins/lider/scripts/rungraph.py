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
from lider import metrics           # noqa: E402

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


def commit(root, rid, state, kind, **fields):
    """Record the event and persist, in that order. Every mutating command ends here."""
    log_event(state, kind, **fields)
    save(root, rid, state)


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


def find_by_id(items, item_id):
    return next((x for x in items or [] if x["id"] == item_id), None)


def csv_ids(raw):
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def find_unit(state, unit_id):
    return find_by_id(state.get("units"), unit_id)


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


def open_questions(state, unit_id=None):
    """Questions still unanswered. An unanswered input is an UNESTABLISHED one."""
    return [q for q in state.get("questions", [])
            if q["status"] == "open" and (unit_id is None or q.get("unit") in (None, unit_id))]


def uncovered_criteria(state):
    """Required criteria that no unit claims to cover.

    NOTE, and it is load-bearing: this checks the MAPPING, not the work. Both
    sides of it are written by the same orchestrator, so unlike the family rule or
    defect-identity convergence it verifies bookkeeping consistency rather than a
    fact from outside. It still catches the common, currently invisible error -
    dropping a requirement by never declaring a unit for it - but it must never be
    presented as evidence that anything was implemented.
    """
    claimed = set()
    for unit in state.get("units", []):
        claimed.update(unit.get("covers") or [])
    return [c for c in state.get("criteria", [])
            if c["status"] == "required" and c["id"] not in claimed]


def spec_drift(state):
    """Compare the pinned spec against the file on disk.

    ('ok', None) | ('changed', detail) | ('unreadable', detail) | ('unpinned', None)

    The one new guard that checks a declaration against an EXTERNAL fact, which is
    why it ranks above the rest: the pinned sha256 was stored and then never read
    again, so an implementer could work from a file that no longer matched what
    the ledger records was decided.
    """
    spec = state.get("spec")
    if not spec:
        return ("unpinned", None)
    try:
        with open(spec["path"], encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return ("unreadable", str(exc))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest != spec["sha256"]:
        return ("changed", "pinned %s, on disk %s" % (spec["sha256"][:12], digest[:12]))
    return ("ok", None)


# --- open findings ---------------------------------------------------------
def open_findings(state):
    return [f for f in state["findings"] if f.get("decision") is None]


def open_severe(state):
    return [f for f in open_findings(state) if f.get("severity") in fx.SEVERE]


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
        "criteria": [],
        "questions": [],
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
    commit(root, rid, state, "spec", sha256=state["spec"]["sha256"][:12])
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


def cmd_findings(args):
    """Ingest a review round's findings JSON (the pair-review contract)."""
    root, (rid, state) = args.dir, need(args.dir, args.run)
    # A bad --unit raises KeyError; main() already turns that into the same
    # "rungraph: <message>" + USAGE that a local handler produced.
    scope = scope_of(state, args.unit)
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
    severe = len([f for f in fresh if f["severity"] in fx.SEVERE])
    scope["rounds"].append({
        "round": round_no, "at": int(time.time()),
        "ingested": added, "severe": severe, "recurring": recurring,
        # The identities, not just the count: this is what convergence reads.
        "severe_defects": sorted({f["defect_id"] for f in fresh
                                  if f["severity"] in fx.SEVERE}),
        "verdict": doc.get("verdict") if isinstance(doc, dict) else None,
        "engine": doc.get("engine") if isinstance(doc, dict) else None,
    })
    commit(root, rid, state, "findings", round=round_no, count=added, severe=severe, recurring=recurring, unit=args.unit)
    print("%sround %d: %d findings (%d BLOCKER/MAJOR, %d recurring)"
          % (("[%s] " % args.unit) if args.unit else "", round_no, added, severe, recurring))
    return OK


def cmd_adjudicate(args):
    root, (rid, state) = args.dir, need(args.dir, args.run)
    # A bad --unit raises KeyError; main() already turns that into the same
    # "rungraph: <message>" + USAGE that a local handler produced.
    scope = scope_of(state, args.unit)
    target = next((f for f in scope["findings"] if f["id"] == args.finding), None)
    if target is None:
        print("rungraph: no finding '%s'" % args.finding, file=sys.stderr)
        return USAGE
    target["decision"] = args.decision
    target["rationale"] = args.rationale or ""
    target["decided_at"] = int(time.time())
    commit(root, rid, state, "adjudicate", finding=args.finding, decision=args.decision, unit=args.unit)
    print("%s -> %s" % (args.finding, args.decision))
    return OK


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

    code, message = check_edge(GRAPH, cur, dest, "", "node")
    if code != OK:
        return code, message

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


def cmd_enter(args):
    """The guard, applied. Every transition in the flow goes through here."""
    root, (rid, state) = args.dir, need(args.dir, args.run)
    code, message = evaluate(state, args.node, args.unit, args.force)
    if code != OK:
        print("rungraph: %s" % message, file=sys.stderr)
        return code

    # One mutation path. scope_of(state, None) is the run itself, so the unit and
    # run cases differ only in what the event records and how the line reads.
    scope = scope_of(state, args.unit)
    before = scope["node"]
    scope["node"] = args.node
    scope.setdefault("path", []).append(args.node)
    extra = {"unit": args.unit} if args.unit else {}
    commit(root, rid, state, "enter",
           **dict(extra, **{"from": before, "to": args.node, "forced": bool(args.force)}))
    print("%s%s -> %s%s" % (("[%s] " % args.unit) if args.unit else "", before, args.node,
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


def cmd_next(args):
    """What could run right now. LEDGER-read-only, and deliberately advisory.

    Precise about what it does not touch: it never writes run.json. It DOES append
    one eligibility row to metrics.jsonl - that is its second job - so calling it
    "read-only" without qualification overstated the purity. Flagged in review.

    This is NOT a scheduler and must not become one by accident: it reports what
    the graph permits, it does not decide or act. Its real job is to record how
    many units were eligible CONCURRENTLY, so that any future decision about
    building a scheduler rests on measured parallelism rather than assumed
    parallelism. Nobody has yet looked at whether real runs even have units
    eligible at the same time.
    """
    _root, (rid, state) = args.dir, need(args.dir, args.run)
    units = []
    for unit in state.get("units", []):
        blocked = unblocked(state, unit)
        eligible = (unit["node"] == "pending" and not blocked
                    and evaluate_unit(state, unit, "implement", False)[0] == OK)
        units.append({"id": unit["id"], "node": unit["node"], "eligible": eligible,
                      "blocked_by": blocked})
    width = len([u for u in units if u["eligible"]])
    report = {"run": rid, "node": state["node"],
              "legal_moves": list(GRAPH[state["node"]]), "units": units,
              "concurrency_width": width}

    # Never raises, so measuring cannot break a run.
    metrics.record(args.dir, "eligibility", run=rid, node=state["node"],
                   units=len(units), width=width)

    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return OK
    print("node: %s      legal moves: %s"
          % (state["node"], ", ".join(report["legal_moves"]) or "(terminal)"))
    if not units:
        print("no units; this run is a single flat unit")
        return OK
    for unit in units:
        mark = "READY" if unit["eligible"] else ("blocked by " + ", ".join(unit["blocked_by"])
                                                 if unit["blocked_by"] else unit["node"])
        print("  %-14s %-11s %s" % (unit["id"], unit["node"], mark))
    print("units that could run concurrently right now: %d" % width)
    return OK


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
    if state.get("questions"):
        pending = open_questions(state)
        assumed = [q for q in state["questions"] if q["status"] == "assumed"]
        print("questions: %d open, %d assumed" % (len(pending), len(assumed)))
        for q in state["questions"]:
            if q["status"] != "answered":
                print("  %-6s %-9s %s" % (q["id"], q["status"], q["text"][:60]))
    if state.get("criteria"):
        missing = uncovered_criteria(state)
        print("criteria: %d, %d required and uncovered" % (len(state["criteria"]), len(missing)))
        for crit in state["criteria"]:
            print("  %-8s %-9s %s" % (crit["id"], crit["status"], crit["text"][:56]))
        if missing:
            print("  (coverage is a MAPPING check, not evidence anything was implemented)")
    if state.get("units"):
        pending = unfinished_units(state)
        print("units: %d, %d still open" % (len(state["units"]), len(pending)))
        for unit in state["units"]:
            blocked = unblocked(state, unit)
            # NOT `severe`: that name holds the RUN's open findings a few lines up,
            # and shadowing it with an int made `show` - the first thing a resumed
            # session runs - crash with a TypeError on any run whose units had
            # severe findings. Found by a coverage test, not by using it.
            unit_severe = len(open_severe(unit))
            notes = []
            if blocked:
                notes.append("blocked by " + ", ".join(blocked))
            if unit["rounds"]:
                notes.append("%d round(s)" % len(unit["rounds"]))
            if unit_severe:
                notes.append("%d open BLOCKER/MAJOR" % unit_severe)
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

    q = sub.add_parser("criterion", help="declare acceptance criteria, defer one, or list")
    q.add_argument("action", choices=["add", "defer", "list"])
    q.add_argument("--id")
    q.add_argument("--text")
    q.add_argument("--reason", help="required when deferring: a descope must be visible")
    q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_criterion)

    q = sub.add_parser("question", help="record an open question, resolve one, or list")
    q.add_argument("action", choices=["add", "resolve", "list"])
    q.add_argument("--id")
    q.add_argument("--text")
    q.add_argument("--status", choices=["answered", "assumed"])
    q.add_argument("--answer")
    q.add_argument("--unit")
    q.set_defaults(fn=cmd_question)

    q = sub.add_parser("next",
                       help="what could run right now (never writes the ledger; records "
                            "one eligibility row to metrics.jsonl)")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_next)

    q = sub.add_parser("unit", help="declare a unit of work, or list them")
    q.add_argument("action", choices=["add", "list"])
    q.add_argument("--id")
    q.add_argument("--title", default="")
    q.add_argument("--depends-on", dest="depends_on",
                   help="comma-separated ids that must finish first")
    q.add_argument("--covers", help="comma-separated acceptance criteria this unit maps to")
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
