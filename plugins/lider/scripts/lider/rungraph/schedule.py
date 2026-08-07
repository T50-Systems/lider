"""Advisory next/schedule — does not execute engines."""
import json
import os
import sys

from lider import metrics

from .constants import KIND_CONSTRUCTION, OK, REFUSED, UNIT_TERMINAL, graph_for
from .guards import evaluate_unit
from .model import unblocked
from .storage import is_strict, need

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
    g = graph_for(state)
    legal = list(g.get(state["node"], []))
    report = {"run": rid, "node": state["node"], "kind": state.get("kind", KIND_CONSTRUCTION),
              "strict": is_strict(state),
              "legal_moves": legal, "units": units,
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


def unit_ready_now(state, unit):
    """True when this unit may start implement under the live ledger (not simulated)."""
    if unit.get("node") != "pending":
        return False
    if unblocked(state, unit):
        return False
    return evaluate_unit(state, unit, "implement", False)[0] == OK


def compute_schedule(state, max_width=None):
    """Waves of units that can proceed in parallel, given dependency edges.

    Does NOT run engines. Wave 0 is what is READY right now (same as `next`).
    Later waves assume earlier waves finish (their ids join the done set) so you
    get a full plan without pretending the ledger advanced.

    Units already mid-flight (implement/review/adjudicate/...) are listed as
    in_flight, not re-scheduled. Cycles or missing deps surface as stuck.
    """
    all_ids = {u["id"] for u in state.get("units", [])}
    finished = {u["id"] for u in state.get("units", []) if u["node"] in UNIT_TERMINAL}
    in_flight = [u for u in state.get("units", [])
                 if u["node"] not in UNIT_TERMINAL and u["node"] != "pending"]
    pending = [u for u in state.get("units", []) if u["node"] == "pending"]

    simulated_done = set(finished)
    remaining = {u["id"]: u for u in pending}
    waves = []
    stuck = []
    guard = 0
    while remaining and guard < len(all_ids) + 2:
        guard += 1
        ready_ids = []
        for uid, unit in remaining.items():
            deps = unit.get("depends_on") or []
            unknown = [d for d in deps if d not in all_ids]
            unmet = [d for d in deps if d not in simulated_done]
            if unknown:
                continue
            if not unmet:
                # Live prereqs (open questions, checks) only gate wave 0.
                if not waves and not unit_ready_now(state, unit):
                    continue
                ready_ids.append(uid)
        if not ready_ids:
            stuck = list(remaining.values())
            break
        ready_ids.sort()
        if max_width and max_width > 0:
            chosen = ready_ids[:max_width]
        else:
            chosen = ready_ids
        wave_units = [remaining[i] for i in chosen]
        waves.append([{
            "id": u["id"],
            "title": u.get("title", ""),
            "depends_on": list(u.get("depends_on") or []),
            "covers": list(u.get("covers") or []),
            "node": u["node"],
        } for u in wave_units])
        for uid in chosen:
            simulated_done.add(uid)
            del remaining[uid]

    return {
        "waves": waves,
        "wave_count": len(waves),
        "width_now": len(waves[0]) if waves else 0,
        "max_wave_width": max((len(w) for w in waves), default=0),
        "in_flight": [{"id": u["id"], "node": u["node"], "title": u.get("title", "")}
                      for u in in_flight],
        "stuck": [{"id": u["id"], "depends_on": list(u.get("depends_on") or []),
                   "blocked_by": unblocked(state, u)} for u in stuck],
        "finished": sorted(finished),
    }


def schedule_commands(rid, plan, root, worktree_root=None):
    """Shell lines a human (or host agent) can run. One worktree per unit in a wave."""
    lines = []
    lines.append("# Lider schedule for run %s — ledger is still the arbiter;" % rid)
    lines.append("# these commands do NOT auto-run engines. Parallel = one worktree per unit.")
    base = worktree_root or os.path.join(root, ".lider", "worktrees", rid)
    lines.append("mkdir -p %s 2>/dev/null || mkdir %s 2>nul" % (base, base))
    for i, wave in enumerate(plan["waves"]):
        lines.append("")
        lines.append("# --- wave %d (%d unit(s) in parallel) ---" % (i, len(wave)))
        for u in wave:
            wt = os.path.join(base, u["id"])
            branch = "unit/%s-%s" % (rid, u["id"])
            lines.append("## unit %s: %s" % (u["id"], u.get("title") or ""))
            lines.append("git worktree add \"%s\" -b %s HEAD 2>/dev/null || git worktree add \"%s\" %s"
                         % (wt, branch, wt, branch))
            lines.append(
                "python \"$LIDER/scripts/rungraph.py\" --dir \"%s\" --run %s "
                "enter implement --unit %s"
                % (root, rid, u["id"]))
            lines.append(
                "# then in %s: agent-implement / host implementer for this unit only"
                % wt)
        if i + 1 < len(plan["waves"]):
            lines.append("# wait for wave %d to reach unit done, then continue" % i)
    if plan.get("in_flight"):
        lines.append("")
        lines.append("# already in flight (do not re-schedule):")
        for u in plan["in_flight"]:
            lines.append("#   %s @ %s" % (u["id"], u["node"]))
    if plan.get("stuck"):
        lines.append("")
        lines.append("# STUCK (deps unfinished or unknown) — fix mapping before scheduling:")
        for u in plan["stuck"]:
            lines.append("#   %s blocked_by=%s" % (u["id"], ",".join(u.get("blocked_by") or [])))
    return "\n".join(lines) + "\n"


def cmd_schedule(args):
    """Plan parallel unit waves. Does not execute implementers or change the ledger
    graph position — only prints (and records metrics). The orchestrator still
    runs the work and every enter still goes through the guard.

    Why this exists: `next` answers "who is ready now"; schedule answers "what is
    the whole parallel plan given deps", which is what you need to fan work across
    worktrees without holding the dependency graph in your head.
    """
    root, (rid, state) = args.dir, need(args.dir, args.run)
    if state.get("kind") not in (None, KIND_CONSTRUCTION):
        print("rungraph: schedule is for construction runs with units "
              "(kind=%s)" % state.get("kind"), file=sys.stderr)
        return REFUSED
    if not state.get("units"):
        print("rungraph: no units declared — flat run has nothing to schedule. "
              "`unit add` first, or stay on the single-unit path.", file=sys.stderr)
        return REFUSED

    max_width = args.max_width if getattr(args, "max_width", None) else None
    plan = compute_schedule(state, max_width=max_width)
    plan["run"] = rid
    plan["node"] = state["node"]
    plan["max_width_cap"] = max_width

    metrics.record(args.dir, "schedule", run=rid, node=state["node"],
                   waves=plan["wave_count"], width_now=plan["width_now"],
                   max_wave_width=plan["max_wave_width"],
                   stuck=len(plan["stuck"]), in_flight=len(plan["in_flight"]))

    fmt = getattr(args, "format", None) or ("json" if args.json else "text")
    if fmt == "json" or args.json:
        json.dump(plan, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return OK
    if fmt == "commands":
        sys.stdout.write(schedule_commands(rid, plan, root,
                                           getattr(args, "worktree_root", None)))
        return OK

    # human text
    print("schedule for run %s (node=%s)" % (rid, state["node"]))
    print("waves: %d    ready now: %d    peak wave width: %d%s"
          % (plan["wave_count"], plan["width_now"], plan["max_wave_width"],
             ("    cap=%d" % max_width) if max_width else ""))
    if plan["in_flight"]:
        print("in flight:")
        for u in plan["in_flight"]:
            print("  %-14s %s  %s" % (u["id"], u["node"], u.get("title", "")[:40]))
    for i, wave in enumerate(plan["waves"]):
        print("wave %d (%d parallel):" % (i, len(wave)))
        for u in wave:
            deps = (", after %s" % ",".join(u["depends_on"])) if u["depends_on"] else ""
            print("  %-14s %s%s" % (u["id"], (u.get("title") or "")[:36], deps))
    if plan["stuck"]:
        print("STUCK (will never schedule until deps resolve):")
        for u in plan["stuck"]:
            print("  %-14s blocked_by=%s" % (u["id"], ",".join(u["blocked_by"]) or "?"))
    if plan["wave_count"] == 0 and not plan["in_flight"]:
        print("nothing to schedule — all units finished or none are startable")
    else:
        print("tip: `schedule --format commands` prints worktree + enter lines; "
              "ledger still requires enter/implement per unit.")
    return OK
