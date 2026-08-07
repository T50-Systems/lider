#!/usr/bin/env python3
"""rungraph.py - the run ledger: Lider's flow as an enforced state machine.

`pipeline/SKILL.md` describes a graph in prose. Prose is honoured, not enforced:
nothing counts adjudication rounds, nothing checks that the reviewer differs from
the implementer, and nothing survives the session that held the spec. This turns
that graph into data and its rules into guards.

Three things it makes real:

  1. THE GRAPH IS DATA. Graph tables list nodes and legal edges. A transition
     that is not an edge is refused, with the legal ones named.

  2. THE ADJUDICATION LOOP IS BOUNDED AND MUST CONVERGE. Returning to the
     implementer opens a round. Rounds are capped, and each one must strictly
     reduce open BLOCKER+MAJOR findings by identity, not by count.

  3. `could not determine` IS A TYPE, NOT A PARAGRAPH. Every check is
     ok | not-ok | undetermined; `undetermined` blocks forward edges exactly like
     a failure. Exit code 2 means "I could not establish this".

State lives in <repo>/.lider/runs/<run-id>/run.json, written atomically.

When to graph vs loop (do not invent a second framework):
  - Retry/converge on one act -> loop *inside* a node (adjudication rounds, fanout).
  - Multi-role edges, barriers, resume across sessions -> this graph.
  - No checkable predicate -> prose, not a new node.
`show` lists artifact presence for the current kind so refusals are predictable.

Implementation is split under `lider/rungraph/` (constants, storage, model, guards,
handoff, ops, schedule, show, commands, cli). This file is the stable CLI entry.

Exit codes:  0 ok  |  1 refused (a rule says no)  |  2 undetermined  |  3 usage
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lider.rungraph.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
