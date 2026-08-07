"""G4: audit snapshot — structure vs content pins (no engine launch)."""
from __future__ import annotations

import json
import os
import sys
import time

from .constants import KIND_CONSTRUCTION, OK, graph_for
from .model import open_severe, unfinished_units
from .show import artifact_lines
from .storage import is_strict, need


def _plugin_version():
    """Best-effort version from plugins/lider/plugin.json (structure pin)."""
    here = os.path.dirname(os.path.abspath(__file__))
    # .../scripts/lider/rungraph -> .../plugins/lider/plugin.json
    candidate = os.path.normpath(os.path.join(here, "..", "..", "..", "plugin.json"))
    try:
        with open(candidate, encoding="utf-8") as fh:
            return json.load(fh).get("version")
    except (OSError, ValueError, TypeError):
        return None


def build_snapshot(state, rid, root=None):
    """Pure dict: structure half + content pins + artifact checklist."""
    kind = state.get("kind") or KIND_CONSTRUCTION
    node = state.get("node")
    g = graph_for(state)
    legal = list(g.get(node, []))
    # Structure fingerprint: sorted edge table (stable, independent of run content)
    structure_edges = {src: list(dsts) for src, dsts in sorted(g.items())}

    spec = state.get("spec") or {}
    roles = {
        role: {
            "engine": info.get("engine"),
            "model": info.get("model"),
            "family": info.get("family"),
            "forced": bool(info.get("forced")),
        }
        for role, info in (state.get("roles") or {}).items()
    }
    severe = open_severe(state)
    for u in state.get("units") or []:
        severe.extend(
            dict(f, id="%s/%s" % (u["id"], f["id"])) for f in open_severe(u)
        )

    arts = []
    for label, hint in artifact_lines(state):
        arts.append({"line": label, "hint": hint})

    handoff = state.get("handoff")
    handoff_out = state.get("handoff_out")
    plan = state.get("session_plan")

    return {
        "kind": "lider.run.snapshot",
        "version": 1,
        "at": int(time.time()),
        "run_id": rid,
        "title": state.get("title"),
        "plugin_version": _plugin_version(),
        "structure": {
            "run_kind": kind,
            "strict": is_strict(state),
            "node": node,
            "path": list(state.get("path") or []),
            "legal_next": legal,
            "edges": structure_edges,
            "unit_count": len(state.get("units") or []),
            "units_open": [u["id"] for u in unfinished_units(state)],
        },
        "content": {
            "spec_sha256": (spec.get("sha256") or None),
            "spec_path": (spec.get("path") or None),
            "spec_bytes": spec.get("bytes"),
            "roles": roles,
            "criteria": [
                {"id": c["id"], "status": c.get("status")}
                for c in (state.get("criteria") or [])
            ],
            "open_questions": [
                q["id"] for q in (state.get("questions") or [])
                if q.get("status") == "open"
            ],
            "findings_open_severe": [
                {"id": f["id"], "severity": f.get("severity")} for f in severe
            ],
            "rounds": len(state.get("rounds") or []),
            "handoff_in_sha256": (handoff or {}).get("sha256"),
            "handoff_out_sha256": (handoff_out or {}).get("sha256"),
            "session_plan_path": (plan or {}).get("path"),
            "target": state.get("target"),
        },
        "artifacts": arts,
    }


def cmd_snapshot(args):
    """Export structure + content pins for audit (G4). Does not mutate the run."""
    _root, (rid, state) = args.dir, need(args.dir, args.run)
    snap = build_snapshot(state, rid, root=args.dir)

    out = getattr(args, "out", None)
    if out:
        out = os.path.abspath(out)
        parent = os.path.dirname(out)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print("snapshot written: %s" % out)

    if args.json:
        json.dump(snap, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return OK

    if out:
        return OK

    # human text
    s, c = snap["structure"], snap["content"]
    print("snapshot run %s — %s" % (rid, snap.get("title")))
    print("plugin: %s" % (snap.get("plugin_version") or "?"))
    print("STRUCTURE  kind=%s  strict=%s  node=%s"
          % (s["run_kind"], s["strict"], s["node"]))
    print("  path: %s" % " -> ".join(s["path"]))
    print("  legal next: %s" % (", ".join(s["legal_next"]) or "(terminal)"))
    if s["unit_count"]:
        print("  units: %d open=%s"
              % (s["unit_count"], ",".join(s["units_open"]) or "(none)"))
    print("CONTENT")
    print("  spec: %s" % ((c["spec_sha256"] or "NOT PINNED")[:12]
                          if c["spec_sha256"] else "NOT PINNED"))
    if c["roles"]:
        for role, info in c["roles"].items():
            print("  role %-12s %s [%s]" % (role, info.get("engine"),
                                            info.get("family") or "?"))
    if c["findings_open_severe"]:
        print("  open severe: %s"
              % ", ".join(f["id"] for f in c["findings_open_severe"]))
    if snap["artifacts"]:
        print("ARTIFACTS")
        for row in snap["artifacts"]:
            print("  %s" % row["line"])
            if row.get("hint"):
                print("       → %s" % row["hint"])
    print("tip: snapshot --json | --out <path> for a durable audit blob")
    return OK
