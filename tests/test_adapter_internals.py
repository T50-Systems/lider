"""Adapter internals: discovery, isolation, stream grammars, error vocabulary.

The argv shapes are covered in test_adapters.py. This covers everything an
adapter does *around* building a command line - the parts that only ran when a
real engine ran, and so had never been exercised by a test.
"""
import json
import os
import sys

import pytest

from lider.adapters.calvoproxy import CalvoProxyAdapter
from lider.adapters.claude import ClaudeAdapter
from lider.adapters.codex import CodexAdapter
from lider.adapters.generic import GenericAdapter
from lider.adapters.grok import GrokAdapter
from lider.validate import check, type_ok, validate_file

from conftest import FINDINGS_SCHEMA


class TestDiscovery:
    def test_generic_needs_lider_bin(self, monkeypatch):
        monkeypatch.delenv("LIDER_BIN", raising=False)
        assert GenericAdapter().locate() is False

    def test_generic_accepts_an_absolute_path(self, monkeypatch):
        monkeypatch.setenv("LIDER_BIN", sys.executable)
        adapter = GenericAdapter()
        assert adapter.locate() and adapter.bin == sys.executable

    def test_generic_falls_back_to_a_path_lookup(self, monkeypatch):
        monkeypatch.setenv("LIDER_BIN", os.path.basename(sys.executable))
        adapter = GenericAdapter()
        assert adapter.locate() is (adapter.bin is not None)

    def test_grok_looks_in_its_own_install_dir_when_not_on_path(self, tmp_path, monkeypatch):
        """MEASURED: grok is routinely absent from PATH in non-interactive shells."""
        home = tmp_path / "home"
        (home / ".grok" / "bin").mkdir(parents=True)
        binary = home / ".grok" / "bin" / "grok"
        binary.write_text("", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        adapter = GrokAdapter()
        assert adapter.locate() and adapter.bin == str(binary)

    def test_grok_reports_absence_rather_than_guessing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "nothing"))
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        assert GrokAdapter().locate() is False

    def test_codex_extends_path_to_the_npm_global_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        assert CodexAdapter().locate() is False   # nothing there, and it says so

    def test_calvoproxy_points_at_the_skill_helper_by_default(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        ask = home / ".claude" / "skills" / "invoke-calvoproxy" / "ask.sh"
        ask.parent.mkdir(parents=True)
        ask.write_text("", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("LIDER_CALVOPROXY_ASK", raising=False)
        adapter = CalvoProxyAdapter()
        assert adapter.locate() and adapter.bin == str(ask)

    def test_calvoproxy_honours_an_explicit_override(self, tmp_path, monkeypatch):
        override = tmp_path / "ask.sh"
        override.write_text("", encoding="utf-8")
        monkeypatch.setenv("LIDER_CALVOPROXY_ASK", str(override))
        adapter = CalvoProxyAdapter()
        assert adapter.locate() and adapter.bin == str(override)


class TestCodexIsolation:
    """A run must not inherit the user's personal install - plugins, hooks, a
    multi-GB log DB - on every invocation."""

    def _home(self, tmp_path, config="model = \"x\"\nnotify = [\"noisy\"]\n"):
        real = tmp_path / "realhome" / ".codex"
        real.mkdir(parents=True)
        (real / "auth.json").write_text('{"token": "secret"}', encoding="utf-8")
        (real / "config.toml").write_text(config, encoding="utf-8")
        return real

    def test_credentials_are_carried_and_noise_is_not(self, tmp_path, monkeypatch):
        real = self._home(tmp_path)
        monkeypatch.setenv("CODEX_HOME", str(real))
        adapter = CodexAdapter()
        adapter.isolate(str(tmp_path))
        iso = os.environ["CODEX_HOME"]
        assert iso != str(real)
        assert json.loads(open(os.path.join(iso, "auth.json")).read())["token"] == "secret"
        config = open(os.path.join(iso, "config.toml")).read()
        assert 'model = "x"' in config            # scalars carried over
        assert "notify" not in config             # everything else dropped
        assert 'approval_policy = "never"' in config

    def test_a_missing_config_is_survivable(self, tmp_path, monkeypatch):
        real = tmp_path / "realhome" / ".codex"
        real.mkdir(parents=True)
        monkeypatch.setenv("CODEX_HOME", str(real))
        adapter = CodexAdapter()
        adapter.isolate(str(tmp_path))
        assert 'approval_policy = "never"' in open(
            os.path.join(os.environ["CODEX_HOME"], "config.toml")).read()

    def test_preflight_warns_but_does_not_block_on_missing_credentials(self, tmp_path,
                                                                       monkeypatch, capsys):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty"))
        assert CodexAdapter().preflight(FINDINGS_SCHEMA) == 0
        assert "no auth.json" in capsys.readouterr().err


class TestStreamGrammars:
    """Verified against captured transcripts; asserted here so they cannot drift."""

    def test_codex_reads_its_own_markers(self):
        adapter = CodexAdapter()
        assert adapter.activity("exec\nnpm run build\n") == "exec: npm run build"
        assert adapter.activity("+++ b/src/auth.ts\n") == "edit: src/auth.ts"
        assert adapter.activity("apply patch\n") == "edit: applying patch"
        assert "finalizing" in adapter.activity("tokens used\n")
        assert adapter.activity("codex\nlooking at the diff\n") == "say: looking at the diff"

    def test_codex_inflight_opens_on_exec_and_closes_on_completion(self):
        adapter = CodexAdapter()
        assert adapter.inflight("exec\nnpm test\n") is True
        assert adapter.inflight("bash -lc npm test succeeded in 900ms\n") is False
        assert adapter.inflight("something unrelated\n") is None

    def test_claude_reads_stream_json_events(self):
        adapter = ClaudeAdapter()
        assert adapter.activity('{"type":"tool_use","name":"Bash"}') == "tool: Bash"
        assert adapter.activity('{"type":"tool_result"}') == "tool done"
        assert adapter.activity('{"type":"result"}') == "finalizing"

    def test_claude_inflight_tracks_the_tool_call(self):
        adapter = ClaudeAdapter()
        assert adapter.inflight('{"type":"tool_use","name":"Bash"}') is True
        assert adapter.inflight('{"type":"tool_result"}') is False
        assert adapter.inflight('{"type":"system"}') is None

    def test_an_adapter_with_no_grammar_says_nothing_rather_than_guessing(self):
        assert GenericAdapter().inflight("anything at all\n") is None


class TestErrorVocabulary:
    @pytest.mark.parametrize("adapter,tail,expected", [
        (CodexAdapter(), "please run codex login", "auth"),
        (CodexAdapter(), "you've hit your usage limit", "fatal"),
        (CodexAdapter(), "some other failure", ""),
        (ClaudeAdapter(), "run claude login", "auth"),
        (ClaudeAdapter(), "overloaded_error", "retry"),
        (GrokAdapter(), "run grok login", "auth"),
        (GrokAdapter(), "unrelated", ""),
        (CalvoProxyAdapter(), "connection refused", "retry"),
        (CalvoProxyAdapter(), "unrelated", ""),
    ])
    def test_each_engine_names_its_own_failures(self, adapter, tail, expected, capsys):
        assert adapter.classify_tail(tail) == expected
        capsys.readouterr()

    def test_every_adapter_offers_a_remediation(self):
        for adapter in (CodexAdapter(), ClaudeAdapter(), GrokAdapter(),
                        CalvoProxyAdapter(), GenericAdapter()):
            assert adapter.auth_hint()


class TestUsageEdges:
    def test_an_unreadable_log_is_none_not_a_crash(self, tmp_path):
        missing = str(tmp_path / "nope.log")
        assert ClaudeAdapter().usage(missing) is None
        assert GrokAdapter().usage(missing) is None

    def test_a_log_with_no_result_event_is_none(self, tmp_path):
        log = tmp_path / "c.log"
        log.write_text('{"type":"system"}\n{"type":"assistant"}\n', encoding="utf-8")
        assert ClaudeAdapter().usage(str(log)) is None

    def test_the_last_result_wins(self, tmp_path):
        log = tmp_path / "c.log"
        log.write_text(
            json.dumps({"type": "result", "total_cost_usd": 1.0}) + "\n" +
            json.dumps({"type": "result", "total_cost_usd": 2.0}) + "\n", encoding="utf-8")
        assert ClaudeAdapter().usage(str(log))["cost_usd"] == 2.0


class TestValidatorFallback:
    """The built-in checker, used when jsonschema is not installed.

    The plugin must work with nothing installed but Python, so this path is not a
    curiosity - it is the one most users would hit.
    """

    def test_type_checking_treats_bools_and_numbers_as_distinct(self):
        assert type_ok(1, "integer") and type_ok(1.5, "number")
        assert not type_ok(True, "integer"), "a bool is not an integer here"
        assert type_ok("x", "string") and type_ok(None, "null")
        assert type_ok(1, ["string", "integer"])

    def test_an_unknown_type_keyword_does_not_invent_a_failure(self):
        assert type_ok("anything", "some-future-type")

    def test_required_properties_are_enforced(self):
        errors = []
        check({"a": 1}, {"type": "object", "required": ["a", "b"]}, "", errors)
        assert any("missing required property 'b'" in e for e in errors)

    def test_additional_properties_false_is_enforced(self):
        errors = []
        check({"a": 1, "surprise": 2},
              {"type": "object", "properties": {"a": {}}, "additionalProperties": False},
              "", errors)
        assert any("unexpected property 'surprise'" in e for e in errors)

    def test_enums_are_enforced(self):
        errors = []
        check("maybe", {"enum": ["yes", "no"]}, "", errors)
        assert any("not one of" in e for e in errors)

    def test_arrays_are_checked_item_by_item_with_a_path(self):
        errors = []
        check([1, "two"], {"type": "array", "items": {"type": "integer"}}, "", errors)
        assert any("[1]" in e for e in errors)

    def test_a_conformant_document_produces_no_errors(self):
        errors = []
        check({"engine": "x", "verdict": "approve", "findings": []},
              json.load(open(FINDINGS_SCHEMA, encoding="utf-8")), "", errors)
        assert errors == []

    def test_unreadable_input_is_2_and_never_1(self, tmp_path):
        """"I could not check" must not be reported as "it failed"."""
        assert validate_file(FINDINGS_SCHEMA, str(tmp_path / "absent.json")) == 2
