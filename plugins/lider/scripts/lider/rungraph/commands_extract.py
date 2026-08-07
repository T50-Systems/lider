"""CLI: extract session log → plan; apply-plan → ledger (inception/construction)."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

from .constants import (
    KIND_CONSTRUCTION,
    KIND_INCEPTION,
    KIND_OPERATIONS,
    OK,
    REFUSED,
    UNDETERMINED,
    USAGE,
)
from .extract import extract_plan, load_plan_file, write_plan
from .model import find_by_id, find_unit, new_unit
from .storage import commit, load, need, save


def _plans_dir(root):
    return os.path.join(root, ".lider", "plans")


def _default_plan_path(root, rid_or_title):
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in (rid_or_title or "plan"))
    return os.path.join(_plans_dir(root), "%s.plan.json" % safe[:64])


def cmd_extract(args):
    """Parse a session log / structured JSON into a lider.session.plan file.

    Does NOT write the ledger. Use `apply-plan` (or --apply) to seed a run.
    """
    root = args.dir
    path = os.path.abspath(args.file)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        print("rungraph: cannot read %s: %s" % (path, exc), file=sys.stderr)
        return UNDETERMINED

    plan = extract_plan(text, source_path=path)
    out = args.out or _default_plan_path(root, plan.get("title") or "session")
    out = os.path.abspath(out)
    write_plan(out, plan)

    if args.json:
        json.dump(plan, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print("plan written: %s" % out)
        print("coverage: %s    mode: %s" % (plan["coverage"], plan["mode"]))
        print("criteria: %d    questions: %d    units: %d"
              % (len(plan["criteria"]), len(plan["questions"]), len(plan["units"])))
        for n in plan.get("notes") or []:
            print("  note: %s" % n)
        if plan["coverage"] == "undetermined":
            print("rungraph: WARNING: plan is a frame shell only — fill criteria/units "
                  "before seal, or hand-author a structured plan JSON.", file=sys.stderr)

    if getattr(args, "apply", False):
        # Re-dispatch into apply-plan with the plan we just wrote.
        class _A:
            pass
        a = _A()
        a.dir = root
        a.run = args.run
        a.plan = out
        a.force = bool(getattr(args, "force", False))
        a.frame_out = getattr(args, "frame_out", None)
        a.enter_spec = bool(getattr(args, "enter_spec", False))
        a.title = getattr(args, "title", None) or plan.get("title")
        a.kind = getattr(args, "kind", None) or KIND_INCEPTION
        a.init = bool(getattr(args, "init", False) or not args.run)
        a.max_rounds = getattr(args, "max_rounds", 3)
        a.strict = bool(getattr(args, "strict", False))
        return cmd_apply_plan(a)
    return OK if plan["coverage"] != "undetermined" else UNDETERMINED


def _ensure_run(args):
    """Return (root, rid, state). Optionally init a new inception/construction run."""
    root = args.dir
    rid = args.run
    if getattr(args, "init", False) or not rid:
        rid = rid or ("from-log-%d" % int(time.time()))
        if load(root, rid) and not args.force:
            print("rungraph: run '%s' already exists (pass --force or another --run)"
                  % rid, file=sys.stderr)
            return None
        kind = getattr(args, "kind", None) or KIND_INCEPTION
        if kind == KIND_OPERATIONS:
            print("rungraph: apply-plan is for inception or construction, not operations",
                  file=sys.stderr)
            return None
        # Minimal init (mirror cmd_init fields)
        from .constants import SCHEMA_VERSION
        from .storage import env_strict
        strict = bool(getattr(args, "strict", False) or env_strict())
        state = {
            "schema_version": SCHEMA_VERSION,
            "run_id": rid,
            "title": args.title or "from session log",
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
            "max_rounds": getattr(args, "max_rounds", None) or 3,
            "events": [],
            "handoff": None,
            "handoff_out": None,
            "target": None,
        }
        save(root, rid, state)
        ignore = os.path.join(root, ".lider", ".gitignore")
        if not os.path.exists(ignore):
            with open(ignore, "w", encoding="utf-8") as fh:
                fh.write("*\n")
        print("run '%s' initialised (kind=%s) for apply-plan" % (rid, kind))
        return root, rid, state

    rid, state = need(root, rid)
    return root, rid, state


def cmd_apply_plan(args):
    """Seed a run from a lider.session.plan: frame pin + criteria + questions + units.

    Does NOT seal a handoff and does NOT enter sealed. After apply, review the
    frame, fix mapping, optionally challenge, then `enter sealed` as usual.
    """
    plan_path = os.path.abspath(args.plan)
    try:
        plan = load_plan_file(plan_path)
    except (OSError, ValueError) as exc:
        print("rungraph: cannot load plan: %s" % exc, file=sys.stderr)
        return UNDETERMINED

    ensured = _ensure_run(args)
    if ensured is None:
        return REFUSED
    root, rid, state = ensured

    if state.get("kind") == KIND_OPERATIONS:
        print("rungraph: apply-plan cannot target an operations run", file=sys.stderr)
        return REFUSED

    # Write frame markdown and pin as spec (same shape as cmd_spec).
    frame_text = plan.get("frame_markdown") or ""
    if not frame_text.strip():
        print("rungraph: plan has empty frame_markdown", file=sys.stderr)
        return REFUSED

    frame_out = args.frame_out or os.path.join(
        root, ".lider", "runs", rid, "frame.from-log.md"
    )
    frame_out = os.path.abspath(frame_out)
    os.makedirs(os.path.dirname(frame_out), exist_ok=True)
    with open(frame_out, "w", encoding="utf-8") as fh:
        fh.write(frame_text if frame_text.endswith("\n") else frame_text + "\n")

    text = open(frame_out, encoding="utf-8").read()
    # Soft section check (same spirit as cmd_spec for inception)
    missing = [s for s in ("scope", "constraint") if s not in text.lower()]
    if missing and not args.force:
        print("rungraph: frame missing section(s): %s (use --force to accept)"
              % ", ".join(missing), file=sys.stderr)
        return REFUSED

    state["spec"] = {
        "path": frame_out,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "bytes": len(text.encode("utf-8")),
        "text": text,
        "at": int(time.time()),
    }

    # Criteria
    added_c = 0
    for c in plan.get("criteria") or []:
        cid, ctext = c.get("id"), c.get("text")
        if not cid or not ctext:
            continue
        existing = find_by_id(state.get("criteria"), cid)
        if existing and not args.force:
            continue
        if existing:
            existing["text"] = ctext
            existing["status"] = "required"
        else:
            state.setdefault("criteria", []).append({
                "id": cid, "text": ctext, "status": "required",
                "reason": None, "at": int(time.time()),
            })
            added_c += 1

    # Questions (open by default; answered/assumed if plan says so)
    added_q = 0
    for q in plan.get("questions") or []:
        qtext = (q.get("text") or "").strip()
        if not qtext:
            continue
        # skip exact duplicate open questions
        if any(x.get("text") == qtext for x in state.get("questions") or []):
            if not args.force:
                continue
        qid = "q%d" % (len(state.setdefault("questions", [])) + 1)
        status = q.get("status") or "open"
        answer = q.get("answer")
        if status == "assumed" and not answer:
            status = "open"
        state["questions"].append({
            "id": qid, "text": qtext, "status": status,
            "answer": answer, "unit": q.get("unit"),
            "at": int(time.time()),
        })
        added_q += 1

    # Units
    added_u = 0
    for u in plan.get("units") or []:
        uid = u.get("id")
        if not uid:
            continue
        existing = find_unit(state, uid)
        covers = list(u.get("covers") or [])
        depends = list(u.get("depends_on") or [])
        if existing and not args.force:
            # merge covers/depends only
            existing["covers"] = list(dict.fromkeys(
                list(existing.get("covers") or []) + covers))
            existing["depends_on"] = list(dict.fromkeys(
                list(existing.get("depends_on") or []) + depends))
            if u.get("title"):
                existing["title"] = u["title"]
            continue
        if existing:
            existing["title"] = u.get("title") or existing.get("title") or uid
            existing["covers"] = covers
            existing["depends_on"] = depends
        else:
            unit = new_unit(uid, u.get("title") or uid, depends,
                            state.get("max_rounds") or 3)
            unit["covers"] = covers
            state.setdefault("units", []).append(unit)
            added_u += 1

    # Bookkeeping pointer (not a sealed handoff)
    state["session_plan"] = {
        "path": plan_path,
        "sha256": plan.get("source", {}).get("sha256") or "",
        "plan_kind": plan.get("kind"),
        "coverage": plan.get("coverage"),
        "applied_at": int(time.time()),
    }

    commit(root, rid, state, "apply-plan",
           plan=plan_path, coverage=plan.get("coverage"),
           criteria=added_c, questions=added_q, units=added_u,
           frame=frame_out)

    print("applied plan → run '%s' (kind=%s, node=%s)" % (rid, state.get("kind"), state["node"]))
    print("  frame: %s" % frame_out)
    print("  +%d criteria, +%d questions, +%d units (coverage=%s)"
          % (added_c, added_q, added_u, plan.get("coverage")))
    for n in plan.get("notes") or []:
        print("  note: %s" % n)
    print("next: review frame + mapping, then `enter spec` (or challenge) and seal "
          "when ready — apply-plan does NOT seal.")

    if getattr(args, "enter_spec", False) and state["node"] == "init":
        # Soft advance: only init → spec is always legal for inception/construction.
        state["node"] = "spec"
        state.setdefault("path", []).append("spec")
        commit(root, rid, state, "enter", **{"from": "init", "to": "spec",
                                             "forced": False, "via": "apply-plan"})
        print("entered node 'spec' (--enter-spec)")

    if plan.get("coverage") == "undetermined":
        return UNDETERMINED
    return OK
