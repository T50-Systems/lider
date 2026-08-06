"""lider.runtime - engine-agnostic supervision of one engine process.

Port of the former agent-runtime.sh. Same guarantees, same status contract, same
exit codes; the parts that were only ever data manipulation are now data
manipulation, and two things are genuinely stronger than the shell version:

  * PROCESS-TREE KILL. The shell had to map an MSYS pid to a Windows pid by
    parsing `ps` output before it could `taskkill //T`, and walked PPIDs as a
    supplement. Here the pid IS the native pid, so `taskkill /F /T` is exact on
    Windows and `killpg` on a new session is exact on POSIX. The whole class of
    "the walk missed a reparented grandchild" goes away.

  * STATUS SERIALISATION. The shell built status.json with printf and hand-rolled
    escaping. This uses a real serialiser, so no engine message can ever produce
    an unparseable status file.

One property was deliberately traded. The shell wrapped every launch in
`timeout -k 10`, which bounded the run even if the supervisor itself misbehaved.
Resolving a coreutils binary from Windows Python is exactly the trap that already
bit us once (`bash` resolving to the WSL shim), so the timeout is enforced here
instead - by a DAEMON WATCHDOG THREAD that is independent of the poll loop, so a
hung supervisor still gets the process killed at the deadline.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

from . import log

WINDOWS = os.name == "nt"

# Exit codes, unchanged from the shell version - callers and skills depend on them.
EXIT_TIMEOUT = 124
EXIT_WATCHDOG = 125
EXIT_NOT_FOUND = 127
EXIT_USAGE = 2
EXIT_CANCELLED = 130

AUTH_SIGNS = ("unauthorized", "not logged in", "invalid api key", "authentication failed",
              "401 ", "token expired", "please log in", "forbidden", "403 ")
TRANSIENT_SIGNS = (" 429", "429 ", "too many requests", "rate limit", " 500", " 502", " 503",
                   " 504", " 408", "internal server error", "bad gateway", "service unavailable",
                   "gateway timeout", "overloaded", "econnreset", "etimedout", "econnrefused",
                   "stream disconnected", "connection reset", "temporarily unavailable",
                   "timed out")


def env_int(name, default, minimum=None, maximum=None):
    try:
        value = int(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def tunable(names, default, minimum=None, maximum=None):
    """First name that is set wins. LIDER_* is canonical; CODEX_* stays accepted."""
    for name in names:
        if os.environ.get(name):
            return env_int(name, default, minimum, maximum)
    return default


def posix_bash():
    """The POSIX bash that can actually run a shell script, or None.

    MEASURED, twice: (1) on Windows, `subprocess` cannot exec a shebang script at
    all - it needs an explicit interpreter; (2) resolving "bash" off PATH finds
    C:\\Windows\\System32\\bash.exe, the WSL shim, which then fails with
    `execvpe(/bin/bash) failed: No such file or directory` because a Windows path
    means nothing in WSL's filesystem namespace. So: an explicit override, then
    the shell we were launched from, then known Git Bash locations, and only then
    PATH with System32 excluded.
    """
    override = os.environ.get("LIDER_BASH")
    if override and os.path.exists(override):
        return override
    shell = os.environ.get("SHELL", "")
    if shell and "bash" in os.path.basename(shell).lower() and os.path.exists(shell):
        return shell
    if WINDOWS:
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            if base:
                candidate = os.path.join(base, "Git", "bin", "bash.exe")
                if os.path.exists(candidate):
                    return candidate
    import shutil
    found = shutil.which("bash")
    if found and "system32" not in found.replace("\\", "/").lower():
        return found
    return None


def interpreter_for(path):
    """Argv prefix needed to run `path`, for launchers that cannot use a shebang."""
    if path and path.lower().endswith((".sh", ".bash")):
        bash = posix_bash()
        if not bash:
            raise OSError("no POSIX bash found to run %s (set LIDER_BASH)" % path)
        return [bash]
    return []


def kill_tree(proc):
    """Kill a process and everything it spawned, leaving no orphans."""
    if proc is None or proc.poll() is not None:
        return
    log.diag("kill_tree: pid=%s (%s)" % (proc.pid, "taskkill /T" if WINDOWS else "killpg"))
    if WINDOWS:
        # /T covers the whole tree; the pid is native, so no ps parsing is needed.
        subprocess.call(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            pass
        deadline = time.time() + 3
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.2)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass


class Status(object):
    """The live status file a watcher polls. Written atomically, always parseable."""

    def __init__(self, path, engine, tool):
        self.path = path
        self.engine = engine
        self.tool = tool
        self.started_at = int(time.time())
        self.stall_armed = 0
        self.usage = None

    def write(self, state, attempt=0, max_attempts=0, pid=None, elapsed=0, idle=0,
              log_bytes=0, exit_code=None, reason="", activity=""):
        if not self.path:
            return
        doc = {
            "engine": self.engine, "tool": self.tool, "state": state,
            "attempt": attempt, "max_attempts": max_attempts, "pid": pid,
            "elapsed_s": elapsed, "idle_s": idle, "log_bytes": log_bytes,
            "exit": exit_code, "reason": reason, "activity": activity[:180],
            "stall_watchdog": self.stall_armed, "usage": self.usage,
            "started_at": self.started_at, "updated_at": int(time.time()),
        }
        directory = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, self.path)
        except OSError:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


class Supervisor(object):
    """Runs one engine invocation with heartbeat, watchdogs, retry and teardown."""

    def __init__(self, adapter, tool, log_path, status_path):
        self.adapter = adapter
        self.tool = tool
        self.log_path = log_path
        self.status = Status(status_path, adapter.id, tool)
        self.poll_s = tunable(["LIDER_POLL_S", "CODEX_POLL_S"], 5, 1)
        self.heartbeat_s = tunable(["LIDER_HEARTBEAT_S", "CODEX_HEARTBEAT_S"], 10, 1)
        self.retry_hook = None      # callable -> bool; must succeed before a retry
        self._proc = None
        self._baseline = 0          # log size at launch; activity never reads below it
        self.usage = None           # what the engine reported it consumed
        self.elapsed_s = 0

    # -- logging helpers ---------------------------------------------------
    def _log_size(self):
        try:
            return os.path.getsize(self.log_path)
        except OSError:
            return 0

    def _read_from(self, offset, limit=None):
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(offset)
                data = fh.read() if limit is None else fh.read(limit)
            return data.decode("utf-8", "replace")
        except OSError:
            return ""

    def _tail(self, nbytes=20000):
        """The tail of THIS run's engine output.

        Floored at the pre-launch baseline so the log header - our own metadata,
        written before the engine started - can never be reported back as "what
        the engine is doing right now".
        """
        size = self._log_size()
        return self._read_from(max(self._baseline, size - nbytes))

    def _heartbeat(self, attempt, maxa, elapsed, idle, nbytes, activity):
        # Live narration: this is what the operator watches while work runs.
        log.emit("[%s] %s/%s attempt=%s/%s elapsed=%ss idle=%ss log=%sB | %s"
                 % (time.strftime("%H:%M:%S"), self.adapter.id, self.tool, attempt, maxa,
                    elapsed, idle, nbytes, activity or "..."))

    # -- one attempt -------------------------------------------------------
    def _once(self, argv, timeout_s, stall_s, startup_s, attempt, maxa):
        start = time.time()
        baseline = self._log_size()
        self._baseline = baseline
        offset = baseline
        prev_bytes = baseline
        last_out = start
        last_hb = start
        grew = False
        inflight = False
        activity = ""
        reason = ""

        self.status.write("starting", attempt, maxa)

        popen_kwargs = {}
        if WINDOWS:
            # Own console-process group so a Ctrl-C in the parent's console does not
            # race our own teardown.
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True   # a killable process group

        logfh = open(self.log_path, "ab")
        try:
            proc = subprocess.Popen(argv, stdout=logfh, stderr=subprocess.STDOUT,
                                    **popen_kwargs)
        except OSError as exc:
            logfh.write(("runtime: could not launch: %s\n" % exc).encode("utf-8"))
            logfh.close()
            self.status.write("failed", attempt, maxa, exit_code=EXIT_NOT_FOUND,
                              reason="launch-failed")
            return EXIT_NOT_FOUND, "launch-failed"
        finally:
            logfh.close()

        self._proc = proc
        log.diag_argv("launch[%s/%s]" % (self.adapter.id, self.tool), argv)
        log.diag("pid=%s timeout=%ss stall=%ss startup=%ss"
                 % (proc.pid, timeout_s, stall_s, startup_s))

        # The hard bound, independent of the loop below: if this supervisor itself
        # wedges, the deadline still fires and the tree still dies.
        hard = {"fired": False}

        def _deadline():
            if proc.poll() is None:
                hard["fired"] = True
                kill_tree(proc)

        timer = threading.Timer(timeout_s, _deadline)
        timer.daemon = True
        timer.start()

        try:
            while True:
                time.sleep(self.poll_s)
                now = time.time()
                elapsed = int(now - start)

                if proc.poll() is not None:
                    break

                nbytes = self._log_size()
                if nbytes > prev_bytes:
                    last_out = now
                    prev_bytes = nbytes
                    grew = True
                idle = int(now - last_out)

                # In-flight state is tracked incrementally over newly appended
                # COMPLETE lines: a verbose command that scrolls its start marker
                # out of any display window can never flip the state and get
                # healthy work killed.
                if self.adapter.has_inflight and nbytes > offset:
                    chunk = self._read_from(offset, nbytes - offset)
                    cut = chunk.rfind("\n")
                    if cut >= 0:
                        complete = chunk[:cut + 1]
                        offset += len(complete.encode("utf-8"))
                        delta = self.adapter.inflight(complete)
                        if delta is not None:
                            inflight = delta

                found = self.adapter.activity(self._tail())
                if found:
                    activity = found
                shown = activity + (" (running %ss)" % idle if inflight else "")

                self.status.write("running", attempt, maxa, proc.pid, elapsed, idle,
                                  nbytes, activity=shown)
                if now - last_hb >= self.heartbeat_s:
                    self._heartbeat(attempt, maxa, elapsed, idle, nbytes, shown)
                    last_hb = now

                if not grew and elapsed >= startup_s:
                    reason = "startup-failed"
                    break
                # A stall is the engine being idle - NOT a shell command running,
                # whose silence is expected. stall_s == 0 means disarmed.
                if stall_s > 0 and grew and not inflight and idle >= stall_s:
                    reason = "stalled"
                    break
        except KeyboardInterrupt:
            timer.cancel()
            kill_tree(proc)
            self.status.write("cancelled", attempt, maxa, exit_code=EXIT_CANCELLED,
                              reason="signal")
            raise
        finally:
            timer.cancel()

        if reason:
            if proc.poll() is None:
                kill_tree(proc)
                rc = EXIT_WATCHDOG
            else:
                # It finished between the last liveness check and the abort.
                reason = ""
                rc = proc.returncode
        else:
            rc = proc.wait()

        if hard["fired"]:
            rc, reason = EXIT_TIMEOUT, "timeout"
        elif not reason:
            reason = "ok" if rc == 0 else "exit-%s" % rc

        self._proc = None
        found = self.adapter.activity(self._tail())
        if found:
            activity = found
        self.status.write("done" if rc == 0 else "failed", attempt, maxa,
                          elapsed=int(time.time() - start), log_bytes=self._log_size(),
                          exit_code=rc, reason=reason, activity=activity)
        return rc, reason

    # -- classification ----------------------------------------------------
    def classify(self, rc, from_offset):
        """done | retry | auth | fatal, from the exit code and THIS attempt's tail."""
        if rc == 0:
            return "done"
        if rc in (EXIT_TIMEOUT, EXIT_WATCHDOG):
            return "retry"
        if rc in (EXIT_USAGE, EXIT_NOT_FOUND):
            return "fatal"
        # Only this attempt's output: a previous attempt's 429 must not
        # misclassify the current failure.
        tail = self._read_from(from_offset)[-2000:].lower()
        verdict = self.adapter.classify_tail(tail)
        log.diag("classify: rc=%s adapter=%r tail=%r" % (rc, verdict, tail[-200:]))
        if verdict in ("auth", "retry", "fatal"):
            return verdict
        if any(sign in tail for sign in AUTH_SIGNS):
            return "auth"
        if any(sign in tail for sign in TRANSIENT_SIGNS):
            return "retry"
        return "fatal"

    # -- public entry point ------------------------------------------------
    def run(self, argv, timeout_s, stall_s, startup_s, retries, backoff_s):
        if timeout_s < 1:
            print("runtime: timeout must be >= 1s", file=sys.stderr)
            return EXIT_USAGE
        retries = max(0, min(10, retries))
        backoff_s = max(0, min(60, backoff_s))

        # An adapter that cannot report in-flight state DISARMS the stall watchdog
        # rather than guessing. This is the plugin's own three-outcome rule applied
        # to its own supervision: "I cannot tell whether it is stalled" is not "it
        # is stalled", and killing a healthy 8-minute build is the worse error.
        if not self.adapter.has_inflight and stall_s > 0:
            stall_s = 0
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write("runtime: adapter '%s' reports no in-flight grammar - stall watchdog "
                         "DISARMED (hard timeout %ss still applies).\n"
                         % (self.adapter.id, timeout_s))
        self.status.stall_armed = 1 if stall_s > 0 else 0

        maxa = retries + 1
        rc = 0
        for attempt in range(1, maxa + 1):
            start_offset = self._log_size()
            rc, _reason = self._once(argv, timeout_s, stall_s, startup_s, attempt, maxa)
            verdict = self.classify(rc, start_offset)
            if verdict == "done":
                break
            if verdict == "auth":
                print("%s/%s: authentication failed - %s, then retry. Not auto-retrying."
                      % (self.adapter.id, self.tool, self.adapter.auth_hint()), file=sys.stderr)
                break
            if verdict == "fatal" or attempt >= maxa:
                break
            # A retry precondition that cannot be met means NOT retrying: re-running
            # over a half-written tree is unsafe.
            if self.retry_hook and not self.retry_hook():
                print("%s/%s: retry precondition failed; not retrying."
                      % (self.adapter.id, self.tool), file=sys.stderr)
                break
            delay = min(60, backoff_s * (2 ** min(6, attempt - 1)))
            if backoff_s:
                delay = min(60, delay + int(time.time() * 1000) % backoff_s)
            print("[%s] %s/%s attempt %s hit exit %s (%s); retrying in %ss..."
                  % (time.strftime("%H:%M:%S"), self.adapter.id, self.tool, attempt, rc,
                     verdict, delay), flush=True)
            self.status.write("retrying", attempt, maxa, exit_code=rc, reason="retry")
            time.sleep(delay)
        # Read what the run consumed AFTER it ends. Unknown stays None here and
        # all the way down: an unmeasured cost must never average in as zero.
        self.elapsed_s = int(time.time() - self.status.started_at)
        try:
            self.usage = self.adapter.usage(self.log_path)
        except Exception as exc:                      # never let accounting break a run
            log.diag("usage read failed: %s" % exc)
            self.usage = None
        self.status.usage = self.usage
        if self.usage:
            cost = self.usage.get("cost_usd")
            log.emit("%s/%s usage: %s | in=%s out=%s | billed=%s"
                     % (self.adapter.id, self.tool,
                        ("$%.4f" % cost) if cost is not None else "cost UNKNOWN",
                        self.usage.get("input_tokens"), self.usage.get("output_tokens"),
                        self.usage.get("model_billed") or "?"))
        self.status.write("done" if rc == 0 else "failed", maxa, maxa,
                          elapsed=self.elapsed_s, exit_code=rc,
                          reason="ok" if rc == 0 else "failed")
        return rc
