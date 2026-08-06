"""lider.metrics - the append-only record that makes Lider's own choices evidence-based.

Lider makes the same handful of decisions over and over, and today every one of
them is doctrine written in a SKILL.md. Each is answerable from data the flow
ALREADY produces and then throws away:

  DECISION                          MEASURE THAT SETTLES IT
  which engine implements?          accept-without-return rate, rounds to
                                    converge, cost, wall time - per engine
  which engine reviews?             finding PRECISION: share of its findings
                                    that survived refutation and were accepted
  which lenses to run?              UNIQUE findings contributed (not duplicates
                                    of another lens) per dollar. A lens that only
                                    ever echoes `correctness` should be dropped.
  how many refutation votes?        how often the deciding vote flipped an
                                    outcome. If the 3rd never flips, 2 is enough.
  are the timeouts right?           duration distribution, idle gaps, and whether
                                    watchdog aborts were real hangs
  is the loop healthy?              rounds per run, convergence slope, escalations

Records are JSON Lines under <repo>/.lider/metrics.jsonl - append-only, one event
per line, safe to concatenate across machines and trivial to inspect with grep.

THE RULE THIS SHARES WITH THE REST OF THE PLUGIN: a quantity we could not measure
is recorded as null, NEVER as zero. A cost of 0 and a cost we could not read are
opposite facts, and averaging the second as the first silently makes an expensive
engine look free. Every aggregate here reports how many inputs were unknown.
"""
import json
import os
import time

SCHEMA_VERSION = 1


def store_path(root):
    return os.path.join(root, ".lider", "metrics.jsonl")


def record(root, kind, **fields):
    """Append one event. Never raises: metrics must not break the flow."""
    try:
        path = store_path(root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        event = {"v": SCHEMA_VERSION, "kind": kind, "at": int(time.time())}
        event.update(fields)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return True
    except (OSError, TypeError, ValueError):
        return False


def read(root):
    path = store_path(root)
    if not os.path.exists(path):
        return []
    events = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue          # a torn line is one lost event, not a broken store
    return events


class Agg(object):
    """A sum that keeps track of what it could not add.

    `total` is the sum of what was known; `unknown` counts the inputs that were
    not. Reporting a mean over `n` while silently dropping `unknown` values is
    how a missing cost becomes a free engine.
    """

    def __init__(self):
        self.total = 0.0
        self.n = 0
        self.unknown = 0

    def add(self, value):
        if value is None:
            self.unknown += 1
        else:
            self.total += value
            self.n += 1

    @property
    def mean(self):
        return self.total / self.n if self.n else None

    def fmt(self, unit="", places=4):
        if not self.n:
            return "unknown (%d/%d unmeasured)" % (self.unknown, self.unknown)
        text = "%.*f%s" % (places, self.total, unit)
        if self.unknown:
            text += "  [+%d unmeasured]" % self.unknown
        return text


def by(events, kind, key):
    """Group events of one kind by a field, preserving first-seen order."""
    groups = {}
    for event in events:
        if event.get("kind") != kind:
            continue
        groups.setdefault(event.get(key), []).append(event)
    return groups
