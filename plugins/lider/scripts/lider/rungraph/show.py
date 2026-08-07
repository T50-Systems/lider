"""Human/JSON status: artifact checklist + show."""
import json
import sys

from .constants import (
    KIND_CONSTRUCTION,
    KIND_INCEPTION,
    KIND_OPERATIONS,
    OK,
    graph_for,
)
from .model import (
    blocking_checks,
    open_questions,
    open_severe,
    stuck_defects,
    unblocked,
    uncovered_criteria,
    unfinished_units,
)
from .storage import is_strict, need

def check_verdict(state, name):
    """Latest verdict for a named check, or None if never recorded."""
    chk = (state.get("checks") or {}).get(name)
    return chk.get("verdict") if chk else None


def artifact_lines(state):
    """Human checklist: what checkable artifacts this run has / still needs.

    Pure - no I/O. `show` prints this so a resumed session sees missing pins
    before the next `enter` refusal. Not a second schema engine: only surfaces
    facts the ledger already stores.
    """
    kind = state.get("kind", KIND_CONSTRUCTION)
    node = state.get("node") or "init"
    lines = []
    strict = is_strict(state)

    def row(ok, label, how):
        lines.append(("%s  %s" % ("ok " if ok else " --", label), how if not ok else None))

    if kind == KIND_INCEPTION:
        row(bool(state.get("spec")), "frame pinned (spec --file)",
            "pin discovery with `spec --file` before sealed")
        n_crit = len(state.get("criteria") or [])
        row(n_crit > 0, "acceptance criteria (%d)" % n_crit,
            "`criterion add` — seal needs at least one")
        open_q = open_questions(state)
        row(not open_q, "questions closed (%d open)" % len(open_q),
            "answer or assume with --answer")
        miss = uncovered_criteria(state)
        row(n_crit > 0 and not miss, "criteria covered by units",
            "uncovered: %s" % ", ".join(c["id"] for c in miss) if miss else "`unit add --covers`")
        challenged = "challenge" in (state.get("path") or [])
        row(challenged, "challenge visited",
            "optional; STRICT requires `enter challenge` before sealed")
        sealed = bool(state.get("handoff_out")) or node == "sealed"
        row(sealed, "handoff sealed (.lider/handoffs/)",
            "`enter sealed` when the checklist is green")
        return lines

    if kind == KIND_OPERATIONS:
        tgt = state.get("target")
        row(bool(tgt), "target pinned (env + ref)",
            "`target --env ... --ref ...` before scope/act")
        if tgt:
            row(bool(tgt.get("previous_ref")), "previous_ref (rollback target)",
                "optional until rollback; STRICT rollback needs it")
        pre = check_verdict(state, "preflight")
        row(pre == "ok", "preflight check ok",
            "record `check --name preflight --verdict ok` (STRICT before act)")
        eff = check_verdict(state, "effect") or check_verdict(state, "prove")
        row(eff == "ok", "effect/prove check ok",
            "record `check --name effect|prove` (STRICT before closed)")
        # Incident signal: not-ok/undetermined on effect/incident names
        signal = False
        for name, chk in (state.get("checks") or {}).items():
            if name in ("effect", "prove", "incident", "soak") and chk.get("verdict") in (
                    "not-ok", "undetermined"):
                signal = True
                break
        if node in ("incident", "rollback") or "incident" in (state.get("path") or []):
            row(signal or not strict, "incident signal (failure check)",
                "STRICT: not-ok/undetermined on effect|incident before incident")
        rb = check_verdict(state, "rollback-preflight")
        if node == "rollback" or "rollback" in (state.get("path") or []):
            row(rb == "ok" or not strict, "rollback-preflight ok",
                "STRICT: `check --name rollback-preflight --verdict ok`")
        return lines

    # construction (default)
    row(bool(state.get("spec")), "spec pinned (spec --file)",
        "`spec --file` then `enter spec`")
    row(bool(state.get("handoff")), "inception handoff imported",
        "recommended; STRICT needs `import --handoff` before implement")
    roles = state.get("roles") or {}
    row("implementer" in roles, "implementer assigned",
        "`assign --role implementer --engine ...` before implement")
    row("reviewer" in roles, "reviewer assigned (other family)",
        "`assign --role reviewer` — refused if same family as implementer")
    findings = state.get("findings") or []
    any_unit_findings = any((u.get("findings") or []) for u in (state.get("units") or []))
    past_review = node in (
        "adjudicate", "verify", "commit", "promote", "effect", "done", "escalated")
    if findings or any_unit_findings or past_review:
        row(bool(findings) or any_unit_findings, "findings ingested",
            "`findings --file` after review — schema under plugins/lider/schemas/")
        severe_open = list(open_severe(state))
        for u in state.get("units") or []:
            severe_open.extend(open_severe(u))
        if findings or any_unit_findings:
            row(not severe_open, "no undecided BLOCKER/MAJOR",
                "adjudicate each, or they block verify/done")
    n_crit = len(state.get("criteria") or [])
    if n_crit or node in ("plan", "join") or state.get("units"):
        miss = uncovered_criteria(state)
        row(n_crit > 0 and not miss, "criteria covered by units (mapping only)",
            "uncovered: %s" % ", ".join(c["id"] for c in miss) if miss
            else "`criterion add` + `unit add --covers` before plan")
    open_q = open_questions(state)
    if open_q or state.get("questions"):
        row(not open_q, "open questions resolved",
            "%d open — answer or assume with --answer" % len(open_q))
    return lines


def cmd_show(args):
    root, (rid, state) = args.dir, need(args.dir, args.run)
    if args.json:
        json.dump(state, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return OK

    bad, unknown = blocking_checks(state)
    severe = open_severe(state)
    kind = state.get("kind", KIND_CONSTRUCTION)
    print("run %s - %s" % (rid, state["title"]))
    print("kind: %s%s" % (kind, "  STRICT" if is_strict(state) else ""))
    print("node: %s      path: %s" % (state["node"], " -> ".join(state["path"])))
    legal = graph_for(state).get(state["node"], [])
    print("next: %s" % (", ".join(legal) or "(terminal)"))
    spec = state["spec"]
    frame_label = "frame" if kind == KIND_INCEPTION else "spec"
    print("%s: %s" % (frame_label,
                      ("%s (%s)" % (spec["sha256"][:12], spec["path"]) if spec else "NOT PINNED")))
    if state.get("handoff"):
        h = state["handoff"]
        print("handoff in: %s (%s)" % (h.get("path"), (h.get("sha256") or "")[:12]))
    if state.get("handoff_out"):
        h = state["handoff_out"]
        print("handoff out: %s (%s)" % (h.get("path"), (h.get("sha256") or "")[:12]))
    if state.get("target"):
        t = state["target"]
        print("target: env=%s ref=%s%s%s%s"
              % (t.get("env"), t.get("ref"),
                 (" previous=%s" % t["previous_ref"]) if t.get("previous_ref") else "",
                 (" url=%s" % t["url"]) if t.get("url") else "",
                 (" surfaces=%s" % ",".join(t["surfaces"])) if t.get("surfaces") else ""))
        if t.get("construction_run"):
            print("  from construction run: %s" % t["construction_run"])
    # Artifact checklist — missing rows are what the next `enter` is likely to refuse.
    arts = artifact_lines(state)
    if arts:
        print("artifacts:")
        for label, hint in arts:
            print("  %s" % label)
            if hint:
                print("       → %s" % hint)
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
