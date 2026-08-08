"""OpenAI Codex CLI."""
import atexit
import os
import re
import shutil
import sys
import tempfile

from . import Adapter, AdapterRefused

KEEP_CONFIG = re.compile(r"^(model|model_reasoning_effort|service_tier)\s*=")


class CodexAdapter(Adapter):
    id = "codex"
    has_inflight = True       # grammar below is the CLI's own stream markers
    streams = True            # narrates as it works, so early silence is suspicious
    native_schema = True      # --output-schema is enforced server-side

    def locate(self):
        self.bin = shutil.which("codex")
        if not self.bin:
            # npm global installs are routinely absent from a non-interactive PATH.
            for extra in (os.path.join(os.environ.get("APPDATA", ""), "npm"),
                          os.path.join(os.environ.get("HOME", ""), "AppData", "Roaming", "npm")):
                if extra:
                    candidate = shutil.which("codex", path=extra)
                    if candidate:
                        self.bin = candidate
                        break
        return bool(self.bin)

    def _real_codex_home(self):
        """Locate the user's real ~/.codex directory."""
        if os.environ.get("CODEX_HOME"):
            return os.environ["CODEX_HOME"]
        # Windows uses USERPROFILE, Unix uses HOME
        for var in ("HOME", "USERPROFILE"):
            val = os.environ.get(var)
            if val:
                candidate = os.path.join(val, ".codex")
                if os.path.isdir(candidate):
                    return candidate
        return None

    def isolate(self, anchor_dir):
        """Point CODEX_HOME at a throwaway home.

        The user's real ~/.codex can carry broken skills, failing hooks and a
        multi-GB log DB that load on EVERY invocation - latency, token noise, and
        per-turn hook failures that push real calls toward the timeout. Only
        credentials and a few scalars are carried over.
        """
        real = self._real_codex_home()
        if not real:
            print("codex: cannot locate ~/.codex (tried CODEX_HOME, HOME, USERPROFILE).",
                  file=sys.stderr)
            return
        iso = tempfile.mkdtemp(prefix=".codex-iso-", dir=anchor_dir or None)
        atexit.register(shutil.rmtree, iso, True)
        try:
            shutil.copy(os.path.join(real, "auth.json"), os.path.join(iso, "auth.json"))
        except OSError:
            pass
        carried = []
        try:
            with open(os.path.join(real, "config.toml"), encoding="utf-8") as fh:
                carried = [ln for ln in fh if KEEP_CONFIG.match(ln.strip())]
        except OSError:
            pass
        with open(os.path.join(iso, "config.toml"), "w", encoding="utf-8") as fh:
            fh.writelines(carried)
            fh.write('approval_policy = "never"\n[features]\nmemories = false\nmulti_agent = false\n')
        os.environ["CODEX_HOME"] = iso
        self.iso_home, self.real_home = iso, real

    def preflight(self, schema):
        real = self._real_codex_home()
        if not real or not os.path.isfile(os.path.join(real, "auth.json")):
            print("codex preflight: WARNING no auth.json under '%s' - Codex may fail to "
                  "authenticate." % (real or "unknown"), file=sys.stderr)
        return 0

    def auth_hint(self):
        return "run 'codex login'"

    def activity(self, tail):
        act = ""
        lines = tail.splitlines()
        for i, line in enumerate(lines):
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if line == "exec" and nxt:
                act = "exec: " + nxt
            elif line.startswith("+++ b/"):
                act = "edit: " + line[6:]
            elif line == "apply patch":
                act = "edit: applying patch"
            elif re.search(r" succeeded in \d+ms", line):
                act = "cmd ok"
            elif re.search(r" failed in \d+ms", line):
                act = "cmd failed"
            elif re.search(r" exited \d+ ", line):
                act = "cmd exited"
            elif line == "codex" and nxt:
                act = "say: " + nxt
            elif line == "tokens used":
                act = "finalizing (counting tokens)"
        return act[:140]

    def inflight(self, chunk):
        state = None
        for line in chunk.splitlines():
            if line == "exec":
                state = True
            elif (re.search(r" (succeeded|failed) in \d+ms", line)
                  or re.search(r" exited \d+ ", line)
                  or line in ("apply patch", "codex", "tokens used")):
                state = False
        return state

    def classify_tail(self, tail):
        # MEASURED: an account without Codex access fails with "You've hit your
        # usage limit ... try again at <date>". The binary is on PATH and the
        # login is valid, so neither `locate()` nor an auth check catches it -
        # only the run does. Classified as fatal (a quota does not clear on a
        # retry) and named explicitly, because the generic "exit 1" reads like a
        # bug in the wrapper rather than a subscription that is not there.
        if "usage limit" in tail or "upgrade to plus" in tail:
            print("codex: this account has no Codex access (usage limit). "
                  "Use --engine claude or --engine grok.", file=sys.stderr)
            return "fatal"
        return "auth" if "codex login" in tail else ""

    def usage(self, log_path):
        """Not implemented: no verified transcript to write a parser against.

        The CLI prints a `tokens used` marker (the activity grammar keys off it),
        but the quota on this account expired before a successful run could be
        captured, so the exact fields are unknown. Returning None makes every run
        count as UNMEASURED in the aggregates - visibly missing rather than
        silently zero. Same rule as the in-flight grammar: do not write a parser
        against a format you have not seen.
        """
        return None

    def argv(self, mode, model, prompt, schema, out):
        cmd = [self.bin, "exec", "--skip-git-repo-check", "-c", "mcp_servers={}"]
        if mode == "review":
            cmd += ["--sandbox", "read-only"]
            if schema:
                cmd += ["--output-schema", schema, "-o", out]
        elif mode == "implement":
            # Intentional: lifts the workspace-write confinement (no writes outside
            # the repo, no network) that made out-of-repo specs unreadable and let a
            # task silently do nothing. Scope the prompt tightly instead.
            cmd += ["--sandbox", "danger-full-access"]
        else:
            raise AdapterRefused("codex: unknown mode %r" % mode)
        if model:
            cmd += ["--model", model]
        elif mode == "implement":
            # The CLI default model is not one this plugin allows, so an unpinned
            # implement run is a configuration error, not something to guess at.
            raise AdapterRefused("codex: implement mode requires an explicit --model")
        return cmd + [prompt]
