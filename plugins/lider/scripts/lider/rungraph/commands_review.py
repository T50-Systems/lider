"""Findings ingest, adjudication decisions, handoff import."""
import json
import os
import sys
import time

from lider import findings as fx

from .constants import KIND_INCEPTION, OK, REFUSED, UNDETERMINED, USAGE
from .handoff import load_handoff
from .model import new_unit, scope_of
from .storage import commit, need

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

def cmd_import(args):
    """Construction: load a sealed inception handoff into this run (operational path)."""
    root, (rid, state) = args.dir, need(args.dir, args.run)
    if state.get("kind") == KIND_INCEPTION:
        print("rungraph: import is for construction runs, not inception", file=sys.stderr)
        return REFUSED
    if state["node"] != "init" and not args.force:
        print("rungraph: import only from node 'init' (now at '%s'); --force to override"
              % state["node"], file=sys.stderr)
        return REFUSED
    path = os.path.abspath(args.handoff)
    try:
        doc = load_handoff(path)
    except (OSError, ValueError) as exc:
        print("rungraph: cannot import handoff: %s" % exc, file=sys.stderr)
        return UNDETERMINED

    state["criteria"] = list(doc.get("criteria") or [])
    state["questions"] = list(doc.get("questions") or [])
    # Units re-enter as pending subgraphs; construction will walk them.
    units = []
    for raw in doc.get("units") or []:
        unit = new_unit(raw["id"], raw.get("title", ""),
                        list(raw.get("depends_on") or []),
                        state["max_rounds"])
        unit["covers"] = list(raw.get("covers") or [])
        units.append(unit)
    state["units"] = units
    state["handoff"] = {
        "path": path,
        "sha256": doc["sha256"],
        "id": doc.get("id"),
        "inception_run": doc.get("inception_run"),
        "imported_at": int(time.time()),
    }
    # Frame text is reference only; construction still pins its own build spec.
    if doc.get("frame") and doc["frame"].get("text") is not None:
        state["inception_frame"] = {
            "path": doc["frame"].get("path"),
            "sha256": doc["frame"].get("sha256"),
            "bytes": doc["frame"].get("bytes"),
            "text": doc["frame"].get("text"),
            "at": int(time.time()),
        }
    commit(root, rid, state, "import", handoff=doc.get("id"), path=path,
           sha256=doc["sha256"][:12], units=len(units),
           criteria=len(state["criteria"]))
    print("imported handoff '%s' (%s) - %d criterion/a, %d unit(s). "
          "Pin the BUILD spec next (`spec --file`), then enter the construction graph."
          % (doc.get("id"), doc["sha256"][:12], len(state["criteria"]), len(units)))
    return OK
