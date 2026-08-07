"""Units, criteria, questions, findings helpers."""
import hashlib
import time

from lider import findings as fx

from .constants import UNIT_TERMINAL

def new_unit(unit_id, title, depends_on, max_rounds):
    return {
        "id": unit_id, "title": title, "depends_on": list(depends_on),
        "covers": [],
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

