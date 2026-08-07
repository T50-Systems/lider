"""What the ledger SHOWS, and the paths it takes when asked odd questions.

`show` is the first thing a resumed session runs, and `list` is how an operator
sees what is declared. Both were exercised only incidentally: the guards had
tests, the reporting did not. A ledger that guards correctly and reports wrongly
is still a ledger you cannot trust.
"""
import json
import os

import pytest

from conftest import findings_doc, write_json

SPEC = "## Scope\nx\n## Hard constraints\ny\n## Mandatory verification\nnpm test\n"
OK, REFUSED, UNDETERMINED, USAGE = 0, 1, 2, 3


@pytest.fixture
def led(cli, tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")

    def run(*args):
        return cli("rungraph.py", "--dir", tmp_path, "--run", "r", *args)

    run("init", "--title", "reporting phase", "--max-rounds", "9")
    run("spec", "--file", spec)
    run("enter", "spec")          # pinning a spec does not move the node; entering does
    run.tmp = tmp_path
    return run


class TestShow:
    def test_a_fresh_run_reports_its_node_and_where_it_may_go(self, led):
        out = led("show").stdout
        assert "node: spec" in out
        assert "next:" in out and "implement" in out

    def test_json_mode_emits_the_whole_state(self, led):
        state = json.loads(led("show", "--json").stdout)
        assert state["run_id"] == "r" and state["node"] == "spec"
        assert "units" in state and "criteria" in state

    def test_roles_are_shown_with_their_families(self, led):
        led("assign", "--role", "implementer", "--engine", "claude", "--model", "opus")
        led("assign", "--role", "reviewer", "--engine", "grok")
        out = led("show").stdout
        assert "anthropic" in out and "xai" in out

    def test_a_forced_role_is_marked_as_forced(self, led):
        led("assign", "--role", "implementer", "--engine", "codex")
        led("assign", "--role", "reviewer", "--engine", "gpt-5.6-sol", "--force")
        assert "FORCED" in led("show").stdout

    def test_checks_are_shown_with_their_three_states(self, led):
        led("check", "--name", "a", "--verdict", "ok", "--evidence", "held")
        led("check", "--name", "b", "--verdict", "not-ok", "--evidence", "busy")
        led("check", "--name", "c", "--verdict", "undetermined", "--evidence", "cli died")
        out = led("show").stdout
        assert "FAILING: b" in out
        assert "UNDETERMINED (not a pass): c" in out

    def test_rounds_and_open_findings_are_listed(self, led, tmp_path):
        led("assign", "--role", "implementer", "--engine", "claude")
        led("assign", "--role", "reviewer", "--engine", "grok")
        led("enter", "implement")
        led("enter", "review")
        led("findings", "--file", write_json(tmp_path / "f.json", findings_doc(
            ("BLOCKER", "race on the cache map", "cache.ts:40"),
            ("NIT", "naming", "a.ts:1"))))
        out = led("show").stdout
        assert "rounds: 1/9" in out
        assert "OPEN BLOCKER/MAJOR (1)" in out
        assert "race on the cache map" in out

    def test_units_are_shown_with_what_blocks_them(self, led):
        led("unit", "add", "--id", "auth", "--title", "login flow")
        led("unit", "add", "--id", "api", "--depends-on", "auth")
        out = led("show").stdout
        assert "units: 2, 2 still open" in out
        assert "blocked by auth" in out

    def test_a_units_rounds_and_open_findings_appear_in_the_run_view(self, led, tmp_path):
        led("assign", "--role", "implementer", "--engine", "claude")
        led("assign", "--role", "reviewer", "--engine", "grok")
        led("unit", "add", "--id", "auth")
        led("enter", "plan")
        led("enter", "implement", "--unit", "auth")
        led("enter", "review", "--unit", "auth")
        led("findings", "--unit", "auth", "--file", write_json(
            tmp_path / "u.json", findings_doc(("MAJOR", "unvalidated input", "a.ts:3"))))
        out = led("show").stdout
        assert "1 round(s)" in out and "1 open BLOCKER/MAJOR" in out

    def test_questions_and_criteria_surface_with_their_status(self, led):
        led("question", "add", "--text", "which provider?")
        led("question", "resolve", "--id", "q1", "--status", "assumed", "--answer", "clerk")
        led("question", "add", "--text", "still open?")
        led("criterion", "add", "--id", "AC1", "--text", "login works")
        out = led("show").stdout
        assert "questions: 1 open, 1 assumed" in out
        assert "criteria: 1, 1 required and uncovered" in out
        assert "MAPPING check" in out

    def test_a_stuck_defect_is_called_out_by_name(self, led, tmp_path):
        led("assign", "--role", "implementer", "--engine", "claude")
        led("assign", "--role", "reviewer", "--engine", "grok")
        led("enter", "implement")
        for i in (1, 2, 3):
            led("enter", "review")
            led("findings", "--file", write_json(tmp_path / ("r%d.json" % i), findings_doc(
                ("BLOCKER", "race condition on the shared cache", "s.ts:12"))))
            led("enter", "adjudicate")
            led("adjudicate", "--finding", "r%d-1" % i, "--decision", "return")
            led("enter", "implement", "--force")
        out = led("show").stdout
        assert "STUCK" in out and "3 rounds" in out


class TestListings:
    def test_criterion_list_says_who_covers_what(self, led):
        led("criterion", "add", "--id", "AC1", "--text", "login works")
        led("criterion", "add", "--id", "AC2", "--text", "logout works")
        led("unit", "add", "--id", "auth", "--covers", "AC1")
        out = led("criterion", "list").stdout
        assert "auth" in out and "NOT COVERED" in out

    def test_criterion_list_on_an_empty_run(self, led):
        assert "no acceptance criteria declared" in led("criterion", "list").stdout

    def test_a_deferred_criterion_shows_its_status(self, led):
        led("criterion", "add", "--id", "AC1", "--text", "later")
        led("criterion", "defer", "--id", "AC1", "--reason", "next phase")
        assert "deferred" in led("criterion", "list").stdout

    def test_question_list_shows_answers(self, led):
        led("question", "add", "--text", "which provider?")
        led("question", "resolve", "--id", "q1", "--status", "answered", "--answer", "clerk")
        assert "clerk" in led("question", "list").stdout

    def test_question_list_on_an_empty_run(self, led):
        assert "no open questions" in led("question", "list").stdout

    def test_unit_list_on_a_flat_run_says_so(self, led):
        assert "single flat unit" in led("unit", "list").stdout


class TestOddQuestions:
    def test_resolving_a_question_that_does_not_exist(self, led):
        proc = led("question", "resolve", "--id", "q9", "--status", "answered", "--answer", "x")
        assert proc.returncode == USAGE

    def test_adding_a_question_with_no_text(self, led):
        assert led("question", "add").returncode == USAGE

    def test_adding_a_criterion_with_no_id(self, led):
        assert led("criterion", "add", "--text", "x").returncode == USAGE

    def test_deferring_a_criterion_that_does_not_exist(self, led):
        assert led("criterion", "defer", "--id", "AC9", "--reason", "x").returncode == USAGE

    def test_a_duplicate_criterion_is_refused(self, led):
        led("criterion", "add", "--id", "AC1", "--text", "x")
        assert led("criterion", "add", "--id", "AC1", "--text", "y").returncode == REFUSED

    def test_adding_a_unit_with_no_id(self, led):
        assert led("unit", "add").returncode == USAGE

    def test_adjudicating_a_finding_that_does_not_exist(self, led):
        assert led("adjudicate", "--finding", "r9-9", "--decision", "accept").returncode == USAGE

    def test_a_document_with_no_findings_key_means_no_findings(self, led, tmp_path):
        """Not an error: an engine that reports a verdict and nothing else found
        nothing. A malformed one - findings that are not a list - IS an error."""
        empty = write_json(tmp_path / "empty.json", {"engine": "x", "verdict": "approve"})
        assert led("findings", "--file", empty).returncode == OK
        broken = write_json(tmp_path / "broken.json", {"engine": "x", "findings": "nope"})
        assert led("findings", "--file", broken).returncode == USAGE

    def test_ingesting_a_bare_array_is_accepted(self, led, tmp_path):
        """Some engines answer with the list itself rather than the envelope."""
        bare = write_json(tmp_path / "bare.json",
                          [{"severity": "NIT", "summary": "naming", "location": "a:1"}])
        assert led("findings", "--file", bare).returncode == OK

    def test_a_missing_file_is_a_usage_error_not_a_crash(self, led, tmp_path):
        assert led("findings", "--file", tmp_path / "absent.json").returncode == USAGE

    def test_scoping_to_a_unit_that_does_not_exist(self, led, tmp_path):
        good = write_json(tmp_path / "f.json", findings_doc())
        assert led("findings", "--unit", "ghost", "--file", good).returncode == USAGE

    def test_re_initialising_without_force_is_refused(self, led):
        assert led("init", "--title", "again").returncode == REFUSED

    def test_re_initialising_with_force_starts_over(self, led):
        assert led("init", "--title", "again", "--force").returncode == OK
        assert "node: init" in led("show").stdout

    def test_a_command_before_any_run_exists(self, cli, tmp_path):
        proc = cli("rungraph.py", "--dir", tmp_path / "empty", "show")
        assert proc.returncode == USAGE
        assert "no run found" in proc.stderr

    def test_the_most_recently_updated_run_is_the_default(self, cli, tmp_path):
        spec = tmp_path / "s.md"
        spec.write_text(SPEC, encoding="utf-8")
        cli("rungraph.py", "--dir", tmp_path, "--run", "first", "init", "--title", "a")
        cli("rungraph.py", "--dir", tmp_path, "--run", "second", "init", "--title", "b")
        assert "run second" in cli("rungraph.py", "--dir", tmp_path, "show").stdout

    def test_an_invalid_run_id_is_rejected(self, cli, tmp_path):
        proc = cli("rungraph.py", "--dir", tmp_path, "--run", "../escape", "show")
        assert proc.returncode == USAGE


class TestNextReporting:
    def test_a_flat_run_says_it_has_no_units(self, led):
        assert "single flat unit" in led("next").stdout

    def test_units_are_reported_ready_or_blocked(self, led):
        led("unit", "add", "--id", "auth")
        led("unit", "add", "--id", "api", "--depends-on", "auth")
        out = led("next").stdout
        assert "READY" in out and "blocked by auth" in out
        assert "concurrently right now: 1" in out

    def test_json_mode_carries_the_legal_moves(self, led):
        report = json.loads(led("next", "--json").stdout)
        assert report["node"] == "spec"
        assert "implement" in report["legal_moves"]
