"""argparse CLI for rungraph."""
import argparse
import sys

from .commands_extract import cmd_apply_plan, cmd_extract
from .commands_flow import cmd_enter, cmd_gate
from .commands_review import cmd_adjudicate, cmd_findings, cmd_import
from .commands_setup import (
    cmd_assign,
    cmd_check,
    cmd_criterion,
    cmd_init,
    cmd_question,
    cmd_spec,
    cmd_target,
    cmd_unit,
)
from .constants import (
    DECISIONS,
    KIND_CONSTRUCTION,
    KIND_INCEPTION,
    KINDS,
    USAGE,
    VERDICTS,
)
from .schedule import cmd_next, cmd_schedule
from .show import cmd_show

DOC_HEAD = "rungraph.py - the run ledger: Lider's flow as an enforced state machine."

def build_parser():
    # --dir/--run are accepted on BOTH sides of the subcommand. argparse defaults
    # to global-only, which silently rejects the placement most people reach for
    # first (`... enter spec --run demo`).
    # SUPPRESS matters: without it the subparser's unset default (None) would
    # overwrite a value already given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default=argparse.SUPPRESS,
                        help="repo root holding .lider/ (default: cwd)")
    common.add_argument("--run", default=argparse.SUPPRESS,
                        help="run id (default: the most recently updated run)")

    p = argparse.ArgumentParser(prog="rungraph.py", description=DOC_HEAD,
                                parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=lambda **kw:
                           argparse.ArgumentParser(parents=[common], **kw))

    q = sub.add_parser("init", help="start a run")
    q.add_argument("--title", required=True)
    q.add_argument("--kind", choices=list(KINDS),
                   default=KIND_CONSTRUCTION,
                   help="construction (default), inception (discovery), or operations (shared state)")
    q.add_argument("--strict", action="store_true",
                   help="stricter gates: inception challenge+handoff import; "
                        "operations preflight before act and effect before closed. "
                        "Also LIDER_STRICT=1")
    q.add_argument("--max-rounds", type=int, default=3)
    q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_init)

    q = sub.add_parser("spec", help="pin the closed spec (or inception frame)")
    q.add_argument("--file", required=True)
    q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_spec)

    q = sub.add_parser("import", help="construction: load a sealed inception handoff "
                                      "from .lider/handoffs/")
    q.add_argument("--handoff", required=True, help="path to sealed handoff JSON")
    q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_import)

    q = sub.add_parser("target", help="operations: pin env/ref under change")
    q.add_argument("--env", required=True, help="environment name (prod, staging, ...)")
    q.add_argument("--ref", required=True, help="expected git SHA, tag, or release id (desired/current)")
    q.add_argument("--previous-ref", dest="previous_ref",
                   help="last known good ref (required for STRICT rollback)")
    q.add_argument("--url", help="base URL or health endpoint of the environment")
    q.add_argument("--surfaces", help="comma-separated surfaces to verify (api,web,...)")
    q.add_argument("--notes")
    q.add_argument("--construction-run", dest="construction_run",
                   help="optional construction run id this ops action ships")
    q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_target)

    q = sub.add_parser("assign", help="record who plays a role")
    q.add_argument("--role", required=True,
                   choices=["architect", "implementer", "reviewer", "challenger"])
    q.add_argument("--engine", required=True)
    q.add_argument("--model")
    q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_assign)

    q = sub.add_parser("check", help="record a check with a three-valued verdict")
    q.add_argument("--name", required=True)
    q.add_argument("--verdict", required=True, choices=list(VERDICTS))
    q.add_argument("--evidence")
    q.set_defaults(fn=cmd_check)

    q = sub.add_parser("criterion", help="declare acceptance criteria, defer one, or list")
    q.add_argument("action", choices=["add", "defer", "list"])
    q.add_argument("--id")
    q.add_argument("--text")
    q.add_argument("--reason", help="required when deferring: a descope must be visible")
    q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_criterion)

    q = sub.add_parser("question", help="record an open question, resolve one, or list")
    q.add_argument("action", choices=["add", "resolve", "list"])
    q.add_argument("--id")
    q.add_argument("--text")
    q.add_argument("--status", choices=["answered", "assumed"])
    q.add_argument("--answer")
    q.add_argument("--unit")
    q.set_defaults(fn=cmd_question)

    q = sub.add_parser("next",
                       help="what could run right now (never writes the ledger; records "
                            "one eligibility row to metrics.jsonl)")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_next)

    q = sub.add_parser("schedule",
                       help="plan parallel unit waves from deps (does not run engines; "
                            "records one schedule row to metrics.jsonl)")
    q.add_argument("--json", action="store_true", help="same as --format json")
    q.add_argument("--format", choices=["text", "json", "commands"], default="text",
                   help="text (default), json, or shell commands with worktrees")
    q.add_argument("--max-width", type=int, default=0,
                   help="cap units per wave (0 = unlimited). Use when hosts/worktrees are limited")
    q.add_argument("--worktree-root",
                   help="with --format commands: parent dir for unit worktrees")
    q.set_defaults(fn=cmd_schedule)

    q = sub.add_parser("unit", help="declare a unit of work, or list them")
    q.add_argument("action", choices=["add", "list"])
    q.add_argument("--id")
    q.add_argument("--title", default="")
    q.add_argument("--depends-on", dest="depends_on",
                   help="comma-separated ids that must finish first")
    q.add_argument("--covers", help="comma-separated acceptance criteria this unit maps to")
    q.add_argument("--max-rounds", type=int)
    q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_unit)

    q = sub.add_parser("findings", help="ingest a review round's findings JSON")
    q.add_argument("--file", required=True)
    q.add_argument("--unit", help="scope this round to a unit of work")
    q.set_defaults(fn=cmd_findings)

    q = sub.add_parser("adjudicate", help="decide one finding")
    q.add_argument("--finding", required=True)
    q.add_argument("--decision", required=True, choices=list(DECISIONS))
    q.add_argument("--rationale")
    q.add_argument("--unit", help="scope this decision to a unit of work")
    q.set_defaults(fn=cmd_adjudicate)

    q = sub.add_parser("enter", help="move to a node (guarded)")
    q.add_argument("node")
    q.add_argument("--unit", help="move a UNIT through its own subgraph instead of the run")
    q.add_argument("--force", action="store_true", help="override a guard, recorded in the ledger")
    q.set_defaults(fn=cmd_enter)

    q = sub.add_parser("gate", help="ask whether a move would be allowed, without moving")
    q.add_argument("node")
    q.add_argument("--unit")
    q.set_defaults(fn=cmd_gate)

    q = sub.add_parser("show", help="what a resumed orchestrator reads first")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_show)

    q = sub.add_parser(
        "extract",
        help="session log / transcript → lider.session.plan (trace-to-graph; no LLM)")
    q.add_argument("--file", required=True,
                   help="session log (.md/.txt) or structured plan/handoff JSON")
    q.add_argument("--out", help="where to write the plan JSON "
                                 "(default: .lider/plans/<title>.plan.json)")
    q.add_argument("--json", action="store_true", help="also dump plan to stdout")
    q.add_argument("--apply", action="store_true",
                   help="after extract, seed a run (use --run / --init; see apply-plan)")
    q.add_argument("--init", action="store_true",
                   help="with --apply: create the run if missing (default when --run omitted)")
    q.add_argument("--kind", choices=[KIND_INCEPTION, KIND_CONSTRUCTION],
                   default=KIND_INCEPTION, help="with --apply/--init (default: inception)")
    q.add_argument("--title", help="with --apply: run title")
    q.add_argument("--strict", action="store_true")
    q.add_argument("--max-rounds", type=int, default=3)
    q.add_argument("--frame-out", dest="frame_out",
                   help="with --apply: path for the written frame markdown")
    q.add_argument("--enter-spec", action="store_true",
                   help="with --apply: move init→spec after seeding")
    q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_extract)

    q = sub.add_parser(
        "apply-plan",
        help="seed a run from a lider.session.plan (does not seal handoff)")
    q.add_argument("--plan", required=True, help="path to .plan.json from extract")
    q.add_argument("--init", action="store_true",
                   help="create run if needed (inception by default; use --run for id)")
    q.add_argument("--kind", choices=[KIND_INCEPTION, KIND_CONSTRUCTION],
                   default=KIND_INCEPTION)
    q.add_argument("--title", help="title when creating a run")
    q.add_argument("--strict", action="store_true")
    q.add_argument("--max-rounds", type=int, default=3)
    q.add_argument("--frame-out", dest="frame_out")
    q.add_argument("--enter-spec", action="store_true")
    q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_apply_plan)
    return p


def main():
    args = build_parser().parse_args()
    args.dir = getattr(args, "dir", None) or "."
    args.run = getattr(args, "run", None)
    try:
        return args.fn(args)
    except FileNotFoundError as exc:
        print("rungraph: %s" % exc, file=sys.stderr)
        return USAGE
    except (ValueError, KeyError) as exc:
        print("rungraph: %s" % exc, file=sys.stderr)
        return USAGE
