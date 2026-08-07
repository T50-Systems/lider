"""The two wrappers, end to end: agent-exec.py and agent-implement.py.

Both had zero coverage while being the entry point for every engine call the
plugin makes. They are exercised here through `main()` with a fake engine, which
is the same path a caller takes minus the `sys.exit` wrapper.

The engine is always a small Python script. No real engine is ever invoked.
"""
import json
import os
import subprocess
import sys

import pytest

from conftest import FINDINGS_SCHEMA, read_json

OK, REFUSED, UNDETERMINED, NO_OUTPUT, NOT_FOUND = 0, 1, 2, 3, 127


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """Point the generic adapter at a scripted fake engine."""
    def build(body, **env):
        script = tmp_path / "engine.py"
        script.write_text("import sys, time\n" + body, encoding="utf-8")
        monkeypatch.setenv("LIDER_ENGINE", "generic")
        monkeypatch.setenv("LIDER_BIN", sys.executable)
        monkeypatch.setenv("LIDER_ARGS_REVIEW", str(script))
        monkeypatch.setenv("LIDER_ARGS_IMPLEMENT", str(script))
        monkeypatch.setenv("LIDER_EXTRACT_JSON", "1")
        monkeypatch.setenv("LIDER_RETRIES", "0")
        monkeypatch.setenv("LIDER_SCHEMA", FINDINGS_SCHEMA)
        monkeypatch.setenv("LIDER_METRICS_DIR", str(tmp_path))
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        return script
    return build


GOOD = ("print('{\"engine\":\"fake\",\"verdict\":\"approve\",\"findings\":[]}')\n")
BAD_SHAPE = ("print('{\"engine\":\"fake\",\"verdict\":\"looks_good\",\"findings\":[]}')\n")
NO_JSON = ("print('I only speak prose today')\n")


class TestAgentExec:
    def test_a_clean_review_returns_a_validated_document(self, cli, engine, tmp_path):
        engine(GOOD)
        out, log = tmp_path / "out.json", tmp_path / "run.log"
        assert cli("agent-exec.py", 30, out, log, "review this").returncode == OK
        assert read_json(out)["verdict"] == "approve"

    def test_output_that_does_not_conform_is_3_not_0(self, cli, engine, tmp_path):
        """The engine "succeeded" - its answer is still unusable."""
        engine(BAD_SHAPE)
        proc = cli("agent-exec.py", 30, tmp_path / "o.json", tmp_path / "r.log", "x")
        assert proc.returncode == NO_OUTPUT
        assert "does not conform" in proc.stderr

    def test_an_engine_that_answers_prose_is_3(self, cli, engine, tmp_path):
        engine(NO_JSON)
        proc = cli("agent-exec.py", 30, tmp_path / "o.json", tmp_path / "r.log", "x")
        assert proc.returncode == NO_OUTPUT

    def test_a_missing_engine_is_127(self, cli, engine, tmp_path, monkeypatch):
        engine(GOOD)
        monkeypatch.setenv("LIDER_BIN", "/definitely/not/here")
        proc = cli("agent-exec.py", 30, tmp_path / "o.json", tmp_path / "r.log", "x")
        assert proc.returncode == NOT_FOUND

    def test_a_missing_schema_is_a_usage_error(self, cli, engine, tmp_path, monkeypatch):
        engine(GOOD)
        monkeypatch.setenv("LIDER_SCHEMA", str(tmp_path / "nope.json"))
        proc = cli("agent-exec.py", 30, tmp_path / "o.json", tmp_path / "r.log", "x")
        assert proc.returncode == 2
        assert "schema not found" in proc.stderr

    def test_the_engine_exit_code_is_propagated(self, cli, engine, tmp_path):
        engine("import sys; sys.exit(42)\n")
        proc = cli("agent-exec.py", 30, tmp_path / "o.json", tmp_path / "r.log", "x")
        assert proc.returncode == 42

    def test_a_hang_is_124_and_says_so(self, cli, engine, tmp_path):
        engine("time.sleep(120)\n")
        proc = cli("agent-exec.py", 5, tmp_path / "o.json", tmp_path / "r.log", "x")
        assert proc.returncode == 124
        assert "timeout" in proc.stderr

    def test_an_unknown_engine_falls_back_to_generic(self, cli, engine, tmp_path, monkeypatch):
        engine(GOOD)
        proc = cli("agent-exec.py", "--engine", "no-such-engine", 30,
                   tmp_path / "o.json", tmp_path / "r.log", "x")
        assert proc.returncode == OK

    def test_a_refusing_adapter_is_a_usage_error(self, cli, tmp_path, monkeypatch):
        monkeypatch.setenv("LIDER_SCHEMA", FINDINGS_SCHEMA)
        monkeypatch.setenv("LIDER_CALVOPROXY_ASK", sys.executable)
        proc = cli("agent-implement.py", "--engine", "calvoproxy", 10,
                   tmp_path / "r.log", tmp_path / "done", "", "x")
        assert proc.returncode == 2

    def test_bad_usage_is_reported_rather_than_guessed(self, cli, engine, tmp_path):
        engine(GOOD)
        assert cli("agent-exec.py", 30).returncode == 2
        assert cli("agent-exec.py", "notanumber", "a", "b", "c").returncode == 2
        assert cli("agent-exec.py", "--model", 30, "a", "b", "c").returncode == 2

    def test_the_status_file_narrates_the_run(self, cli, engine, tmp_path):
        engine(GOOD)
        log = tmp_path / "run.log"
        cli("agent-exec.py", 30, tmp_path / "o.json", log, "x")
        status = read_json(log.parent / (log.name + ".status.json"))
        assert status["engine"] == "generic" and status["state"] == "done"
        assert "stall_watchdog" in status and "startup_watchdog" in status

    def test_every_run_is_recorded_even_when_it_failed(self, cli, engine, tmp_path):
        """A run that timed out is a data point about the timeout, not a row to omit."""
        engine("time.sleep(120)\n")
        cli("agent-exec.py", 5, tmp_path / "o.json", tmp_path / "r.log", "x")
        rows = [json.loads(x) for x in
                (tmp_path / ".lider" / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows and rows[-1]["exit"] == 124


class TestAgentImplement:
    def _repo(self, tmp_path, dirty=False):
        """A real git repo, because the checkpoint logic reads real git."""
        subprocess.run(["git", "init", "-q"], cwd=tmp_path)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
        (tmp_path / "f.txt").write_text("one", encoding="utf-8")
        # The test writes its engine, logs and markers into this same directory;
        # without this they show up as untracked and the checkpoint reads the tree
        # as dirty for a reason that has nothing to do with the code under test.
        (tmp_path / ".gitignore").write_text("*\n!f.txt\n!.gitignore\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path)
        if dirty:
            (tmp_path / "f.txt").write_text("two", encoding="utf-8")
        return tmp_path

    def test_a_clean_run_writes_its_exit_code_to_the_done_marker(self, cli, engine, tmp_path):
        self._repo(tmp_path)
        engine("print('ok')\n")
        done = tmp_path / "done"
        assert cli("agent-implement.py", 30, tmp_path / "r.log", done, "", "build it").returncode == OK
        assert done.read_text(encoding="utf-8").strip() == "0"

    def test_the_done_marker_is_written_on_failure_too(self, cli, engine, tmp_path):
        """A watcher that never sees <done> cannot tell failure from still-running."""
        self._repo(tmp_path)
        engine("import sys; sys.exit(7)\n")
        done = tmp_path / "done"
        assert cli("agent-implement.py", 30, tmp_path / "r.log", done, "", "x").returncode == 7
        assert done.read_text(encoding="utf-8").strip() == "7"

    def test_a_clean_tree_arms_the_retry_checkpoint(self, cli, engine, tmp_path):
        self._repo(tmp_path)
        engine("print('ok')\n")
        log = tmp_path / "r.log"
        cli("agent-implement.py", 30, log, tmp_path / "done", "", "x")
        assert "auto-retry: enabled" in log.read_text(encoding="utf-8")

    def test_a_dirty_tree_disables_auto_retry(self, cli, engine, tmp_path):
        """Re-running over a half-written tree is unsafe, so it is not attempted."""
        self._repo(tmp_path, dirty=True)
        engine("print('ok')\n")
        log = tmp_path / "r.log"
        cli("agent-implement.py", 30, log, tmp_path / "done", "", "x")
        assert "auto-retry: disabled" in log.read_text(encoding="utf-8")
        assert "not verifiably clean" in log.read_text(encoding="utf-8")

    def test_outside_a_repo_auto_retry_is_also_disabled(self, cli, engine, tmp_path):
        engine("print('ok')\n")
        log = tmp_path / "r.log"
        cli("agent-implement.py", 30, log, tmp_path / "done", "", "x")
        assert "not a git repo" in log.read_text(encoding="utf-8")

    def test_a_missing_engine_is_127_and_still_marks_done(self, cli, engine, tmp_path,
                                                          monkeypatch):
        engine("print('ok')\n")
        monkeypatch.setenv("LIDER_BIN", "/definitely/not/here")
        done = tmp_path / "done"
        assert cli("agent-implement.py", 30, tmp_path / "r.log", done, "",
                   "x").returncode == NOT_FOUND
        assert done.read_text(encoding="utf-8").strip() == "127"

    def test_bad_usage_is_refused(self, cli, engine, tmp_path):
        engine("print('ok')\n")
        assert cli("agent-implement.py", 30, "log").returncode == 2
        assert cli("agent-implement.py", "notanumber", "l", "d", "m", "p").returncode == 2
        assert cli("agent-implement.py", "--bogus", "x", 30, "l", "d", "m", "p").returncode == 2

    def test_the_log_header_records_what_was_launched(self, cli, engine, tmp_path):
        self._repo(tmp_path)
        engine("print('ok')\n")
        log = tmp_path / "r.log"
        cli("agent-implement.py", 30, log, tmp_path / "done", "", "x")
        header = log.read_text(encoding="utf-8")
        assert "mode: implement" in header and "WRITE ACCESS" in header
        assert "workdir:" in header


class TestTheDataShims:
    """Five-line CLIs over tested library code - cheap to cover, cheap to break."""

    def test_extract_json_shim(self, cli, tmp_path):
        log = tmp_path / "a.log"
        log.write_text('{"engine":"x","verdict":"approve","findings":[]}', encoding="utf-8")
        out = tmp_path / "out.json"
        assert cli("extract-json.py", log, out).returncode == 0
        assert read_json(out)["verdict"] == "approve"

    def test_extract_json_shim_reports_nothing_found_as_3(self, cli, tmp_path):
        log = tmp_path / "a.log"
        log.write_text("no json here", encoding="utf-8")
        assert cli("extract-json.py", log, tmp_path / "o.json").returncode == 3

    def test_extract_json_shim_rejects_bad_usage(self, cli):
        assert cli("extract-json.py").returncode == 2

    def test_validate_json_shim(self, cli, tmp_path):
        doc = tmp_path / "d.json"
        doc.write_text('{"engine":"x","verdict":"approve","findings":[]}', encoding="utf-8")
        assert cli("validate-json.py", FINDINGS_SCHEMA, doc).returncode == 0

    def test_validate_json_shim_reports_a_violation_as_1(self, cli, tmp_path):
        doc = tmp_path / "d.json"
        doc.write_text('{"engine":"x","verdict":"nope","findings":[]}', encoding="utf-8")
        assert cli("validate-json.py", FINDINGS_SCHEMA, doc).returncode == 1

    def test_validate_json_shim_rejects_bad_usage(self, cli):
        assert cli("validate-json.py").returncode == 2
