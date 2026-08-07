"""Runtime internals: interpreter resolution, the retry ladder, usage accounting.

The watchdogs and teardown are covered in test_supervisor.py (slow, real
processes). These are the paths around them that only ran when something went
wrong - which is exactly when you want them to have been tested.
"""
import os
import sys

import pytest

from lider import runtime
from lider.adapters.generic import GenericAdapter
from lider.runtime import Supervisor, interpreter_for, posix_bash


class Fake(GenericAdapter):
    id = "fake"
    has_inflight = False
    streams = False


def supervisor(tmp_path, adapter=None):
    log = tmp_path / "r.log"
    log.write_text("", encoding="utf-8")
    return Supervisor(adapter or Fake(), "review", str(log), str(log) + ".status.json")


class TestFindingAPosixBash:
    """MEASURED, twice: `subprocess` cannot exec a shebang on Windows, and a bare
    "bash" resolves to the WSL shim in System32, which cannot see Windows paths."""

    def test_an_explicit_override_wins(self, tmp_path, monkeypatch):
        override = tmp_path / "mybash.exe"
        override.write_text("", encoding="utf-8")
        monkeypatch.setenv("LIDER_BASH", str(override))
        assert posix_bash() == str(override)

    def test_an_override_pointing_nowhere_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LIDER_BASH", str(tmp_path / "absent"))
        monkeypatch.setenv("SHELL", "")
        assert posix_bash() != str(tmp_path / "absent")

    def test_the_shell_we_were_launched_from_is_tried_next(self, tmp_path, monkeypatch):
        shell = tmp_path / "bash.exe"
        shell.write_text("", encoding="utf-8")
        monkeypatch.delenv("LIDER_BASH", raising=False)
        monkeypatch.setenv("SHELL", str(shell))
        assert posix_bash() == str(shell)

    def test_a_non_bash_shell_is_not_mistaken_for_one(self, tmp_path, monkeypatch):
        shell = tmp_path / "zsh"
        shell.write_text("", encoding="utf-8")
        monkeypatch.delenv("LIDER_BASH", raising=False)
        monkeypatch.setenv("SHELL", str(shell))
        assert posix_bash() != str(shell)

    def test_the_wsl_shim_in_system32_is_never_returned(self, tmp_path, monkeypatch):
        """The trap: it exists, it is called bash, and it cannot see Windows paths."""
        shim = tmp_path / "System32"
        shim.mkdir()
        (shim / "bash.exe").write_text("", encoding="utf-8")
        monkeypatch.delenv("LIDER_BASH", raising=False)
        monkeypatch.setenv("SHELL", "")
        monkeypatch.setenv("PATH", str(shim))
        monkeypatch.setenv("ProgramFiles", str(tmp_path / "nope"))
        monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "nope"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nope"))
        assert posix_bash() is None

    def test_a_git_bash_install_is_found_on_windows(self, tmp_path, monkeypatch):
        if os.name != "nt":
            pytest.skip("Windows-specific lookup")
        base = tmp_path / "PF"
        (base / "Git" / "bin").mkdir(parents=True)
        (base / "Git" / "bin" / "bash.exe").write_text("", encoding="utf-8")
        monkeypatch.delenv("LIDER_BASH", raising=False)
        monkeypatch.setenv("SHELL", "")
        monkeypatch.setenv("ProgramFiles", str(base))
        assert posix_bash() == str(base / "Git" / "bin" / "bash.exe")


class TestInterpreterResolution:
    def test_a_shell_script_gets_an_explicit_interpreter(self, tmp_path, monkeypatch):
        bash = tmp_path / "bash.exe"
        bash.write_text("", encoding="utf-8")
        monkeypatch.setenv("LIDER_BASH", str(bash))
        assert interpreter_for("/x/engine.sh") == [str(bash)]
        assert interpreter_for("/x/engine.bash") == [str(bash)]

    def test_anything_else_needs_no_prefix(self):
        assert interpreter_for("/x/engine.exe") == []
        assert interpreter_for("") == []

    def test_no_bash_at_all_is_an_explicit_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LIDER_BASH", raising=False)
        monkeypatch.setenv("SHELL", "")
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setenv("ProgramFiles", str(tmp_path / "nope"))
        monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "nope"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nope"))
        with pytest.raises(OSError) as excinfo:
            interpreter_for("/x/engine.sh")
        assert "LIDER_BASH" in str(excinfo.value)


class TestTunables:
    def test_lider_names_win_and_codex_names_still_work(self, monkeypatch):
        monkeypatch.delenv("LIDER_STALL_S", raising=False)
        monkeypatch.setenv("CODEX_STALL_S", "77")
        assert runtime.tunable(["LIDER_STALL_S", "CODEX_STALL_S"], 300) == 77
        monkeypatch.setenv("LIDER_STALL_S", "12")
        assert runtime.tunable(["LIDER_STALL_S", "CODEX_STALL_S"], 300) == 12

    def test_junk_falls_back_to_the_default_rather_than_crashing(self, monkeypatch):
        monkeypatch.setenv("LIDER_POLL_S", "not-a-number")
        assert runtime.env_int("LIDER_POLL_S", 5) == 5

    def test_bounds_are_applied(self, monkeypatch):
        monkeypatch.setenv("X", "999")
        assert runtime.env_int("X", 5, minimum=1, maximum=60) == 60
        monkeypatch.setenv("X", "0")
        assert runtime.env_int("X", 5, minimum=1) == 1

    def test_an_unset_name_uses_the_default(self, monkeypatch):
        monkeypatch.delenv("NOPE_A", raising=False)
        monkeypatch.delenv("NOPE_B", raising=False)
        assert runtime.tunable(["NOPE_A", "NOPE_B"], 42) == 42


class TestTheRetryLadder:
    def _engine(self, tmp_path, body, name="e.py"):
        script = tmp_path / name
        script.write_text("import sys, time\n" + body, encoding="utf-8")
        return [sys.executable, str(script)]

    def test_an_invalid_timeout_is_a_usage_error(self, tmp_path):
        sup = supervisor(tmp_path)
        assert sup.run([sys.executable, "-c", "pass"], 0, 0, 0, 0, 0) == 2

    def test_an_auth_failure_is_reported_and_NOT_retried(self, tmp_path, capsys):
        """Retrying a 401 just burns attempts."""
        argv = self._engine(tmp_path, "print('401 unauthorized'); sys.exit(1)\n")
        sup = supervisor(tmp_path)
        sup.poll_s = 1
        assert sup.run(argv, 30, 0, 0, retries=3, backoff_s=0) == 1
        assert "authentication failed" in capsys.readouterr().err

    def test_a_deterministic_error_is_fatal_and_not_retried(self, tmp_path):
        argv = self._engine(tmp_path, "print('TypeError: boom'); sys.exit(1)\n")
        sup = supervisor(tmp_path)
        sup.poll_s = 1
        assert sup.run(argv, 30, 0, 0, retries=3, backoff_s=0) == 1
        assert sup.status.stall_armed == 0

    def test_a_transient_error_is_retried_up_to_the_limit(self, tmp_path):
        counter = tmp_path / "n"
        argv = self._engine(tmp_path, (
            "import os\n"
            "p = %r\n"
            "n = (open(p).read() if os.path.exists(p) else '') + 'x'\n"
            "open(p, 'w').write(n)\n"
            "print('http 429 too many requests')\n"
            "sys.exit(1)\n" % str(counter)))
        sup = supervisor(tmp_path)
        sup.poll_s = 1
        sup.run(argv, 30, 0, 0, retries=2, backoff_s=0)
        assert len(counter.read_text(encoding="utf-8")) == 3   # first try + 2 retries

    def test_a_retry_precondition_that_cannot_be_met_stops_the_ladder(self, tmp_path, capsys):
        """Re-running over a half-written tree is unsafe, so it is not attempted."""
        counter = tmp_path / "n"
        argv = self._engine(tmp_path, (
            "import os\n"
            "p = %r\n"
            "open(p, 'a').write('x')\n"
            "print('503 service unavailable')\n"
            "sys.exit(1)\n" % str(counter)))
        sup = supervisor(tmp_path)
        sup.poll_s = 1
        sup.retry_hook = lambda: False
        sup.run(argv, 30, 0, 0, retries=3, backoff_s=0)
        assert len(counter.read_text(encoding="utf-8")) == 1
        assert "retry precondition failed" in capsys.readouterr().err

    def test_a_launch_that_cannot_start_is_127(self, tmp_path):
        sup = supervisor(tmp_path)
        sup.poll_s = 1
        assert sup.run(["/definitely/not/a/binary"], 20, 0, 0, 0, 0) == 127


class TestUsageAccountingNeverBreaksARun:
    def test_an_adapter_that_raises_while_reading_usage_is_swallowed(self, tmp_path):
        class Exploding(Fake):
            def usage(self, log_path):
                raise RuntimeError("bad parser")
        sup = supervisor(tmp_path, Exploding())
        sup.poll_s = 1
        assert sup.run([sys.executable, "-c", "print('ok')"], 20, 0, 0, 0, 0) == 0
        assert sup.usage is None

    def test_a_reported_cost_reaches_the_status_file(self, tmp_path):
        class Reporting(Fake):
            def usage(self, log_path):
                return {"cost_usd": 1.25, "input_tokens": 5, "output_tokens": 7,
                        "model_billed": "some-model"}
        sup = supervisor(tmp_path, Reporting())
        sup.poll_s = 1
        sup.run([sys.executable, "-c", "print('ok')"], 20, 0, 0, 0, 0)
        assert sup.usage["cost_usd"] == 1.25
        assert sup.status.usage["model_billed"] == "some-model"

    def test_a_status_path_of_none_is_tolerated(self, tmp_path):
        """Not every caller wants a status file; writing must not require one."""
        log = tmp_path / "x.log"
        log.write_text("", encoding="utf-8")
        sup = Supervisor(Fake(), "review", str(log), None)
        sup.poll_s = 1
        assert sup.run([sys.executable, "-c", "print('ok')"], 20, 0, 0, 0, 0) == 0
