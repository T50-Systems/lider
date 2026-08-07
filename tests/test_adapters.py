"""The adapter contract, and every measured trap encoded as a regression.

Each assertion here corresponds to a real debugging cycle. If one of these ever
fails, the fix is not to loosen the test.
"""
import json
import os

import pytest

from lider import adapters
from lider.adapters import Adapter, AdapterRefused, DEFAULT_ENGINE
from lider.validate import validate_file

from conftest import FINDINGS_SCHEMA, write_json


def load(engine_id, binary="/fake/bin"):
    adapter = adapters.load(engine_id)
    adapter.bin = binary
    return adapter


class TestRegistry:
    def test_every_shipped_adapter_loads_and_declares_argv(self):
        for engine_id in adapters.available():
            adapter = load(engine_id)
            assert adapter.id == engine_id
            assert type(adapter).argv is not Adapter.argv, "%s must define argv" % engine_id

    def test_an_unknown_id_falls_back_to_generic_rather_than_guessing(self):
        assert adapters.load("no-such-engine-9000").id == "generic"

    def test_an_invalid_id_is_rejected(self):
        with pytest.raises(ValueError):
            adapters.load("../../etc/passwd")

    def test_the_default_engine_is_one_we_can_actually_reach(self):
        """Codex is on PATH but has no account, so it must not be the default."""
        assert DEFAULT_ENGINE != "codex"
        assert DEFAULT_ENGINE in adapters.available()


class TestStreamingDeclarations:
    @pytest.mark.parametrize("engine_id,streams", [
        ("codex", True), ("claude", True),
        ("opencode", True), ("pi", True),
        ("grok", False), ("calvoproxy", False), ("generic", False),
    ])
    def test_each_adapter_declares_whether_its_engine_streams(self, engine_id, streams):
        assert load(engine_id).streams_output() is streams

    def test_streams_defaults_to_the_inflight_answer(self):
        class Silent(Adapter):
            id = "silent"
        class Talkative(Adapter):
            id = "talkative"
            has_inflight = True
        assert Silent().streams_output() is False
        assert Talkative().streams_output() is True


class TestMeasuredTraps:
    def test_claude_passes_the_schema_INLINE_not_as_a_path(self):
        """MEASURED: a filename is parsed as JSON and dies with
        `--json-schema is not valid JSON: Unexpected identifier "C"`."""
        argv = load("claude").argv("review", None, "p", FINDINGS_SCHEMA, "out.json")
        value = argv[argv.index("--json-schema") + 1]
        assert value != FINDINGS_SCHEMA
        assert json.loads(value)["type"] == "object"

    def test_claude_only_asks_for_bare_when_a_key_exists(self, monkeypatch):
        """MEASURED: --bare restricts auth to ANTHROPIC_API_KEY and never reads
        OAuth, so under a normal login it exits 1 with "Not logged in"."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert "--bare" not in load("claude").argv("review", None, "p", FINDINGS_SCHEMA, "o")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert "--bare" in load("claude").argv("review", None, "p", FINDINGS_SCHEMA, "o")

    def test_claude_review_is_locked_down_by_permission_mode(self):
        argv = load("claude").argv("review", None, "p", FINDINGS_SCHEMA, "o")
        assert argv[argv.index("--permission-mode") + 1] == "plan"
        assert "Write" in argv[argv.index("--disallowed-tools") + 1]

    def test_grok_locks_down_with_RULES_because_its_denylist_fails_open(self):
        """VERIFIED: an adversarial prompt with every write tool denylisted still
        overwrote its target. Only --deny rules hold."""
        argv = load("grok").argv("review", None, "p", FINDINGS_SCHEMA, "o")
        assert "--disallowed-tools" not in argv
        assert argv.count("--deny") >= 3
        assert argv[argv.index("--permission-mode") + 1] == "dontAsk"

    def test_grok_never_runs_below_high_effort(self):
        for mode in ("review", "implement"):
            argv = load("grok").argv(mode, None, "p", FINDINGS_SCHEMA, "o")
            assert argv[argv.index("--effort") + 1] == "high"

    def test_grok_read_only_flag_does_not_exist_and_is_not_used(self):
        assert "--read-only" not in load("grok").argv("review", None, "p", FINDINGS_SCHEMA, "o")

    def test_codex_refuses_to_implement_without_an_explicit_model(self):
        """Its CLI default is a model this plugin does not allow."""
        with pytest.raises(AdapterRefused):
            load("codex").argv("implement", None, "p", "", "")
        assert load("codex").argv("implement", "gpt-5.6-terra", "p", "", "")

    def test_calvoproxy_refuses_to_implement_at_all(self):
        """It has no filesystem. A run that quietly did nothing would be worse."""
        with pytest.raises(AdapterRefused):
            load("calvoproxy").argv("implement", None, "p", "", "")

    def test_opencode_implement_uses_auto_and_json_format(self):
        argv = load("opencode").argv("implement", "anthropic/claude-sonnet-4", "do it",
                                     FINDINGS_SCHEMA, "o")
        assert argv[:2] == ["/fake/bin", "run"]
        assert "--format" in argv and argv[argv.index("--format") + 1] == "json"
        assert "--auto" in argv
        assert argv[argv.index("--model") + 1] == "anthropic/claude-sonnet-4"

    def test_opencode_review_does_not_auto_approve_writes(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_PERMISSION", raising=False)
        argv = load("opencode").argv("review", None, "review this", FINDINGS_SCHEMA, "o")
        assert "--auto" not in argv
        assert "OPENCODE_PERMISSION" in __import__("os").environ

    def test_pi_review_is_read_only_tools_only(self):
        argv = load("pi").argv("review", "anthropic/claude-sonnet-4", "review",
                               FINDINGS_SCHEMA, "o")
        assert "-p" in argv and "--mode" in argv and argv[argv.index("--mode") + 1] == "json"
        assert argv[argv.index("--tools") + 1] == "read,grep,find,ls"
        assert argv[-1] == "review"

    def test_pi_implement_does_not_strip_write_tools(self):
        argv = load("pi").argv("implement", None, "build", "", "")
        assert "--tools" not in argv
        assert argv[-1] == "build"

    def test_an_unknown_mode_is_refused_by_every_adapter(self):
        for engine_id in adapters.available():
            with pytest.raises((AdapterRefused, KeyError, ValueError, NotImplementedError)):
                load(engine_id).argv("teleport", None, "p", FINDINGS_SCHEMA, "o")


class TestSandboxes:
    def test_review_is_read_only_and_implement_is_not(self):
        review = load("codex").argv("review", "m", "p", FINDINGS_SCHEMA, "o")
        implement = load("codex").argv("implement", "m", "p", "", "")
        assert review[review.index("--sandbox") + 1] == "read-only"
        assert implement[implement.index("--sandbox") + 1] == "danger-full-access"


class TestUsageAccounting:
    def test_claude_usage_reports_the_model_that_was_BILLED(self, tmp_path):
        """MEASURED: a run launched with --model haiku billed claude-sonnet-5.

        Only this field ever revealed it - the session's own init event said haiku.
        """
        log = tmp_path / "c.log"
        log.write_text(json.dumps({
            "type": "result", "total_cost_usd": 0.2751, "num_turns": 4,
            "usage": {"input_tokens": 6, "output_tokens": 1182,
                      "cache_read_input_tokens": 76111, "cache_creation_input_tokens": 39094},
            "modelUsage": {"claude-sonnet-5": {"costUSD": 0.2751}},
        }), encoding="utf-8")
        used = load("claude").usage(str(log))
        assert used["cost_usd"] == 0.2751
        assert used["model_billed"] == "claude-sonnet-5"
        assert used["output_tokens"] == 1182

    def test_an_absent_cost_stays_None_and_never_becomes_zero(self, tmp_path):
        """An unknown cost and a zero cost are opposite facts."""
        log = tmp_path / "g.log"
        log.write_text(json.dumps({"usage": {"input_tokens": 10, "output_tokens": 2}}),
                       encoding="utf-8")
        used = load("grok").usage(str(log))
        assert used is not None
        assert used["cost_usd"] is None

    def test_an_engine_with_no_usage_report_returns_None(self, tmp_path):
        log = tmp_path / "x.log"
        log.write_text("no json here", encoding="utf-8")
        assert load("grok").usage(str(log)) is None
        assert load("codex").usage(str(log)) is None      # deliberately unimplemented


class TestSchemaValidation:
    def test_a_conformant_document_passes(self, tmp_path):
        doc = write_json(tmp_path / "ok.json", {
            "engine": "x", "verdict": "approve", "findings": []})
        assert validate_file(FINDINGS_SCHEMA, str(doc)) == 0

    def test_a_bad_enum_fails(self, tmp_path):
        doc = write_json(tmp_path / "bad.json", {
            "engine": "x", "verdict": "looks_good", "findings": []})
        assert validate_file(FINDINGS_SCHEMA, str(doc)) == 1

    def test_an_extra_property_fails(self, tmp_path):
        doc = write_json(tmp_path / "extra.json", {
            "engine": "x", "verdict": "approve", "findings": [], "surprise": 1})
        assert validate_file(FINDINGS_SCHEMA, str(doc)) == 1

    def test_a_missing_required_field_fails(self, tmp_path):
        doc = write_json(tmp_path / "thin.json", {"engine": "x", "findings": []})
        assert validate_file(FINDINGS_SCHEMA, str(doc)) == 1

    def test_unreadable_input_is_2_not_1(self, tmp_path):
        """"I could not check" must not be reported as "it failed"."""
        assert validate_file(FINDINGS_SCHEMA, str(tmp_path / "nope.json")) == 2
