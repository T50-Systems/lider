"""Session log / transcript → structured plan (trace-to-graph, no LLM).

Turns free-form discovery notes, chat dumps, or a pre-authored JSON plan into a
`lider.session.plan` document. Heuristics are conservative: missing structure
yields `coverage: undetermined` or partial, never invented AC text.

This is the checkable half of "emergent session → explicit inception units".
Substance (good design) stays in the frame prose; the plan only carries objects
the ledger can enforce (criteria, questions, units, mapping).
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from .constants import PLAN_KIND, PLAN_VERSION

# Section headers we recognise (case-insensitive, optional numbering).
_SECTION = re.compile(
    r"^#{1,3}\s*(?:\d+[.)]\s*)?(?P<title>.+?)\s*$", re.MULTILINE
)
# "- AC1: user can log in"  or  "AC1 — …"  (id includes the AC prefix)
_AC_LINE = re.compile(
    r"^(?:[-*+]|\d+[.)])?\s*"
    r"(?P<id>AC[A-Za-z0-9_-]+)\s*[:.\-–—]\s*(?P<text>.+)$",
    re.IGNORECASE,
)
# "criterion login: …" / "criteria C1: …"
_CRITERION_LINE = re.compile(
    r"^(?:[-*+]|\d+[.)])?\s*"
    r"(?:criterion|criteria)\s*[-_:]?\s*(?P<id>[A-Za-z0-9_-]+)?\s*[:.\-–—]\s*"
    r"(?P<text>.+)$",
    re.IGNORECASE,
)
_AC_INLINE = re.compile(
    r"\b(AC\d+[A-Za-z0-9_-]*)\b\s*[:.\-–—]\s*(.+)$", re.IGNORECASE
)
_UNIT_LINE = re.compile(
    r"^(?:[-*+]|\d+[.)])?\s*"
    r"(?:unit|workstream|work\s*unit)\s*[-_:]?\s*"
    r"(?P<id>[A-Za-z0-9_-]+)\s*[:.\-–—]\s*(?P<title>.+)$",
    re.IGNORECASE,
)
_UNIT_HEADING = re.compile(
    r"^#{2,4}\s*(?:unit\s+)?(?P<id>[a-z][a-z0-9_-]{1,32})\s*[:.\-–—]?\s*(?P<title>.*)$",
    re.IGNORECASE,
)
_DEPENDS = re.compile(
    r"\bdepends?\s*(?:on)?\s*[:\s]\s*(?P<deps>[A-Za-z0-9_,\-\s]+)",
    re.IGNORECASE,
)
_COVERS = re.compile(
    r"\bcovers?\s*[:\s]\s*(?P<covers>[A-Za-z0-9_,\-\s]+)",
    re.IGNORECASE,
)
_CHECKBOX = re.compile(r"^[-*+]\s*\[(?: |x|X)\]\s*(.+)$")
_SLUG = re.compile(r"[^a-z0-9]+")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slug(text: str, fallback: str) -> str:
    s = _SLUG.sub("-", (text or "").strip().lower()).strip("-")
    s = re.sub(r"-+", "-", s)
    if not s:
        return fallback
    return s[:40]


def _split_csv(raw: str):
    return [x.strip() for x in re.split(r"[,;\s]+", raw or "") if x.strip()]


def _sections(text: str):
    """Map lowercased section title -> body text."""
    matches = list(_SECTION.finditer(text))
    if not matches:
        return {"_body": text}
    out = {}
    # preamble before first heading
    if matches[0].start() > 0:
        out["_preamble"] = text[: matches[0].start()].strip()
    for i, m in enumerate(matches):
        title = m.group("title").strip().lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[title] = text[start:end].strip()
    return out


def _pick_section(sections, *names):
    for key, body in sections.items():
        for name in names:
            if name in key:
                return body
    return ""


def _parse_criteria(block: str, notes: list):
    criteria = []
    seen = set()
    for line in (block or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cid, text = "", ""
        m = _AC_LINE.match(line) or _CRITERION_LINE.match(line)
        if m:
            cid = (m.group("id") or "").strip()
            text = (m.group("text") or "").strip()
        else:
            m2 = _AC_INLINE.match(line)
            if m2:
                cid, text = m2.group(1).strip(), m2.group(2).strip()
        if cid or text:
            if not text:
                continue
            if not cid:
                cid = "AC%d" % (len(criteria) + 1)
            if cid.lower().startswith("ac"):
                cid = cid.upper()
            if cid in seen:
                continue
            seen.add(cid)
            criteria.append({"id": cid, "text": text})
            continue
        # checkbox without AC label → still a criterion if under criteria section
        m2 = _CHECKBOX.match(line)
        if m2:
            text = m2.group(1).strip()
            cid = "AC%d" % (len(criteria) + 1)
            while cid in seen:
                cid = "AC%d" % (int(cid[2:]) + 1)
            seen.add(cid)
            criteria.append({"id": cid, "text": text})
    if block and not criteria:
        notes.append("criteria section present but no AC lines matched")
    return criteria


def _parse_questions(block: str):
    questions = []
    for line in (block or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # strip list markers
        line = re.sub(r"^(?:[-*+]|\d+[.)])\s*", "", line)
        line = re.sub(r"^\[(?: |x|X)\]\s*", "", line)
        if "?" in line or line.lower().startswith(("open:", "tbd:", "todo:", "q:")):
            text = re.sub(r"^(open|tbd|todo|q)\s*:\s*", "", line, flags=re.I).strip()
            if text:
                questions.append({"text": text, "status": "open", "answer": None})
    return questions


def _parse_units(block: str, criteria_ids, notes: list):
    units = []
    seen = set()
    current = None
    for line in (block or "").splitlines():
        raw = line.rstrip()
        line = raw.strip()
        if not line:
            continue
        m = _UNIT_LINE.match(line)
        h = _UNIT_HEADING.match(line) if line.startswith("#") else None
        if m or h:
            mm = m or h
            uid = _slug(mm.group("id"), "unit%d" % (len(units) + 1))
            title = (mm.group("title") or uid).strip() or uid
            if uid in seen:
                notes.append("duplicate unit id skipped: %s" % uid)
                current = None
                continue
            seen.add(uid)
            current = {
                "id": uid,
                "title": title,
                "covers": [],
                "depends_on": [],
            }
            units.append(current)
            # inline covers/depends on same line
            dep = _DEPENDS.search(line)
            cov = _COVERS.search(line)
            if dep:
                current["depends_on"] = _split_csv(dep.group("deps"))
            if cov:
                current["covers"] = _split_csv(cov.group("covers"))
            continue
        if current is None:
            continue
        dep = _DEPENDS.search(line)
        cov = _COVERS.search(line)
        if dep:
            current["depends_on"] = list(dict.fromkeys(
                current["depends_on"] + _split_csv(dep.group("deps"))))
        if cov:
            current["covers"] = list(dict.fromkeys(
                current["covers"] + _split_csv(cov.group("covers"))))
        # bare AC mention under a unit
        for ac in re.findall(r"\bAC\d+[A-Za-z0-9_-]*\b", line, flags=re.I):
            ac_u = ac.upper()
            if ac_u in criteria_ids and ac_u not in current["covers"]:
                current["covers"].append(ac_u)
    return units


def _frame_from_sections(sections, title, notes):
    """Build a minimal frame markdown the ledger will accept as a pin."""
    scope = _pick_section(sections, "scope", "overview", "summary", "goal", "context")
    if not scope:
        scope = sections.get("_preamble") or sections.get("_body") or ""
    constraints = _pick_section(
        sections, "constraint", "hard constraint", "limits", "non-goal", "out of scope"
    )
    risks = _pick_section(sections, "risk", "assumptions", "open design")
    parts = ["# %s" % (title or "Discovery frame"), ""]
    parts.append("## Scope")
    parts.append(scope.strip() or "(extracted from session log — review before seal)")
    parts.append("")
    parts.append("## Hard constraints")
    parts.append(constraints.strip() or "(none extracted — fill before construction)")
    if risks.strip():
        parts.append("")
        parts.append("## Risks / open design")
        parts.append(risks.strip())
    parts.append("")
    parts.append("## Source")
    parts.append("Generated by `rungraph extract` from a session log. Heuristic coverage only.")
    if not scope.strip():
        notes.append("no scope section found; frame uses placeholder")
    if not constraints.strip():
        notes.append("no constraints section found; frame uses placeholder")
    return "\n".join(parts).strip() + "\n"


def _normalize_plan(doc, source_path, source_text, notes):
    """Accept already-structured JSON (plan or handoff-shaped) into plan form."""
    title = doc.get("title") or doc.get("id") or "from-session"
    frame = doc.get("frame_markdown") or doc.get("frame_text")
    if not frame and isinstance(doc.get("frame"), dict):
        frame = doc["frame"].get("text")
    if not frame:
        frame = (
            "# %s\n\n## Scope\n(imported structured plan)\n\n"
            "## Hard constraints\n(none)\n" % title
        )
        notes.append("structured input had no frame text; placeholder frame used")

    criteria = []
    for c in doc.get("criteria") or []:
        if isinstance(c, dict) and c.get("id") and c.get("text"):
            criteria.append({"id": str(c["id"]), "text": str(c["text"])})
        elif isinstance(c, str) and ":" in c:
            cid, text = c.split(":", 1)
            criteria.append({"id": cid.strip(), "text": text.strip()})

    questions = []
    for q in doc.get("questions") or []:
        if isinstance(q, dict) and q.get("text"):
            questions.append({
                "text": str(q["text"]),
                "status": q.get("status") or "open",
                "answer": q.get("answer"),
            })
        elif isinstance(q, str):
            questions.append({"text": q, "status": "open", "answer": None})

    units = []
    for u in doc.get("units") or []:
        if not isinstance(u, dict) or not u.get("id"):
            continue
        units.append({
            "id": _slug(str(u["id"]), "unit"),
            "title": str(u.get("title") or u["id"]),
            "covers": list(u.get("covers") or []),
            "depends_on": list(u.get("depends_on") or []),
        })

    return _finish_plan(
        title=title,
        frame_markdown=frame if frame.endswith("\n") else frame + "\n",
        criteria=criteria,
        questions=questions,
        units=units,
        source_path=source_path,
        source_text=source_text,
        notes=notes,
        mode="structured",
    )


def _finish_plan(title, frame_markdown, criteria, questions, units,
                 source_path, source_text, notes, mode):
    # Auto-map units → all criteria only when single unit and criteria exist
    if len(units) == 1 and criteria and not units[0].get("covers"):
        units[0]["covers"] = [c["id"] for c in criteria]
        notes.append("single unit auto-covers all criteria (mapping only)")

    # Orphan criteria → note, not silent invent
    covered = set()
    for u in units:
        covered.update(u.get("covers") or [])
    uncovered = [c["id"] for c in criteria if c["id"] not in covered]
    if uncovered and units:
        notes.append("uncovered criteria after extract: %s" % ", ".join(uncovered))

    if criteria and units and not uncovered:
        coverage = "full"
    elif criteria or units or questions:
        coverage = "partial"
    else:
        coverage = "undetermined"
        notes.append("no criteria/units/questions extracted — plan is a frame shell only")

    return {
        "kind": PLAN_KIND,
        "version": PLAN_VERSION,
        "title": title,
        "extracted_at": int(time.time()),
        "mode": mode,
        "coverage": coverage,
        "source": {
            "path": source_path,
            "sha256": _sha(source_text),
            "bytes": len(source_text.encode("utf-8")),
        },
        "frame_markdown": frame_markdown,
        "criteria": criteria,
        "questions": questions,
        "units": units,
        "notes": notes,
    }


def extract_plan(text, source_path="(stdin)"):
    """Build a lider.session.plan from text. Never raises on soft parse issues."""
    notes = []
    text = text if isinstance(text, str) else str(text)
    stripped = text.strip()
    if not stripped:
        notes.append("empty source")
        return _finish_plan(
            title="empty",
            frame_markdown="# empty\n\n## Scope\n\n## Hard constraints\n\n",
            criteria=[], questions=[], units=[],
            source_path=source_path, source_text=text, notes=notes, mode="empty",
        )

    # Structured JSON plan / handoff-shaped document
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            doc = json.loads(stripped)
        except json.JSONDecodeError as exc:
            notes.append("JSON parse failed (%s); falling back to text heuristics" % exc)
        else:
            if isinstance(doc, dict):
                if doc.get("kind") in (PLAN_KIND, "lider.inception.handoff") or any(
                        k in doc for k in ("criteria", "units", "frame_markdown", "frame")
                ):
                    return _normalize_plan(doc, source_path, text, notes)
            notes.append("JSON root was not a plan-shaped object; falling back to text")

    sections = _sections(text)
    # Title: first H1
    title = "session"
    for line in text.splitlines():
        if line.startswith("# ") and not line.startswith("##"):
            title = line[2:].strip() or title
            break

    crit_block = _pick_section(
        sections, "acceptance", "criteria", "success", "requirements", "ac "
    )
    # also scan whole doc for AC lines if section thin
    criteria = _parse_criteria(crit_block, notes)
    if not criteria:
        criteria = _parse_criteria(text, notes)
        if criteria:
            notes.append("criteria found outside a dedicated section")

    q_block = _pick_section(sections, "question", "open question", "unknown", "tbd")
    questions = _parse_questions(q_block)
    # whole-doc questions only if section empty
    if not questions:
        # only lines that look like questions, avoid every `?` in prose paragraphs
        for line in text.splitlines():
            s = line.strip()
            if s.startswith(("#", "```")):
                continue
            if s.endswith("?") and len(s) < 200:
                s = re.sub(r"^(?:[-*+]|\d+[.)])\s*", "", s)
                questions.append({"text": s, "status": "open", "answer": None})
        if questions:
            notes.append("questions inferred from lines ending in '?'")

    unit_block = _pick_section(
        sections, "unit", "workstream", "plan", "decomposition", "work breakdown", "wbs"
    )
    crit_ids = {c["id"] for c in criteria}
    units = _parse_units(unit_block, crit_ids, notes)
    if not units:
        units = _parse_units(text, crit_ids, notes)
        if units:
            notes.append("units found outside a dedicated section")

    frame = _frame_from_sections(sections, title, notes)
    return _finish_plan(
        title=title,
        frame_markdown=frame,
        criteria=criteria,
        questions=questions,
        units=units,
        source_path=source_path,
        source_text=text,
        notes=notes,
        mode="heuristic",
    )


def load_plan_file(path):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("kind") != PLAN_KIND:
        raise ValueError("not a lider.session.plan (kind=%r)" % doc.get("kind"))
    return doc


def write_plan(path, plan):
    import os
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path
