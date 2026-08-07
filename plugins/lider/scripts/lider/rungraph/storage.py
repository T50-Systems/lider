"""Durable run.json storage and strict-mode helpers."""
import json
import os
import sys
import tempfile
import time

from .constants import USAGE

def runs_dir(root):
    return os.path.join(root, ".lider", "runs")


def handoffs_dir(root):
    """Operational sealed handoffs - under .lider (gitignored with the rest)."""
    return os.path.join(root, ".lider", "handoffs")


def handoff_path(root, handoff_id):
    return os.path.join(handoffs_dir(root), "%s.json" % handoff_id)


def run_path(root, run_id):
    return os.path.join(runs_dir(root), run_id, "run.json")


def check_named(state, *names):
    """First matching check by name, or None."""
    checks = state.get("checks") or {}
    for name in names:
        if name in checks:
            return checks[name]
    return None


def check_verdict_ok(state, *names):
    chk = check_named(state, *names)
    return bool(chk and chk.get("verdict") == "ok")


def env_strict():
    return os.environ.get("LIDER_STRICT", "").strip().lower() in ("1", "true", "yes", "on")


def is_strict(state):
    """Per-run flag wins; else LIDER_STRICT. Stored at init so it survives the session."""
    if state.get("strict"):
        return True
    return env_strict()


def challenged(state):
    """Was a challenge performed? Entering the node, or an ok check named challenge."""
    if "challenge" in (state.get("path") or []):
        return True
    chk = (state.get("checks") or {}).get("challenge")
    return bool(chk and chk.get("verdict") == "ok")


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
