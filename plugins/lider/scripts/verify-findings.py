#!/usr/bin/env python3
"""verify-findings.py — apply adversarial refutation votes to a fan-out round.

A fan-out of finders raises recall and lowers precision: more eyes produce more
plausible-but-wrong findings. Each BLOCKER/MAJOR claim is therefore put to N
independent skeptics whose instruction is to REFUTE it. This counts their ballots
and marks each claim.

The counting rules, and why they are these:

  * A claim is DROPPED when a strict majority of the ballots cast refute it.
    Not a plurality: a claim killed by 1 of 3 with the other two abstaining has
    not been disproven.

  * A LOW-CONFIDENCE refutation does not count as a refutation. The skeptics are
    told to answer refuted=false with confidence=low when they could not
    establish either way; letting that kill a real BLOCKER would make the
    adversarial pass a defect-hiding machine.

  * MISSING BALLOTS ARE NOT ABSTENTIONS IN FAVOUR OF ANYTHING. If fewer than a
    quorum of votes came back, the claim is marked `undetermined` and KEPT — a
    verification we could not run is not a verification that passed, and
    silently dropping a claim because its skeptics crashed is the worst possible
    version of that mistake.

Output: the round with a `refutation` block on every judged finding, findings
dropped only when genuinely refuted, and a `dropped[]` list so nothing vanishes
without a trace.

Exit codes:  0 all claims judged  |  2 at least one claim undetermined  |  3 usage
"""
import argparse
import glob
import json
import os
import sys

SEVERE = ("BLOCKER", "MAJOR")
OK, UNDETERMINED, USAGE = 0, 2, 3


def read_ballots(directory, index):
    ballots, missing = [], 0
    for path in sorted(glob.glob(os.path.join(directory, "refute-%d-*.json" % index))):
        if path.endswith(".exit"):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            missing += 1
            continue
        if not isinstance(doc, dict) or "refuted" not in doc:
            missing += 1
            continue
        ballots.append(doc)
    return ballots, missing


def main():
    ap = argparse.ArgumentParser(prog="verify-findings.py")
    ap.add_argument("--round", required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--votes", type=int, default=3, help="ballots requested per claim")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    try:
        with open(args.round, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        print("verify-findings: %s" % exc, file=sys.stderr)
        return USAGE

    # Quorum: a strict majority of the ballots we asked for must come back before
    # a verdict means anything.
    quorum = args.votes // 2 + 1
    # Keep the position in the original list, not the object: claim N in the
    # refute-N-*.json filenames is the Nth SEVERE finding, and matching by
    # identity would be one aliasing bug away from judging the wrong claim.
    severe_idx = [i for i, f in enumerate(doc.get("findings", []))
                  if f.get("severity") in SEVERE]
    severe = [doc["findings"][i] for i in severe_idx]
    kept, dropped, undetermined = [], [], 0

    judged = {}
    for i, finding in enumerate(severe):
        ballots, lost = read_ballots(args.dir, i)
        # Only a confident refutation counts as one.
        refutes = [b for b in ballots if b.get("refuted") and b.get("confidence") != "low"]
        block = {
            "ballots_requested": args.votes,
            "ballots_returned": len(ballots),
            "ballots_lost": lost + max(0, args.votes - len(ballots) - lost),
            "refuted_by": len(refutes),
            "reasons": [b.get("reason", "")[:200] for b in refutes],
            "engines": sorted({b.get("engine", "?") for b in ballots}),
        }
        if len(ballots) < quorum:
            block["status"] = "undetermined"
            undetermined += 1
        elif len(refutes) > len(ballots) / 2.0:
            block["status"] = "refuted"
        else:
            block["status"] = "upheld"
        judged[severe_idx[i]] = block

    out_findings = []
    for pos, finding in enumerate(doc.get("findings", [])):
        block = judged.get(pos)
        if block is None:
            out_findings.append(finding)          # MINOR/NIT are not put to a vote
            continue
        enriched = dict(finding, refutation=block)
        if block["status"] == "refuted":
            dropped.append(enriched)
        else:
            out_findings.append(enriched)
            kept.append(enriched)

    doc["findings"] = out_findings
    doc["dropped"] = dropped
    doc["refutation_summary"] = {
        "claims": len(severe), "upheld": len([k for k in kept
                                              if k["refutation"]["status"] == "upheld"]),
        "refuted": len(dropped), "undetermined": undetermined,
        "votes_per_claim": args.votes, "quorum": quorum,
    }
    if undetermined:
        doc["coverage"] = "undetermined"

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    summary = doc["refutation_summary"]
    print("refutation: %d claim(s) - %d upheld, %d refuted and dropped, %d undetermined"
          % (summary["claims"], summary["upheld"], summary["refuted"], summary["undetermined"]))
    for item in dropped:
        print("  dropped: %s  (%d/%d refuted)"
              % (item["summary"][:60], item["refutation"]["refuted_by"],
                 item["refutation"]["ballots_returned"]))
    if undetermined:
        print("%d claim(s) could not reach quorum and were KEPT, not dropped. "
              "A verification you could not run is not one that passed."
              % undetermined, file=sys.stderr)
        return UNDETERMINED
    return OK


if __name__ == "__main__":
    sys.exit(main())
