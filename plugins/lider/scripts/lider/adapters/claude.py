"""Claude Code CLI, headless (`claude -p`)."""
import json
import os
import re
import shutil

from . import Adapter, AdapterRefused
from ..extract import extract_to

NAME = re.compile(r'"name":"([^"]+)"')
TEXT = re.compile(r'"text":"([^"]{0,120})')


class ClaudeAdapter(Adapter):
    id = "claude"
    # --output-format stream-json emits one JSON event per line, so in-flight
    # state comes from STRUCTURED markers rather than a text grammar. Verified
    # against a real transcript: tool_use opens, tool_result closes, result ends.
    has_inflight = True
    streams = True            # stream-json: one event per line, from the first turn
    native_schema = True      # --json-schema is enforced by the CLI

    def locate(self):
        self.bin = shutil.which("claude")
        if not self.bin:
            for extra in (os.path.join(os.environ.get("HOME", ""), ".local", "bin"),
                          os.path.join(os.environ.get("APPDATA", ""), "npm")):
                if extra:
                    candidate = shutil.which("claude", path=extra)
                    if candidate:
                        self.bin = candidate
                        break
        return bool(self.bin)

    def auth_hint(self):
        return "run 'claude login' (or set ANTHROPIC_API_KEY)"

    def activity(self, tail):
        act = ""
        for line in tail.splitlines():
            if '"type":"tool_use"' in line:
                match = NAME.search(line)
                act = "tool: " + match.group(1) if match else "tool"
            elif '"type":"tool_result"' in line:
                act = "tool done"
            elif '"type":"text"' in line:
                match = TEXT.search(line)
                if match:
                    act = "say: " + match.group(1)
            elif '"type":"result"' in line:
                act = "finalizing"
        return act[:140]

    def inflight(self, chunk):
        state = None
        for line in chunk.splitlines():
            if '"type":"tool_use"' in line:
                state = True
            elif '"type":"tool_result"' in line or '"type":"result"' in line:
                state = False
        return state

    def classify_tail(self, tail):
        if any(s in tail for s in ("claude login", "invalid_api_key", "authentication_error")):
            return "auth"
        if any(s in tail for s in ("overloaded_error", "api_error")):
            return "retry"
        return ""

    def extract(self, log_path, out_path):
        return extract_to(log_path, out_path) == 0

    def usage(self, log_path):
        """Read the final stream-json `result` event.

        VERIFIED against a real transcript: it carries `total_cost_usd`, a `usage`
        block with the token breakdown, and `modelUsage` keyed by the model that
        was actually BILLED. That last key matters - a run launched with
        `--model haiku` was observed billing `claude-sonnet-5`, which nothing but
        this field would ever have revealed.
        """
        result = None
        try:
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    # Parse and inspect the field rather than substring-matching
                    # `"type":"result"`: that only holds for compact output, and
                    # a single space in the serialisation made the whole run read
                    # as cost-unmeasured.
                    if not line.startswith("{") or '"result"' not in line:
                        continue
                    try:
                        doc = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(doc, dict) and doc.get("type") == "result":
                        result = doc
        except OSError:
            return None
        if not result:
            return None
        used = result.get("usage") or {}
        billed = sorted(result.get("modelUsage") or {})
        return {
            "cost_usd": result.get("total_cost_usd"),
            "input_tokens": used.get("input_tokens"),
            "output_tokens": used.get("output_tokens"),
            "cache_read_tokens": used.get("cache_read_input_tokens"),
            "cache_write_tokens": used.get("cache_creation_input_tokens"),
            "model_billed": ",".join(billed) if billed else None,
            "turns": result.get("num_turns"),
            "duration_ms": result.get("duration_ms"),
        }

    def argv(self, mode, model, prompt, schema, out):
        cmd = [self.bin, "-p", prompt, "--output-format", "stream-json", "--verbose"]
        # MEASURED: --bare restricts Anthropic auth to ANTHROPIC_API_KEY and never
        # reads OAuth or the keychain, so under an OAuth login it exits 1 with
        # "Not logged in - Please run /login". Isolation is worth having, but not
        # at the cost of being unable to authenticate at all.
        if os.environ.get("ANTHROPIC_API_KEY"):
            cmd.append("--bare")
        if model:
            cmd += ["--model", model]
        if mode == "review":
            # MEASURED: --json-schema takes the schema INLINE, not a path. Given a
            # filename it fails with `not valid JSON: Unexpected identifier "C"`.
            if schema:
                with open(schema, encoding="utf-8") as fh:
                    cmd += ["--json-schema", fh.read()]
            # Read-only by permission rule, not by hope. Canary-tested: the model
            # attempted a Write and was blocked.
            cmd += ["--permission-mode", "plan",
                    "--disallowed-tools", "Edit,Write,NotebookEdit,Bash"]
        elif mode == "implement":
            cmd.append("--dangerously-skip-permissions")
        else:
            raise AdapterRefused("claude: unknown mode %r" % mode)
        return cmd
