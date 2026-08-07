"""Process supervision: the two watchdogs, the teardown, and the exit codes.

These drive a real child process, so they are the slow part of the suite - but
they are also the part where a regression is silent and expensive. Every case
here is a failure that actually happened.
"""
import json
import os
import sys
import time

import pytest

from lider.adapters.generic import GenericAdapter
from lider.runtime import Supervisor

pytestmark = pytest.mark.slow


def engine(tmp_path, body, name="engine.py"):
    path = tmp_path / name
    path.write_text("import sys, time\n" + body, encoding="utf-8")
    return str(path)


def supervise(tmp_path, script, adapter, **kw):
    log = tmp_path / (adapter.id + ".log")
    log.write_text("", encoding="utf-8")
    sup = Supervisor(adapter, "review", str(log), str(log) + ".status.json")
    sup.poll_s = 1
    rc = sup.run([sys.executable, script],
                 timeout_s=kw.get("timeout_s", 60), stall_s=kw.get("stall_s", 30),
                 startup_s=kw.get("startup_s", 10), retries=0, backoff_s=0)
    status = json.loads((log.parent / (log.name + ".status.json")).read_text(encoding="utf-8"))
    return rc, status, sup


class Streaming(GenericAdapter):
    id = "streaming-fake"
    has_inflight = True
    streams = True

    def inflight(self, chunk):
        state = None
        for line in chunk.splitlines():
            if line.strip() == "BEGIN":
                state = True
            elif line.strip() == "END":
                state = False
        return state


class Batch(GenericAdapter):
    id = "batch-fake"
    has_inflight = False
    streams = False


class TestStartupWatchdog:
    def test_a_non_streaming_engine_is_not_killed_for_early_silence(self, tmp_path):
        """MEASURED: grok emits one object at the end and wrote nothing before it.

        With the startup watchdog armed, that was not a health check - it was a
        guarantee that any run longer than the window died. A real review was
        aborted at 129s with exit 125 and an empty log.
        """
        script = engine(tmp_path, "time.sleep(14)\nprint('{}')\n")
        rc, status, _ = supervise(tmp_path, script, Batch(), startup_s=5, timeout_s=60)
        assert rc == 0
        assert status["startup_watchdog"] == 0

    def test_a_streaming_engine_that_stays_mute_still_fast_fails(self, tmp_path):
        """No regression: for an engine that should be talking, silence is a signal."""
        script = engine(tmp_path, "time.sleep(120)\n")
        started = time.time()
        rc, status, _ = supervise(tmp_path, script, Streaming(), startup_s=5, timeout_s=90)
        assert rc == 125
        assert status["startup_watchdog"] == 1
        assert time.time() - started < 30       # it failed fast, not at the timeout


class TestStallWatchdog:
    def test_silence_during_a_command_is_not_a_stall(self, tmp_path):
        """A healthy 8-minute test suite writes nothing. Killing it is the worse error."""
        script = engine(tmp_path, (
            "print('BEGIN', flush=True)\n"
            "time.sleep(14)\n"
            "print('END', flush=True)\n"
            "print('{}', flush=True)\n"))
        rc, status, _ = supervise(tmp_path, script, Streaming(),
                                  stall_s=5, startup_s=20, timeout_s=60)
        assert rc == 0
        assert status["stall_watchdog"] == 1     # armed, and it still did not fire

    def test_an_idle_engine_between_steps_is_a_stall(self, tmp_path):
        script = engine(tmp_path, (
            "print('thinking', flush=True)\n"
            "time.sleep(120)\n"))
        rc, _, _ = supervise(tmp_path, script, Streaming(),
                             stall_s=5, startup_s=20, timeout_s=90)
        assert rc == 125

    def test_an_adapter_with_no_grammar_disarms_the_stall_watchdog(self, tmp_path):
        script = engine(tmp_path, (
            "print('starting', flush=True)\n"
            "time.sleep(14)\n"
            "print('{}', flush=True)\n"))
        rc, status, _ = supervise(tmp_path, script, Batch(),
                                  stall_s=5, startup_s=20, timeout_s=60)
        assert rc == 0
        assert status["stall_watchdog"] == 0


class TestTeardown:
    def test_a_hang_hits_the_hard_timeout_and_leaves_no_grandchildren(self, tmp_path):
        """The bound that holds when every other signal is unreadable.

        The engine spawns a GRANDCHILD that keeps touching a file. After the
        teardown the file must stop growing - which is a real assertion about the
        whole tree dying, not just the process we launched. This is the property
        the native-PID teardown was written for.
        """
        beat = tmp_path / "beat.txt"
        child = engine(tmp_path, "\n".join([
            "p = sys.argv[1]",
            "while True:",
            "    open(p, 'a').write('x')",
            "    time.sleep(0.3)",
            ""]), name="child.py")
        script = engine(tmp_path, "\n".join([
            "import subprocess",
            "subprocess.Popen([sys.executable, %r, %r])" % (child, str(beat)),
            "print('spawned', flush=True)",
            "time.sleep(300)",
            ""]))

        rc, status, _ = supervise(tmp_path, script, Batch(),
                                  timeout_s=10, stall_s=0, startup_s=0)
        assert rc == 124
        assert status["exit"] == 124

        time.sleep(2)
        size = beat.stat().st_size if beat.exists() else 0
        time.sleep(2)
        after = beat.stat().st_size if beat.exists() else 0
        assert after == size, "a grandchild outlived the teardown and kept writing"


class TestLogHygiene:
    def test_our_notes_never_land_in_the_engine_transcript(self, tmp_path):
        """<log> is the engine's transcript, and the watchdog measures its growth.

        A note of ours in there is read back as engine activity - and it was:
        the stall disarm wrote into <log> for a whole release.
        """
        script = engine(tmp_path, "print('{}', flush=True)\n")
        log = tmp_path / "batch-fake.log"
        supervise(tmp_path, script, Batch(), stall_s=5, startup_s=5, timeout_s=30)
        assert "DISARMED" not in log.read_text(encoding="utf-8")


class TestClassification:
    @pytest.mark.parametrize("tail,expected", [
        ("http 429 too many requests", "retry"),
        ("503 service unavailable", "retry"),
        ("econnreset while streaming", "retry"),
        ("401 unauthorized", "auth"),
        ("you are not logged in", "auth"),
        ("TypeError: undefined is not a function", "fatal"),
    ])
    def test_the_error_tail_decides_whether_a_retry_is_worth_it(self, tmp_path, tail, expected):
        log = tmp_path / "c.log"
        log.write_text(tail, encoding="utf-8")
        sup = Supervisor(Batch(), "review", str(log), str(log) + ".status.json")
        assert sup.classify(1, 0) == expected

    def test_transient_codes_always_retry_and_permanent_ones_never_do(self, tmp_path):
        log = tmp_path / "c.log"
        log.write_text("", encoding="utf-8")
        sup = Supervisor(Batch(), "review", str(log), str(log) + ".status.json")
        assert sup.classify(0, 0) == "done"
        assert sup.classify(124, 0) == "retry"    # timeout
        assert sup.classify(125, 0) == "retry"    # watchdog
        assert sup.classify(127, 0) == "fatal"    # engine missing
        assert sup.classify(2, 0) == "fatal"      # bad usage
