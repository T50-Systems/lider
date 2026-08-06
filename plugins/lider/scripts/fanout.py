#!/usr/bin/env python3
"""fanout.py - run several independent reviews concurrently, then reduce them.

One reviewer is one opinion. This runs N at once, each through a different LENS
(what to look for) and ideally a different ENGINE FAMILY (whose blind spots you
are escaping), then merges the results into a single round.

Two modes:

  review    N lenses over the same scope            -> round.json
  refute    N skeptics per severe finding           -> round.verified.json

The second is the important half. A fan-out of finders raises recall and LOWERS
precision: more eyes means more plausible-but-wrong findings, and handing those
to an adjudicator wastes exactly the judgment the flow is built around. So each
severe claim is put to independent skeptics instructed to REFUTE it, and a claim
a majority refutes is dropped before anyone spends time on it.

This file spawns and reduces; it does not supervise. Each individual run goes
through agent-exec.py, which owns the heartbeat, the watchdogs, the process-tree
teardown and the classified retry.

Usage:
  fanout.py review --scope "<what to review>" --lens <name>[:<engine>[:<model>]] ...
  fanout.py refute --round <round.json> [--votes 3] [--engines codex,claude]

Exit codes:
  0  reduced, full coverage
  2  reduced, but coverage UNDETERMINED (a lens or a ballot produced nothing)
  3  bad usage
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lider import log as log_module  # noqa: E402
from lider import metrics  # noqa: E402
from lider import findings as fx  # noqa: E402
from lider.adapters import DEFAULT_ENGINE  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_EXEC = os.path.join(HERE, "agent-exec.py")
SCHEMA_FINDINGS = os.path.join(HERE, "..", "schemas", "findings.schema.json")
SCHEMA_REFUTE = os.path.join(HERE, "..", "schemas", "refutation.schema.json")

OK, UNDETERMINED, USAGE = 0, 2, 3
SEVERE = ("BLOCKER", "MAJOR")

# A lens is a different QUESTION, not a rephrasing of the same one. Redundant
# reviewers find the same things; diverse ones find different things.
LENS_PROMPTS = {
    "correctness": "Focus ONLY on correctness: logic errors, wrong conditions, off-by-one, "
                   "mishandled null/empty/error cases, incorrect state transitions. For each, "
                   "give concrete inputs that produce the wrong output.",
    "security": "Focus ONLY on security: injection, authz/authn gaps, unsafe deserialization, "
                "secrets in code or logs, path traversal, SSRF, unsafe defaults, missing "
                "validation of untrusted input. State the attack, not the smell.",
    "regression": "Focus ONLY on regressions: behaviour this change removes or alters for "
                  "existing callers. Check every caller of every changed signature, removed "
                  "branch, changed default, and altered error path.",
    "concurrency": "Focus ONLY on concurrency and ordering: races, unguarded shared state, "
                   "non-atomic read-modify-write, lost updates, deadlock, assumptions about "
                   "ordering or single-threadedness, retry-induced duplication.",
    "performance": "Focus ONLY on performance: N+1 queries, work inside loops that could be "
                   "hoisted, unbounded growth, missing indexes, blocking calls on hot paths, "
                   "needless allocation or copying. Say why it matters at realistic scale.",
    "conventions": "Focus ONLY on this repository's own conventions: naming, typing, error "
                   "handling, logging, test structure, i18n, file layout. Compare against "
                   "neighbouring code, not general style opinions.",
    "tests": "Focus ONLY on test coverage: behaviour changed without a test, tests asserting "
             "the implementation rather than the behaviour, missing edge/error cases, tests "
             "that would still pass if the feature were deleted.",
}

EXIT_REASON = {
    0: None, 2: "bad usage / adapter refused", 3: "no usable output",
    124: "timeout", 125: "watchdog abort", 127: "engine not found", 130: "cancelled",
}


def slugify(name):
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def run_agent(label, engine, model, schema, out, log, prompt, timeout_s):
    """One supervised review. Returns the wrapper's exit code.

    Never raises: a reviewer that dies is data for the reducer, not an exception
    that takes the whole fan-out down with it.

    The child's stdout is TEED, not captured. Each supervised run narrates itself
    with a heartbeat, and redirecting that into a file nobody is watching would
    make a fan-out silent for its whole duration - the exact "a mute task is
    indistinguishable from a hang" failure this plugin forbids elsewhere. Lines
    are re-emitted under a [lens] tag so four concurrent runs stay attributable,
    and still land on disk for the post-mortem.
    """
    cmd = [sys.executable, AGENT_EXEC, "--engine", engine]
    if model:
        cmd += ["--model", model]
    cmd += [str(timeout_s), out, log, prompt]
    env = dict(os.environ, LIDER_SCHEMA=schema)
    log_module.set_prefix("[%s] " % label)
    log_module.diag_argv("launch", cmd)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=env, text=True, bufsize=1)
    except OSError as exc:
        log_module.warn("fanout: could not launch: %s" % exc)
        return 127
    with open(log + ".wrapper", "w", encoding="utf-8") as sink:
        for line in proc.stdout:
            sink.write(line)
            line = line.rstrip("\n")
            if line:
                log_module.emit(line)
    return proc.wait()


def read_usage(log_path):
    """The usage block the supervisor left on this run's status file."""
    try:
        with open(log_path + ".status.json", encoding="utf-8") as fh:
            return json.load(fh).get("usage") or {}
    except (OSError, ValueError):
        return {}


def record_round(args, jobs, round_path, mode):
    """Per-lens and per-round rows: what the fan-out cost and what it bought.

    Cost is summed with `Agg`, which counts unmeasured runs separately instead of
    adding zeros - so a round whose engines do not report cost reads as
    "unknown", never as "free".
    """
    root = os.environ.get("LIDER_METRICS_DIR") or os.getcwd()
    try:
        with open(round_path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        doc = {}
    contribution = {r.get("lens"): r for r in doc.get("reviewers", [])}

    cost = metrics.Agg()
    for job in jobs:
        usage = read_usage(job["log"])
        cost.add(usage.get("cost_usd"))
        stats = contribution.get(job.get("lens"), {})
        metrics.record(root, "lens", mode=mode,
                       lens=job.get("lens") or job.get("label"), engine=job["engine"],
                       model=job.get("model"), exit=job.get("exit"),
                       cost_usd=usage.get("cost_usd"),
                       output_tokens=usage.get("output_tokens"),
                       model_billed=usage.get("model_billed"),
                       findings=stats.get("count"),
                       unique=stats.get("unique"), shared=stats.get("shared"),
                       measured=bool(usage))

    metrics.record(root, "round", mode=mode,
                   lenses=len(jobs), coverage=doc.get("coverage"),
                   verdict=doc.get("verdict"),
                   findings=len(doc.get("findings", [])),
                   missing=len(doc.get("missing", [])),
                   refutation=doc.get("refutation_summary"),
                   cost_usd=cost.total if cost.n else None,
                   cost_unmeasured=cost.unknown)
    log_module.emit("round cost: %s" % cost.fmt(" USD"))


def parse_lens(spec, default_engine):
    """'security:claude:opus' -> ('security', 'claude', 'opus')."""
    parts = spec.split(":")
    name = parts[0]
    engine = parts[1] if len(parts) > 1 and parts[1] else default_engine
    model = parts[2] if len(parts) > 2 and parts[2] else None
    return name, engine, model


def prune_lenses(lenses, root, min_runs):
    """Drop lenses the record says contribute nothing. Never silently.

    This is the one place the measurement loop closes: `metrics.jsonl` knows how
    many findings each lens contributed that NO other lens found, and a lens with
    a sustained zero is paying for a full engine call to repeat a colleague.

    Two refusals matter more than the pruning itself:

      * a lens with fewer than `min_runs` recorded runs is KEPT. Absence of
        evidence is not evidence of uselessness, and pruning on two data points
        would encode noise as policy.
      * every drop is announced with its evidence. A quietly narrowed fan-out
        reports the same "coverage: complete" as a full one, which is exactly the
        lie this plugin exists to prevent.
    """
    events = metrics.read(root)
    if not events:
        return lenses, []
    stats = {}
    for event in events:
        if event.get("kind") != "lens":
            continue
        entry = stats.setdefault(event.get("lens"), {"runs": 0, "unique": 0})
        entry["runs"] += 1
        entry["unique"] += event.get("unique") or 0

    kept, dropped = [], []
    for name, engine, model in lenses:
        entry = stats.get(name)
        if entry and entry["runs"] >= min_runs and entry["unique"] == 0:
            dropped.append((name, entry))
        else:
            kept.append((name, engine, model))
    if not kept and dropped:
        # Never prune the fan-out down to nothing: a review with no lenses is not
        # a cheap review, it is no review.
        log_module.warn("fanout: pruning would remove every lens; keeping them all.")
        return lenses, []
    return kept, dropped


def build_prompt(name, engine, scope, seen):
    """The lens prompt, plus what earlier rounds already found.

    Without this list a second round is just the first round again: the same
    lens over the same code produces the same findings, the dedupe throws them
    away, and the loop reports "nothing new" while never having looked anywhere
    new. Telling each finder what is already known is what makes another round
    worth its cost.
    """
    lens_text = LENS_PROMPTS.get(
        name, "Review specifically through the lens of: %s. Report only what that lens "
              "is responsible for." % name)
    prompt = (
        "%s\n\nSCOPE OF THIS REVIEW:\n%s\n\n"
        "Return findings per the required schema, with engine set to \"%s\". Report ONLY "
        "what this lens covers - another reviewer is covering the rest, and duplicating "
        "their work costs the round its independence. If this lens finds nothing, return "
        "an empty findings array and verdict \"approve\"." % (lens_text, scope, engine))
    if seen:
        listed = "\n".join("  - %s (%s)" % (d["summary"][:110], d.get("location"))
                            for d in seen[:40])
        prompt += (
            "\n\nALREADY REPORTED by earlier rounds - do NOT report these again, and do "
            "not report trivial restatements of them. Look for what they missed:\n%s"
            % listed)
        if len(seen) > 40:
            prompt += "\n  ... and %d more." % (len(seen) - 40)
    return prompt


def launch_round(args, lenses, out_dir, seen):
    """One wave of lenses -> that wave's round.json. Returns (jobs, round_doc)."""
    os.makedirs(out_dir, exist_ok=True)
    jobs = []
    for name, engine, model in lenses:
        slug = slugify(name)
        jobs.append({"lens": name, "engine": engine, "model": model,
                     "out": os.path.join(out_dir, slug + ".json"),
                     "log": os.path.join(out_dir, slug + ".log"),
                     "prompt": build_prompt(name, engine, args.scope, seen)})

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {}
        for job in jobs:
            log_module.emit("  -> %s (%s%s)" % (job["lens"], job["engine"],
                                                "/" + job["model"] if job["model"] else ""))
            futures[pool.submit(run_agent, job["lens"], job["engine"], job["model"],
                                args.schema, job["out"], job["log"], job["prompt"],
                                args.timeout)] = job
        for future, job in futures.items():
            job["exit"] = future.result()

    manifest = {"lenses": [{
        "lens": j["lens"], "engine": j["engine"], "model": j["model"],
        "out": j["out"], "exit": j["exit"],
        "reason": EXIT_REASON.get(j["exit"], "exit %s" % j["exit"]),
    } for j in jobs]}
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    round_path = os.path.join(out_dir, "round.json")
    subprocess.call([sys.executable, os.path.join(HERE, "reduce-findings.py"),
                     "--manifest", manifest_path, "--out", round_path])
    try:
        with open(round_path, encoding="utf-8") as fh:
            return jobs, json.load(fh)
    except (OSError, ValueError):
        return jobs, {"findings": [], "reviewers": [], "missing": [],
                      "coverage": "undetermined"}


def cmd_review(args):
    """Fan out lenses over the scope, optionally until discovery goes dry.

    A single wave of N lenses is an arbitrary amount of looking: nothing about it
    says the search is finished. With --until-dry K the waves repeat until K
    CONSECUTIVE rounds turn up no defect the earlier rounds had not already
    reported.

    The dedupe is against everything SEEN, never against what survived. Dedupe
    against confirmed findings instead and every claim the refutation pass
    dropped comes back next round, and the loop never terminates.
    """
    lenses = [parse_lens(spec, args.default_engine) for spec in args.lens]
    if args.prune_lenses:
        root = os.environ.get("LIDER_METRICS_DIR") or os.getcwd()
        lenses, dropped = prune_lenses(lenses, root, args.prune_min_runs)
        for name, entry in dropped:
            log_module.emit("fanout: dropping lens '%s' - %d recorded run(s), 0 findings "
                            "no other lens also found. Drop --prune-lenses to keep it."
                            % (name, entry["runs"]))
    log_module.emit("fanout: %d lens(es), concurrency %d, timeout %ds%s"
                    % (len(lenses), args.concurrency, args.timeout,
                       ", until %d dry round(s) (max %d)" % (args.until_dry, args.max_rounds)
                       if args.until_dry else ""))

    seen = []            # every defect any round has reported, deduped
    reviewers, missing = [], []
    dry = wave = 0
    coverage = "complete"

    while True:
        wave += 1
        wave_dir = os.path.join(args.out, "round-%d" % wave) if args.until_dry else args.out
        if args.until_dry:
            log_module.emit("== round %d (dry streak %d/%d) =="
                            % (wave, dry, args.until_dry))
        jobs, doc = launch_round(args, lenses, wave_dir, seen)

        fresh = []
        for finding in doc.get("findings", []):
            if fx.match(finding, seen) is None:
                entry = dict(finding, _key=fx.key(finding), first_round=wave)
                seen.append(entry)
                fresh.append(entry)
        reviewers.extend(dict(r, round=wave) for r in doc.get("reviewers", []))
        for gap in doc.get("missing", []):
            missing.append(dict(gap, round=wave))
            coverage = "undetermined"

        record_round(args, jobs, os.path.join(wave_dir, "round.json"), "review")
        log_module.emit("round %d: %d finding(s), %d new"
                        % (wave, len(doc.get("findings", [])), len(fresh)))

        if not args.until_dry:
            break
        dry = dry + 1 if not fresh else 0
        if dry >= args.until_dry:
            log_module.emit("discovery dry: %d consecutive round(s) found nothing new."
                            % dry)
            break
        if wave >= args.max_rounds:
            # Say what was cut. A bounded search reported without its bound reads
            # as an exhaustive one.
            log_module.warn("fanout: stopped at the %d-round cap with the dry streak at "
                            "%d/%d - discovery had NOT gone quiet. Treat this round as "
                            "incomplete." % (args.max_rounds, dry, args.until_dry))
            coverage = "undetermined"
            missing.append({"lens": "(further rounds)", "engine": None,
                            "reason": "max-rounds cap reached while still finding new defects"})
            break

    merged = [{k: v for k, v in f.items() if not k.startswith("_")} for f in seen]
    merged.sort(key=lambda f: (fx.SEVERITY_RANK.get(f.get("severity"), 9),
                               -(f.get("corroboration", {}).get("engines") or 0)))
    verdict = "request_changes" if any(f.get("severity") in fx.SEVERE for f in merged) \
        else ("approve_with_nits" if merged else "approve")
    out_doc = {
        "engine": "fanout(%s)" % ",".join(sorted({e for _, e, _ in lenses})),
        "verdict": verdict, "coverage": coverage,
        "rounds": wave, "dry_streak": dry,
        "reviewers": reviewers, "missing": missing, "findings": merged,
    }
    round_path = os.path.join(args.out, "round.json")
    with open(round_path, "w", encoding="utf-8") as fh:
        json.dump(out_doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    log_module.emit("round: %s  (%d unique finding(s) over %d round(s), coverage %s)"
                    % (round_path, len(merged), wave, coverage))
    if coverage == "undetermined":
        log_module.warn("COVERAGE UNDETERMINED - see `missing` in the round. This is not a "
                        "clean review; say which lenses or rounds did not happen.")
        return UNDETERMINED
    return OK


def cmd_refute(args):
    with open(args.round, encoding="utf-8") as fh:
        doc = json.load(fh)
    claims = [f for f in doc.get("findings", []) if f.get("severity") in SEVERE]
    if not claims:
        print("fanout: no BLOCKER/MAJOR claims to refute")
        return OK

    engines = [e.strip() for e in args.engines.split(",") if e.strip()] or [args.default_engine]
    print("fanout: refuting %d claim(s) with %d vote(s) each across %s"
          % (len(claims), args.votes, "/".join(engines)))

    jobs = []
    for i, claim in enumerate(claims):
        text = "%s  [%s]  at %s" % (claim.get("summary", ""), claim.get("severity"),
                                    claim.get("location"))
        for v in range(args.votes):
            # Rotate engines so the skeptics are not all the model least likely
            # to refute its own finding.
            engine = engines[v % len(engines)]
            prompt = (
                "A reviewer made this claim about the code in this repository:\n\n  %s\n\n"
                "Your job is to REFUTE it. Read the actual code and establish whether the claim "
                "is true as stated. Be adversarial: reviewers routinely report defects that the "
                "surrounding code already handles, that cannot be reached, or that misread the "
                "control flow.\n\n"
                "Answer per the required schema. Set refuted=true if the claim is wrong, already "
                "handled, or unreachable. Set refuted=false ONLY if you confirmed by reading the "
                "code that the defect is real. If you could not establish either way, set "
                "refuted=false and confidence=\"low\" - never guess a refutation you did not "
                "verify." % text
            )
            base = os.path.join(args.out, "refute-%d-%d" % (i, v))
            jobs.append({"label": "claim%d/vote%d" % (i + 1, v + 1),
                         "engine": engine, "out": base + ".json",
                         "log": base + ".log", "prompt": prompt})

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        codes = list(pool.map(lambda j: run_agent(j["label"], j["engine"], None,
                                                  args.schema_refute, j["out"], j["log"],
                                                  j["prompt"], args.timeout), jobs))
    for job, code in zip(jobs, codes):
        job["exit"] = code
        job.setdefault("lens", job["label"])
        job.setdefault("model", None)

    out_path = args.out_file or (os.path.splitext(args.round)[0] + ".verified.json")
    code = subprocess.call([sys.executable, os.path.join(HERE, "verify-findings.py"),
                            "--round", args.round, "--dir", args.out,
                            "--votes", str(args.votes), "--out", out_path])
    record_round(args, jobs, out_path, "refute")
    log_module.emit("verified round: %s" % out_path)
    return code


def main():
    ap = argparse.ArgumentParser(prog="fanout.py", description=__doc__.split("\n")[0])
    ap.add_argument("--out", help="working dir for logs/results (default: a temp dir)")
    ap.add_argument("--timeout", type=int, default=240, help="per-run timeout in seconds")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--default-engine", default=os.environ.get("LIDER_ENGINE", DEFAULT_ENGINE))
    sub = ap.add_subparsers(dest="mode", required=True)

    q = sub.add_parser("review", help="N lenses over one scope")
    q.add_argument("--scope", required=True)
    q.add_argument("--lens", action="append", required=True,
                   help="name[:engine[:model]] - repeatable")
    q.add_argument("--until-dry", type=int, default=0, metavar="K",
                   help="keep running waves until K consecutive rounds find nothing new "
                        "(0 = a single wave, the default)")
    q.add_argument("--max-rounds", type=int, default=5,
                   help="hard cap on waves; reaching it marks the round incomplete")
    q.add_argument("--prune-lenses", action="store_true",
                   help="consult .lider/metrics.jsonl and skip lenses with a sustained "
                        "zero unique-findings record (announced, never silent)")
    q.add_argument("--prune-min-runs", type=int, default=3, metavar="N",
                   help="minimum recorded runs before a lens may be pruned (default 3): "
                        "too little data is not evidence of uselessness")
    q.set_defaults(fn=cmd_review)

    q = sub.add_parser("refute", help="N skeptics per severe finding")
    q.add_argument("--round", required=True)
    q.add_argument("--votes", type=int, default=3)
    q.add_argument("--engines", default="")
    q.add_argument("--out-file")
    q.set_defaults(fn=cmd_refute)

    args = ap.parse_args()
    args.concurrency = max(1, args.concurrency)
    args.out = args.out or tempfile.mkdtemp(prefix="lider-fanout-")
    os.makedirs(args.out, exist_ok=True)
    args.schema = os.path.abspath(SCHEMA_FINDINGS)
    args.schema_refute = os.path.abspath(SCHEMA_REFUTE)
    if not os.path.exists(AGENT_EXEC):
        print("fanout: agent-exec.sh not found at %s" % AGENT_EXEC, file=sys.stderr)
        return USAGE
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
