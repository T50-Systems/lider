#!/usr/bin/env python3
"""agent-implement.py - engine-agnostic IMPLEMENTER wrapper for the pipeline.

Runs an engine with WRITE access (whatever "full access" means for that engine -
see its adapter) on the CURRENT working directory.

Designed to run in the BACKGROUND: the orchestrator polls `git status --short`
for progress, <log_file>.status.json for live state, and <done_file> for
completion. A heartbeat is printed to stdout while it runs.

AUTO-RETRY is SAFE-GATED: enabled (once, on transient failures) ONLY when the
working tree is CLEAN in a git repo at launch, so recovery is a precise reset to
the launch checkpoint. On a dirty tree or non-repo it is disabled and recovery is
the orchestrator's call.

Usage: agent-implement.py [--engine <id>] <timeout_s> <log_file> <done_file> <model_slug> <prompt>

Exit codes (also written to <done_file> for the watcher):
  0 ok | 124 timeout | 125 watchdog abort | 127 engine not found
  2 bad usage / adapter refused the mode | N the engine's own code
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lider import adapters                      # noqa: E402
from lider.adapters import DEFAULT_ENGINE       # noqa: E402
from lider.runtime import Supervisor, tunable   # noqa: E402
from lider import metrics                       # noqa: E402
from lider import state                         # noqa: E402
from lider import log                           # noqa: E402


def git(*args):
    """Run a git command, returning (rc, stdout). Never raises."""
    try:
        proc = subprocess.run(["git"] + list(args), capture_output=True, text=True)
        return proc.returncode, proc.stdout.strip()
    except OSError:
        return 1, ""


class Checkpoint(object):
    """A clean-tree launch point we can precisely return to before a retry.

    Blindly re-running an implementer that died mid-write is unsafe, so this only
    arms when the tree is verifiably clean in a git repo. Recovery refuses if HEAD
    moved to a different ref since launch (never destroy another branch's
    commits), preserves ignored inputs like .local/ (no -x), and only reports
    success if the tree is verifiably clean again.
    """

    def __init__(self):
        self.root = self.ref = self.head = None

    def arm(self, log_file):
        rc, _ = git("rev-parse", "--is-inside-work-tree")
        if rc != 0:
            self._note(log_file, "auto-retry: disabled (not a git repo)")
            return 0
        rc, dirty = git("status", "--porcelain")
        if rc != 0 or dirty:
            self._note(log_file, "auto-retry: disabled (working tree not verifiably clean)")
            return 0
        _, self.root = git("rev-parse", "--show-toplevel")
        rc, self.ref = git("symbolic-ref", "-q", "HEAD")
        if rc != 0:
            self.ref = "DETACHED"
        _, self.head = git("rev-parse", "HEAD")
        if not (self.root and self.head):
            self._note(log_file, "auto-retry: disabled (could not pin a checkpoint)")
            return 0
        self._note(log_file, "auto-retry: enabled (clean-tree checkpoint %s on %s)"
                   % (self.head, self.ref))
        return tunable(["LIDER_RETRIES", "CODEX_RETRIES"], 1)

    @staticmethod
    def _note(log_file, text):
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    def restore(self):
        if not (self.head and self.root):
            return False
        rc, root_now = git("-C", self.root, "rev-parse", "--show-toplevel")
        if rc != 0 or root_now != self.root:
            return False
        rc, ref_now = git("-C", self.root, "symbolic-ref", "-q", "HEAD")
        if rc != 0:
            ref_now = "DETACHED"
        if ref_now != self.ref:
            return False            # HEAD moved to another branch -> refuse
        if git("-C", self.root, "reset", "--hard", self.head)[0] != 0:
            return False
        if git("-C", self.root, "clean", "-ffd")[0] != 0:
            return False
        # Submodules back to the SUPERPROJECT-recorded commits, then scrubbed.
        git("-C", self.root, "submodule", "update", "--init", "--force", "--recursive")
        git("-C", self.root, "submodule", "foreach", "--recursive",
            "git reset --hard && git clean -ffd")
        return git("-C", self.root, "status", "--porcelain")[1] == ""


def main():
    argv = sys.argv[1:]
    engine = os.environ.get("LIDER_ENGINE", DEFAULT_ENGINE)
    while argv and argv[0].startswith("--"):
        flag = argv.pop(0)
        if flag == "--":
            break
        name, _, inline = flag.partition("=")
        value = inline if inline else (argv.pop(0) if argv else "")
        if name == "--engine" and value:
            engine = value
        else:
            print("agent-implement: bad flag %s" % name, file=sys.stderr)
            return 2
    if len(argv) < 5:
        print("Usage: agent-implement.py [--engine <id>] <timeout_s> <log_file> "
              "<done_file> <model_slug> <prompt>", file=sys.stderr)
        return 2

    try:
        timeout_s = int(argv[0])
    except ValueError:
        print("agent-implement: invalid timeout %r" % argv[0], file=sys.stderr)
        return 2
    log_file, done_file, model = argv[1], argv[2], argv[3]
    prompt = " ".join(argv[4:])
    status_file = log_file + ".status.json"

    # Load and validate state
    current_state = state.read_state()
    expected_gen = current_state.get("generation_id", 1) if current_state else 1

    def finish(code):
        with open(done_file, "w", encoding="utf-8") as fh:
            fh.write("%s\n" % code)
        return code

    # Validate generation before starting
    if current_state is not None:
        valid, msg = state.validate_generation(current_state, expected_gen)
        if not valid:
            print("agent-implement: %s" % msg, file=sys.stderr)
            return finish(2)
    
    try:
        adapter = adapters.load(engine)
    except (ValueError, ImportError) as exc:
        print("agent-implement: %s" % exc, file=sys.stderr)
        return finish(2)
    if not adapter.locate():
        print("agent-implement[%s]: engine binary not found." % adapter.id, file=sys.stderr)
        return finish(127)
    adapter.preflight(None)
    adapter.isolate(os.path.dirname(os.path.abspath(log_file)))

    try:
        argv_cmd = adapter.argv("implement", model, prompt, "", "")
    except adapters.AdapterRefused as exc:
        print("agent-implement: %s" % exc, file=sys.stderr)
        return finish(2)

    with open(log_file, "a", encoding="utf-8") as fh:
        fh.write("=== agent-implement.py %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        fh.write("engine: %s (in-flight grammar: %s)\n" % (adapter.id, int(adapter.has_inflight)))
        fh.write("bin: %s\nmodel: %s\n" % (adapter.bin, model or "<engine default>"))
        fh.write("mode: implement (WRITE ACCESS - scope the prompt tightly)\n")
        fh.write("workdir: %s\ntimeout: %ss\nstatus: %s\n" % (os.getcwd(), timeout_s, status_file))

    checkpoint = Checkpoint()
    retries = checkpoint.arm(log_file)

    sup = Supervisor(adapter, "implement", log_file, status_file)
    if retries:
        sup.retry_hook = checkpoint.restore
    rc = sup.run(argv_cmd, timeout_s,
                 tunable(["LIDER_STALL_S", "CODEX_STALL_S"], 300),
                 tunable(["LIDER_STARTUP_S", "CODEX_STARTUP_S"], 60),
                 retries,
                 tunable(["LIDER_BACKOFF_S", "CODEX_BACKOFF_S"], 5))

    usage = sup.usage or {}
    metrics.record(os.environ.get("LIDER_METRICS_DIR") or os.getcwd(), "run",
                   tool="implement", engine=adapter.id, model=model,
                   model_billed=usage.get("model_billed"),
                   exit=rc, elapsed_s=sup.elapsed_s,
                   cost_usd=usage.get("cost_usd"),
                   input_tokens=usage.get("input_tokens"),
                   output_tokens=usage.get("output_tokens"),
                   turns=usage.get("turns"),
                   stall_watchdog=sup.status.stall_armed,
                   measured=bool(sup.usage))

    if rc == 124:
        print("agent-implement[%s]: timeout after %ss, process tree terminated."
              % (adapter.id, timeout_s), file=sys.stderr)
    elif rc == 125:
        print("agent-implement[%s]: aborted early by watchdog; see %s."
              % (adapter.id, status_file), file=sys.stderr)
    elif rc != 0:
        print("agent-implement[%s]: engine finished with exit code %s."
              % (adapter.id, rc), file=sys.stderr)
    else:
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write("agent-implement.py[%s]: ok.\n" % adapter.id)
        # Update state on success
        if current_state is not None:
            new_state = state.increment_generation(current_state)
            new_state["phase"] = "implement"
            state.update_metadata(new_state,
                engine_used=adapter.id,
                cost_usd=usage.get("cost_usd"),
                tokens_used=usage.get("input_tokens") + usage.get("output_tokens") if usage.get("input_tokens") and usage.get("output_tokens") else None)
            state.write_state_atomic(new_state)
    return finish(rc)


if __name__ == "__main__":
    sys.exit(main())
