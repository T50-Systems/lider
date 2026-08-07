"""Inception handoff seal / load / digest."""
import hashlib
import json
import os
import tempfile
import time

from .constants import (
    HANDOFF_KIND,
    HANDOFF_VERSION,
    OK,
    REFUSED,
    UNDETERMINED,
)
from .model import open_questions, uncovered_criteria
from .storage import challenged, handoff_path, is_strict

def check_seal(state, force):
    """May this inception run seal a handoff? Pure - no writes."""
    if force:
        return OK, None
    if not state.get("spec"):
        return UNDETERMINED, (
            "cannot seal - no frame is pinned. Pin discovery with `spec --file`.")
    pending = open_questions(state)
    if pending:
        return UNDETERMINED, (
            "cannot seal - %d open question(s): %s. Answer or assume with --answer."
            % (len(pending), ", ".join(q["id"] for q in pending)))
    if not state.get("criteria"):
        return REFUSED, (
            "cannot seal - no acceptance criteria. Declare with `criterion add`, or "
            "this handoff has nothing Construction can check coverage against.")
    missing = uncovered_criteria(state)
    if missing:
        return REFUSED, (
            "cannot seal - %d required criterion/criteria covered by no unit: %s. "
            "Declare a unit with --covers, or `criterion defer --reason ...`. NOTE: "
            "this checks the MAPPING only, not that anything was designed well."
            % (len(missing), ", ".join(c["id"] for c in missing)))
    if not challenged(state):
        if is_strict(state):
            return REFUSED, (
                "STRICT: cannot seal without a challenge. `enter challenge` (and usually "
                "assign a challenger from another family), or record "
                "`check --name challenge --verdict ok --evidence ...`, then "
                "`enter sealed`, or --force.")
        # Non-strict: allowed; caller prints a warning (evaluate stays pure).
    return OK, None


def build_handoff_body(state, rid):
    """Canonical handoff dict (no self-hash yet)."""
    spec = state.get("spec") or {}
    return {
        "kind": HANDOFF_KIND,
        "version": HANDOFF_VERSION,
        "id": rid,
        "sealed_at": int(time.time()),
        "inception_run": rid,
        "strict": bool(state.get("strict")),
        "frame": {
            "path": spec.get("path"),
            "sha256": spec.get("sha256"),
            "bytes": spec.get("bytes"),
            "text": spec.get("text"),
            "at": spec.get("at") or int(time.time()),
        },
        "criteria": list(state.get("criteria") or []),
        "questions": [q for q in (state.get("questions") or [])
                      if q.get("status") != "open"],
        "units": [{
            "id": u["id"], "title": u.get("title", ""),
            "covers": list(u.get("covers") or []),
            "depends_on": list(u.get("depends_on") or []),
        } for u in (state.get("units") or [])],
        "challenge": {
            "done": challenged(state),
            "path_includes_challenge": "challenge" in (state.get("path") or []),
        },
        "roles": dict(state.get("roles") or {}),
    }


def handoff_digest(body):
    """Stable hash of the handoff without the sha256 field itself."""
    payload = {k: v for k, v in body.items() if k != "sha256"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def write_handoff(root, state, rid):
    """Seal to .lider/handoffs/<id>.json. Returns (path, sha256)."""
    body = build_handoff_body(state, rid)
    digest = handoff_digest(body)
    body["sha256"] = digest
    path = handoff_path(root, rid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(body, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        os.path.exists(tmp) and os.unlink(tmp)
        raise
    return path, digest


def load_handoff(path):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("kind") != HANDOFF_KIND:
        raise ValueError("not a lider inception handoff (kind=%r)" % doc.get("kind"))
    stored = doc.get("sha256")
    if not stored:
        raise ValueError("handoff has no sha256")
    if handoff_digest(doc) != stored:
        raise ValueError("handoff sha256 mismatch - file was modified after seal")
    return doc
