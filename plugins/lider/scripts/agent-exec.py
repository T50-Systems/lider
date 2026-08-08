#!/usr/bin/env python3
"""agent-exec.py - engine-agnostic REVIEW wrapper.

Runs an engine read-only and comes back with schema-conformant findings, under
full supervision (heartbeat, watchdogs, process-tree teardown, classified retry).

Usage: agent-exec.py [--engine <id>] [--model <slug>] <timeout_s> <out_json> <log_file> <prompt>

Exit codes:
  0    finished ok AND <out_json> exists, parses, and conforms to the schema
  124  hard timeout (process tree killed)
  125  watchdog abort (stalled, or produced nothing at startup)
  3    finished ok but <out_json> is missing, unparseable, or non-conformant
  127  engine binary not found
  2    bad usage / missing schema / the adapter refused the mode
  N    any other code: the engine's exit code is propagated

A live status file is written to <log_file>.status.json throughout the run.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lider import adapters                      # noqa: E402
from lider.adapters import DEFAULT_ENGINE       # noqa: E402
from lider.runtime import Supervisor, tunable   # noqa: E402
from lider.validate import validate_file        # noqa: E402
from lider import log                           # noqa: E402
from lider import metrics                       # noqa: E402
from lider import state                         # noqa: E402

DEFAULT_SCHEMA = os.path.join(HERE, "..", "schemas", "findings.schema.json")


def parse_args(argv):
    engine = os.environ.get("LIDER_ENGINE", DEFAULT_ENGINE)
    model = None
    while argv and argv[0].startswith("--"):
        flag = argv.pop(0)
        if flag == "--":
            break
        name, _, inline = flag.partition("=")
        value = inline if inline else (argv.pop(0) if argv else "")
        if not value:
            print("agent-exec: %s requires a value" % name, file=sys.stderr)
            sys.exit(2)
        if name == "--engine":
            engine = value
        elif name == "--model":
            model = value
        else:
            print("agent-exec: unknown flag %s" % name, file=sys.stderr)
            sys.exit(2)
    if len(argv) < 4:
        print(__doc__.strip().splitlines()[4], file=sys.stderr)
        sys.exit(2)
    timeout_s, out_json, log_file = argv[0], argv[1], argv[2]
    prompt = " ".join(argv[3:])
    try:
        timeout_s = int(timeout_s)
    except ValueError:
        print("agent-exec: invalid timeout %r" % timeout_s, file=sys.stderr)
        sys.exit(2)
    
    # Load and validate state
    current_state = state.read_state()
    expected_gen = current_state.get("generation_id", 1) if current_state else 1
    
    # Validate generation before starting
    if current_state is not None:
        valid, msg = state.validate_generation(current_state, expected_gen)
        if not valid:
            print("agent-exec: %s" % msg, file=sys.stderr)
            sys.exit(2)
    
    return engine, model, timeout_s, out_json, log_file, prompt, current_state


def tail_filtered(log_file, lines=5):
    noise = ("caveman", "Skill descriptions were shortened")
    try:
        with open(log_file, encoding="utf-8", errors="replace") as fh:
            kept = [ln for ln in fh
                    if not any(n in ln for n in noise) and "hook: " not in ln]
        return "".join(kept[-lines:])
    except OSError:
        return ""


def main():
    engine, model, timeout_s, out_json, log_file, prompt, current_state = parse_args(sys.argv[1:])
    schema = os.path.abspath(os.environ.get("LIDER_SCHEMA") or DEFAULT_SCHEMA)
    status_file = log_file + ".status.json"

    if not os.path.isfile(schema):
        print("agent-exec: required schema not found: %s" % schema, file=sys.stderr)
        return 2

    try:
        adapter = adapters.load(engine)
    except (ValueError, ImportError) as exc:
        print("agent-exec: %s" % exc, file=sys.stderr)
        return 2
    if not adapter.locate():
        print("agent-exec[%s]: engine binary not found." % adapter.id, file=sys.stderr)
        return 127
    code = adapter.preflight(schema)
    if code:
        return code
    # A reviewer must not run inside the reviewee's personal configuration.
    adapter.isolate(os.path.dirname(os.path.abspath(out_json)))
    log.diag("adapter=%s bin=%s inflight=%s native_schema=%s model=%s"
             % (adapter.id, adapter.bin, adapter.has_inflight, adapter.native_schema,
                model or "<default>"))

    try:
        argv = adapter.argv("review", model, prompt, schema, out_json)
    except adapters.AdapterRefused as exc:
        print("agent-exec: %s" % exc, file=sys.stderr)
        return 2

    with open(log_file, "a", encoding="utf-8") as fh:
        fh.write("=== agent-exec.py %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        fh.write("engine: %s (in-flight grammar: %s)\n" % (adapter.id, int(adapter.has_inflight)))
        fh.write("bin: %s\nmodel: %s\nmode: review (read-only)\n"
                 % (adapter.bin, model or "<engine default>"))
        fh.write("timeout: %ss\nschema: %s\nout_json: %s\nstatus: %s\n"
                 % (timeout_s, schema, out_json, status_file))

    sup = Supervisor(adapter, "review", log_file, status_file)
    rc = sup.run(argv, timeout_s,
                 tunable(["LIDER_STALL_S", "CODEX_STALL_S"], 180),
                 tunable(["LIDER_STARTUP_S", "CODEX_STARTUP_S"], 60),
                 tunable(["LIDER_RETRIES", "CODEX_RETRIES"], 1),
                 tunable(["LIDER_BACKOFF_S", "CODEX_BACKOFF_S"], 5))

    # One row per supervised run. This is the raw material for every question in
    # lider/metrics.py - which engine to route to, whether the timeouts fit, what
    # a review actually costs. Recorded on failure too: a run that timed out is a
    # data point about the timeout, not a row to omit.
    usage = sup.usage or {}
    metrics.record(os.environ.get("LIDER_METRICS_DIR") or os.getcwd(), "run",
                   tool="review", engine=adapter.id, model=model,
                   model_billed=usage.get("model_billed"),
                   exit=rc, elapsed_s=sup.elapsed_s,
                   cost_usd=usage.get("cost_usd"),
                   input_tokens=usage.get("input_tokens"),
                   output_tokens=usage.get("output_tokens"),
                   turns=usage.get("turns"),
                   stall_watchdog=sup.status.stall_armed,
                   measured=bool(sup.usage))

    def fail(message):
        print("agent-exec[%s]: %s" % (adapter.id, message), file=sys.stderr)
        sys.stderr.write(tail_filtered(log_file))

    if rc == 124:
        fail("timeout after %ss, process tree terminated." % timeout_s)
        return 124
    if rc == 125:
        fail("aborted early by watchdog (stalled or no startup output); see %s." % status_file)
        return 125
    if rc != 0:
        fail("engine finished with exit code %s." % rc)
        return rc

    # Engines that print instead of writing a file get their payload lifted out
    # of the log here. 3 means "finished but gave me nothing usable" - distinct
    # from the engine's own failure codes.
    if not adapter.extract(log_file, out_json):
        fail("engine finished ok but no usable result could be recovered into '%s'." % out_json)
        return 3
    if not os.path.exists(out_json) or os.path.getsize(out_json) == 0:
        fail("engine finished ok but '%s' is missing or empty." % out_json)
        return 3

    if adapter.native_schema:
        # The engine enforced the schema server-side; confirm it is parseable.
        try:
            with open(out_json, encoding="utf-8") as fh:
                json.load(fh)
        except (OSError, ValueError):
            fail("engine finished ok but '%s' is not valid JSON." % out_json)
            return 3
    else:
        # No server-side guarantee: check the shape here, or everything downstream
        # is trusting an unvalidated blob from a weaker model.
        if validate_file(schema, out_json) != 0:
            fail("engine finished ok but '%s' does not conform to %s." % (out_json, schema))
            return 3

    with open(log_file, "a", encoding="utf-8") as fh:
        fh.write("agent-exec.py[%s]: ok (%s valid).\n" % (adapter.id, out_json))
    # Update state on success
    if current_state is not None:
        new_state = state.increment_generation(current_state)
        new_state["phase"] = "review"
        state.update_metadata(new_state,
            engine_used=adapter.id,
            cost_usd=usage.get("cost_usd"),
            tokens_used=usage.get("input_tokens") + usage.get("output_tokens") if usage.get("input_tokens") and usage.get("output_tokens") else None)
        state.write_state_atomic(new_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
