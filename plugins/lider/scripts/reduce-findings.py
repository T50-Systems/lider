#!/usr/bin/env python3
"""reduce-findings.py — merge a fan-out of independent reviews into one round.

N reviewers looking through different lenses (and, ideally, different engine
families) produce N findings files. This folds them into a single round with
provenance, so the adjudicator sees one deduplicated list where each item carries
*who* found it and *how many independent engines* did.

Three things it refuses to do quietly:

  1. COUNT A MISSING REVIEWER AS A CLEAN ONE. A lens that crashed, timed out, or
     returned garbage is recorded in `missing[]` and forces the round's
     `coverage` to `undetermined`. "I could not run the security lens" is not
     "security found nothing" — at fan-out scale that mistake looks like broad
     coverage, which is why it is worth catching here rather than downstream.

  2. TREAT CORROBORATION AS ENGINE-COUNT WHEN IT IS LENS-COUNT. Two lenses on the
     SAME engine agreeing is one engine's opinion twice: same training, same blind
     spots. `engines` and `lenses` are counted separately and reported separately.

  3. AVERAGE AWAY A DISAGREEMENT. When reviewers rate the same defect
     differently, the round keeps the HIGHEST severity and records the spread.
     A BLOCKER that one reviewer called a NIT is still worth a human's attention.

Usage:
  reduce-findings.py --out round.json in1.json in2.json ...
  reduce-findings.py --out round.json --manifest manifest.json    # includes failures

The manifest (written by fanout.sh) is what makes point 1 possible: it lists every
lens that was LAUNCHED, so a lens with no output file can be told apart from a
lens that was never asked for.

Exit codes:  0 merged, full coverage  |  2 merged but coverage undetermined  |  3 usage
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lider.findings import (SEVERE, SEVERITY_RANK, key, same_defect,  # noqa: E402
                            worst)

VERDICT_RANK = {"request_changes": 0, "approve_with_nits": 1, "approve": 2}

OK, UNDETERMINED, USAGE = 0, 2, 3


def load_report(path):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if isinstance(doc, list):
        return {"engine": None, "verdict": None, "findings": doc}
    if not isinstance(doc, dict) or not isinstance(doc.get("findings"), list):
        raise ValueError("%s: no findings array" % path)
    return doc


def main():
    ap = argparse.ArgumentParser(prog="reduce-findings.py")
    ap.add_argument("--out", required=True)
    ap.add_argument("--manifest", help="fanout manifest listing every launched lens")
    ap.add_argument("inputs", nargs="*")
    args = ap.parse_args()

    # Sources: either an explicit list, or the manifest (which also knows about
    # the lenses that produced nothing).
    sources, missing = [], []
    if args.manifest:
        with open(args.manifest, encoding="utf-8") as fh:
            manifest = json.load(fh)
        for entry in manifest.get("lenses", []):
            out = entry.get("out")
            if out and os.path.exists(out) and os.path.getsize(out) > 0:
                sources.append((entry.get("lens"), entry.get("engine"), out))
            else:
                missing.append({
                    "lens": entry.get("lens"), "engine": entry.get("engine"),
                    "exit": entry.get("exit"), "reason": entry.get("reason") or "no output",
                })
    for path in args.inputs:
        sources.append((os.path.splitext(os.path.basename(path))[0], None, path))

    if not sources and not missing:
        print("reduce-findings: nothing to merge", file=sys.stderr)
        return USAGE

    flat, reviewers = [], []
    for lens, engine, path in sources:
        try:
            doc = load_report(path)
        except (OSError, ValueError) as exc:
            # A file we cannot parse is a reviewer we did not hear from, not an
            # empty review. Same rule as a crashed one.
            missing.append({"lens": lens, "engine": engine, "reason": str(exc)})
            continue
        engine = engine or doc.get("engine") or "unknown"
        reviewers.append({"lens": lens, "engine": engine, "verdict": doc.get("verdict"),
                          "count": len(doc["findings"])})
        for item in doc["findings"]:
            flat.append({
                "severity": item.get("severity", "MINOR"),
                "summary": item.get("summary", ""),
                "location": item.get("location"),
                "suggestion": item.get("suggestion"),
                "_lens": lens, "_engine": engine,
                "_key": key(item),
            })

    # Cluster. O(n^2) against cluster heads — review rounds are tens of findings,
    # not thousands, and an exact-but-simple rule beats a clever opaque one here.
    clusters = []
    for item in flat:
        for cluster in clusters:
            if same_defect(cluster["head"]["_key"], item["_key"]):
                cluster["items"].append(item)
                break
        else:
            clusters.append({"head": item, "items": [item]})

    merged = []
    for cluster in clusters:
        items = cluster["items"]
        severities = [i["severity"] for i in items]
        top = worst(severities)
        engines = sorted({i["_engine"] for i in items})
        lenses = sorted({i["_lens"] for i in items})
        # The clearest phrasing among the duplicates, not just the first one seen.
        best = max(items, key=lambda i: len(i["summary"] or ""))
        merged.append({
            "severity": top,
            "summary": best["summary"],
            "location": best["location"],
            "suggestion": next((i["suggestion"] for i in items if i["suggestion"]), None),
            "corroboration": {
                "engines": len(engines), "lenses": len(lenses),
                "found_by_engines": engines, "found_by_lenses": lenses,
                "severity_spread": sorted(set(severities),
                                          key=lambda s: SEVERITY_RANK.get(s, 9)),
            },
        })

    # Most severe first, then best-corroborated: what an adjudicator should read first.
    merged.sort(key=lambda f: (SEVERITY_RANK.get(f["severity"], 9),
                               -f["corroboration"]["engines"],
                               -f["corroboration"]["lenses"]))

    # What each lens contributed that NO other lens found. This is the number
    # that decides whether a lens earns its slot: one that only ever echoes
    # another lens costs a full engine call and adds nothing.
    for reviewer in reviewers:
        reviewer["unique"] = 0
        reviewer["shared"] = 0
    index = {r["lens"]: r for r in reviewers}
    for cluster in clusters:
        lenses = {i["_lens"] for i in cluster["items"]}
        field = "unique" if len(lenses) == 1 else "shared"
        for lens in lenses:
            if lens in index:
                index[lens][field] += 1

    verdicts = [r["verdict"] for r in reviewers if r["verdict"] in VERDICT_RANK]
    verdict = min(verdicts, key=lambda v: VERDICT_RANK[v]) if verdicts else "request_changes"
    coverage = "complete" if not missing else "undetermined"

    round_doc = {
        "engine": "fanout(%s)" % ",".join(sorted({r["engine"] for r in reviewers})) if reviewers else "fanout()",
        "verdict": verdict,
        "coverage": coverage,
        "reviewers": reviewers,
        "missing": missing,
        "findings": merged,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(round_doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    dupes = len(flat) - len(merged)
    print("merged %d findings from %d reviewer(s) -> %d unique (%d duplicate%s collapsed)"
          % (len(flat), len(reviewers), len(merged), dupes, "" if dupes == 1 else "s"))
    multi = [f for f in merged if f["corroboration"]["engines"] > 1]
    if multi:
        print("corroborated by >1 engine: %d" % len(multi))
    for reviewer in reviewers:
        print("  %-14s %2d unique, %2d shared  (%s)"
              % (reviewer["lens"], reviewer["unique"], reviewer["shared"], reviewer["engine"]))
    if missing:
        # Deliberately loud, and the exit code says it too.
        print("COVERAGE UNDETERMINED - %d lens(es) produced nothing:" % len(missing),
              file=sys.stderr)
        for m in missing:
            print("  %s/%s: %s" % (m.get("lens"), m.get("engine") or "?", m["reason"]),
                  file=sys.stderr)
        print("This round did NOT cover what those lenses were for. Do not read it "
              "as 'nothing found there'.", file=sys.stderr)
        return UNDETERMINED
    return OK


if __name__ == "__main__":
    sys.exit(main())
