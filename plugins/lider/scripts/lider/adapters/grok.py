"""Grok Build (xAI) CLI."""
import json
import os
import re
import shutil

from . import Adapter, AdapterRefused
from ..extract import extract_to


class GrokAdapter(Adapter):
    id = "grok"
    # `--output-format json` emits ONE object at the END of the run, so there is
    # no per-step signal to separate "running a long command" from "hung". Per the
    # contract that leaves the stall watchdog disarmed rather than guessing. If a
    # streaming format is confirmed later, add its grammar and flip this - not
    # before.
    has_inflight = False

    def locate(self):
        self.bin = shutil.which("grok")
        if not self.bin:
            home = os.environ.get("HOME", "")
            for candidate in (os.path.join(home, ".grok", "bin", "grok"),
                              os.path.join(home, ".grok", "bin", "grok.exe")):
                if os.path.exists(candidate):
                    self.bin = candidate
                    break
        return bool(self.bin)

    def auth_hint(self):
        return "run 'grok login' or set XAI_API_KEY"

    def classify_tail(self, tail):
        if any(s in tail for s in ("grok login", "xai_api_key", "not authenticated")):
            return "auth"
        return ""

    def extract(self, log_path, out_path):
        return extract_to(log_path, out_path) == 0

    def usage(self, log_path):
        """Read the final JSON object.

        DOCUMENTED TRAP: `total_cost_usd` is ABSENT under browser-login/OAuth. Its
        absence means "not reported", not "free" - so it stays None and the
        aggregates count it as unmeasured rather than adding a zero.
        """
        try:
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return None
        doc = None
        for match in re.finditer(r"\{[^{}]*\"(?:total_cost_usd|usage)\"[^{}]*\}", text):
            try:
                doc = json.loads(match.group(0))
            except ValueError:
                continue
        if not doc:
            return None
        used = doc.get("usage") or {}
        return {
            "cost_usd": doc.get("total_cost_usd"),
            "input_tokens": used.get("input_tokens") or used.get("prompt_tokens"),
            "output_tokens": used.get("output_tokens") or used.get("completion_tokens"),
        }

    def argv(self, mode, model, prompt, schema, out):
        # Effort is pinned high on every call: a standing rule for this engine
        # here, not a default worth overriding. (grok-4.5 accepts only
        # low|medium|high; anything else hard-errors.)
        cmd = [self.bin, "-p", prompt, "--output-format", "json", "--effort", "high"]
        if model:
            cmd += ["--model", model]
        turns = os.environ.get("LIDER_GROK_MAX_TURNS")
        if turns:
            cmd += ["--max-turns", turns]
        if mode == "review":
            # VERIFIED: `--disallowed-tools` FAILS OPEN - an adversarial prompt with
            # every write tool "disallowed" still overwrote its target. Only
            # permission RULES hold, and deny beats allow even under bypass.
            # `--read-only` does not exist; do not add it back.
            cmd += ["--permission-mode", "dontAsk",
                    "--deny", "Edit", "--deny", "Write", "--deny", "Bash"]
        elif mode == "implement":
            # Letting Grok write is a separate approval boundary upstream of this
            # adapter; the orchestrator is responsible for having obtained it.
            cmd.append("--yolo")
        else:
            raise AdapterRefused("grok: unknown mode %r" % mode)
        return cmd
