#!/usr/bin/env python3
"""metrics-report.py - turn Lider's recorded runs into the answers it needs.

Every section here exists to settle a decision the flow currently makes from
doctrine written in a SKILL.md. Read it as: "here is what the table in the skill
should say, according to what actually happened."

  routing     which engine to send work to
  reviewers   which engine's findings are worth adjudicating
  lenses      which lenses earn their slot in a fan-out
  votes       whether the refutation vote count is right
  timing      whether the timeouts and stall thresholds fit reality
  health      how often the infrastructure, not the model, was the problem

Usage: metrics-report.py [--dir <repo>] [--section <name>] [--json]

Unmeasured values are counted and shown, never averaged in as zero. A column
that reads `unknown (3/3 unmeasured)` is telling you the truth: nobody knows.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lider import metrics    # noqa: E402


def section_routing(events):
    """Per engine: did its work get accepted, what did it cost, how long."""
    rows = []
    for engine, group in sorted(metrics.by(events, "run", "engine").items()):
        impl = [e for e in group if e.get("tool") == "implement"]
        ok = len([e for e in group if e.get("exit") == 0])
        cost, secs = metrics.Agg(), metrics.Agg()
        for event in group:
            cost.add(event.get("cost_usd"))
            secs.add(event.get("elapsed_s"))
        rows.append({
            "engine": engine, "runs": len(group), "implements": len(impl),
            "ok_rate": "%d%%" % (100 * ok // len(group)) if group else "-",
            "cost": cost.fmt(" USD"), "mean_s": "%.0f" % secs.mean if secs.n else "?",
        })
    return rows


def section_reviewers(events):
    """Per engine acting as reviewer: findings produced, and how many were unique."""
    rows = []
    for engine, group in sorted(metrics.by(events, "lens", "engine").items()):
        found, uniq, cost = metrics.Agg(), metrics.Agg(), metrics.Agg()
        for event in group:
            found.add(event.get("findings"))
            uniq.add(event.get("unique"))
            cost.add(event.get("cost_usd"))
        share = ("%d%%" % (100 * uniq.total / found.total)) if found.total else "-"
        rows.append({
            "engine": engine, "reviews": len(group),
            "findings": int(found.total), "unique": int(uniq.total),
            "unique_share": share, "cost": cost.fmt(" USD"),
        })
    return rows


def section_lenses(events):
    """The pruning question: what did each lens contribute that nobody else did?

    A lens whose `unique` is 0 across many rounds is paying for an engine call to
    repeat another lens. That is the clearest drop signal the fan-out produces.
    """
    rows = []
    for lens, group in sorted(metrics.by(events, "lens", "lens").items()):
        uniq, shared, cost = metrics.Agg(), metrics.Agg(), metrics.Agg()
        dead = 0
        for event in group:
            uniq.add(event.get("unique"))
            shared.add(event.get("shared"))
            cost.add(event.get("cost_usd"))
            if event.get("exit") not in (0, None):
                dead += 1
        per_unique = ("%.4f" % (cost.total / uniq.total)) if (cost.n and uniq.total) else "?"
        rows.append({
            "lens": lens, "runs": len(group), "failed": dead,
            "unique": int(uniq.total), "shared": int(shared.total),
            "cost": cost.fmt(" USD"), "usd_per_unique": per_unique,
        })
    return rows


def section_votes(events):
    """Was the extra ballot worth buying?

    If no claim was ever decided by its last vote, the vote count is one higher
    than it needs to be, and every refute pass is paying for it.
    """
    rows = []
    for event in events:
        if event.get("kind") != "round" or not event.get("refutation"):
            continue
        summary = event["refutation"]
        rows.append({
            "claims": summary.get("claims"), "upheld": summary.get("upheld"),
            "refuted": summary.get("refuted"),
            "undetermined": summary.get("undetermined"),
            "votes": summary.get("votes_per_claim"), "quorum": summary.get("quorum"),
        })
    return rows


def section_timing(events):
    """Are the bounds right? Watchdog aborts and timeouts, against durations."""
    runs = [e for e in events if e.get("kind") == "run"]
    if not runs:
        return []
    secs = metrics.Agg()
    for event in runs:
        secs.add(event.get("elapsed_s"))
    durations = sorted(e.get("elapsed_s") or 0 for e in runs)
    return [{
        "runs": len(runs),
        "timeouts_124": len([e for e in runs if e.get("exit") == 124]),
        "watchdog_125": len([e for e in runs if e.get("exit") == 125]),
        "median_s": durations[len(durations) // 2],
        "max_s": durations[-1],
        "unwatched": len([e for e in runs if not e.get("stall_watchdog")]),
    }]


def section_health(events):
    """How often was the infrastructure - not the model - the thing that failed?"""
    runs = [e for e in events if e.get("kind") == "run"]
    rounds = [e for e in events if e.get("kind") == "round"]
    return [{
        "runs": len(runs),
        "unmeasured_cost": len([e for e in runs if not e.get("measured")]),
        "schema_failures_3": len([e for e in runs if e.get("exit") == 3]),
        "engine_missing_127": len([e for e in runs if e.get("exit") == 127]),
        "rounds": len(rounds),
        "rounds_undetermined": len([e for e in rounds
                                    if e.get("coverage") == "undetermined"]),
    }]


def section_drift(events):
    """Did we get the model we asked for?

    Born from a measured surprise: a review launched with `--model haiku` was
    billed as `claude-sonnet-5` - twice, reproducibly - at ~$0.27 for a two-line
    file. The session even reported `init.model: claude-haiku-4-5`, so only the
    BILLED model exposed it. Nothing in the flow would ever have noticed.

    A row here means the engine did not honour the pin, or billed a different
    model than it started with. Either way you are paying a price you did not
    choose, so this is worth checking before trusting any per-model cost figure.
    """
    rows = {}
    for event in events:
        if event.get("kind") != "run":
            continue
        asked, billed = event.get("model"), event.get("model_billed")
        if not asked or not billed or asked in billed:
            continue
        key = (event.get("engine"), asked, billed)
        entry = rows.setdefault(key, {"engine": key[0], "asked": asked,
                                      "billed": billed, "runs": 0,
                                      "_cost": metrics.Agg()})
        entry["runs"] += 1
        entry["_cost"].add(event.get("cost_usd"))
    out = []
    for entry in rows.values():
        cost = entry.pop("_cost")
        entry["cost"] = cost.fmt(" USD")
        out.append(entry)
    return out


SECTIONS = {
    "drift": ("Did we get the model we asked for", section_drift),
    "routing": ("Which engine to route work to", section_routing),
    "reviewers": ("Which reviewer's findings are worth adjudicating", section_reviewers),
    "lenses": ("Which lenses earn their slot", section_lenses),
    "votes": ("Is the refutation vote count right", section_votes),
    "timing": ("Do the timeouts fit reality", section_timing),
    "health": ("Was the infrastructure the problem", section_health),
}


def table(rows):
    if not rows:
        return "  (no data yet)"
    cols = list(rows[0])
    width = {c: max(len(str(c)), max(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    out = ["  " + "  ".join(str(c).ljust(width[c]) for c in cols),
           "  " + "  ".join("-" * width[c] for c in cols)]
    for row in rows:
        out.append("  " + "  ".join(str(row.get(c, "")).ljust(width[c]) for c in cols))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(prog="metrics-report.py")
    ap.add_argument("--dir", default=".")
    ap.add_argument("--section", choices=sorted(SECTIONS))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    events = metrics.read(args.dir)
    if not events:
        print("no metrics recorded yet at %s" % metrics.store_path(args.dir), file=sys.stderr)
        return 2

    wanted = [args.section] if args.section else list(SECTIONS)
    result = {name: SECTIONS[name][1](events) for name in wanted}

    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    print("%d event(s) from %s\n" % (len(events), metrics.store_path(args.dir)))
    for name in wanted:
        print("== %s - %s" % (name, SECTIONS[name][0]))
        print(table(result[name]))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
