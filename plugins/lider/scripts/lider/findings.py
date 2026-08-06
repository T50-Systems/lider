"""lider.findings - when are two reported defects the same defect?

One question, three callers, and it must answer identically for all of them:

  * the REDUCER folds a fan-out's parallel reviewers into one round;
  * the LEDGER tracks a defect ACROSS adjudication rounds, so "the implementer
    keeps failing to fix this one" is a fact and not a hunch;
  * LOOP-UNTIL-DRY decides whether a new round of finders actually found
    anything new, or just restated what earlier rounds already said.

The third is why this had to leave the reducer. A discovery loop that dedupes
against the wrong set never terminates: dedupe against CONFIRMED findings and
every claim the refutation pass dropped comes straight back next round. Dedupe
against everything SEEN, always.
"""
import difflib
import os
import re

SEVERITY_RANK = {"BLOCKER": 0, "MAJOR": 1, "MINOR": 2, "NIT": 3}
SEVERE = ("BLOCKER", "MAJOR")
SIMILARITY = 0.62      # paraphrases merge, distinct defects do not
LINE_DRIFT = 25        # reviewers cite the symptom or the cause

_WORD = re.compile(r"[a-z0-9_]+")
_LINE = re.compile(r":(\d+)")


def norm_file(location):
    """'src/auth.ts:42' -> ('auth.ts', 42). Separators unified, path basenamed."""
    if not location:
        return ("", None)
    loc = str(location).replace("\\", "/").strip()
    match = _LINE.search(loc)
    line = int(match.group(1)) if match else None
    path = loc[:match.start()] if match else loc
    return (os.path.basename(path.rstrip(":")), line)


def norm_summary(text):
    return " ".join(_WORD.findall((text or "").lower()))


def key(item):
    """The comparable shape of a finding: (file, line), normalised text, tokens."""
    summary = item.get("summary") or ""
    return {
        "file": norm_file(item.get("location")),
        "norm": norm_summary(summary),
        "tokens": set(_WORD.findall(summary.lower())),
    }


def similarity(a, b):
    """Sequence ratio OR token overlap - each catches what the other misses.

    Sequence ratio catches an edited phrasing of one sentence; token overlap
    catches the same words reordered, which a sequence ratio scores poorly.
    """
    seq = difflib.SequenceMatcher(None, a["norm"], b["norm"]).ratio()
    ta, tb = a["tokens"], b["tokens"]
    jac = len(ta & tb) / float(len(ta | tb)) if (ta or tb) else 0.0
    return max(seq, jac)


def same_defect(a, b):
    """Do these two reports describe the same defect?

    Same file is required: a similar sentence about a different file is a
    different defect, and merging those hides one of them.

    Then either signal suffices:
      * the SAME line - two reviewers pointing at one line are almost always
        describing one defect, however differently they word it; or
      * lexically close summaries within a short distance.

    KNOWN LIMIT that shapes the rule: lexical measures do not recognise
    paraphrase. "add() returns a - b" and "the add function subtracts its
    arguments" score near zero against each other. Without a line match this
    therefore errs toward SEPARATE. That is the right direction: a duplicate
    costs the adjudicator a moment, a wrongly merged finding is a defect nobody
    ever sees again.
    """
    fa, la = a["file"]
    fb, lb = b["file"]
    if fa != fb:
        return False
    if la is not None and lb is not None and la == lb:
        return True
    if la is not None and lb is not None and abs(la - lb) > LINE_DRIFT:
        return False
    return similarity(a, b) >= SIMILARITY


def match(item, known):
    """First entry of `known` describing the same defect as `item`, else None.

    `known` is a list of dicts each carrying a `_key` produced by `key()`.
    """
    probe = key(item)
    for candidate in known:
        candidate_key = candidate.get("_key") or key(candidate)
        if same_defect(probe, candidate_key):
            return candidate
    return None


def worst(severities):
    return min(severities, key=lambda s: SEVERITY_RANK.get(s, 9))
