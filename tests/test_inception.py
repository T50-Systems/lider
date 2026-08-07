"""The checkable half of Inception, and the three defects found while planning it.

A blind panel of Fable and Opus (Grok self-cancelled twice) converged on one
filter: **a rule is worth building only if the machine can check it against a fact
the model did not just assert.** These tests hold what survived that filter - and
one of them, deliberately, holds the honest LIMIT of what survived.
"""
import json
import time

import pytest

SPEC = "## Scope\nx\n## Hard constraints\ny\n## Mandatory verification\nnpm test\n"
OK, REFUSED, UNDETERMINED, USAGE = 0, 1, 2, 3


@pytest.fixture
def led(cli, tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")

    def run(*args):
        return cli("rungraph.py", "--dir", tmp_path, "--run", "i", *args)

    run("init", "--title", "phase")
    run("spec", "--file", spec)
    run.tmp = tmp_path
    run.spec = spec
    return run


def state_of(led):
    return json.loads((led.tmp / ".lider" / "runs" / "i" / "run.json")
                      .read_text(encoding="utf-8"))


class TestGateIsATrueDryRun:
    def test_gate_does_not_write_at_all(self, led):
        """It used to snapshot, let `enter` commit, then restore - destroying any
        write that landed in between and bumping `updated_at` on a *query*."""
        led("enter", "spec")
        before = (led.tmp / ".lider" / "runs" / "i" / "run.json").read_bytes()
        led("gate", "implement")
        led("gate", "commit")
        assert (led.tmp / ".lider" / "runs" / "i" / "run.json").read_bytes() == before

    def test_gate_still_reports_the_same_verdict_as_enter(self, led):
        led("enter", "spec")
        assert led("gate", "commit").returncode == REFUSED
        assert led("gate", "implement").returncode == OK
        assert led("enter", "implement").returncode == OK

    def test_a_write_between_load_and_return_is_not_clobbered(self, led):
        """The concrete harm of a mutating dry run."""
        led("enter", "spec")
        led("gate", "commit")
        led("check", "--name", "survivor", "--verdict", "ok", "--evidence", "kept")
        led("gate", "implement")
        assert "survivor" in state_of(led)["checks"]


class TestUnitForceReplacesInPlace:
    def test_force_leaves_exactly_one_unit(self, led):
        """Appending a shadow unit left find_unit returning the first while
        unfinished_units counted both - the join barrier could never open."""
        led("unit", "add", "--id", "auth", "--title", "first")
        led("unit", "add", "--id", "auth", "--title", "second", "--force")
        units = state_of(led)["units"]
        assert len(units) == 1
        assert units[0]["title"] == "second"

    def test_without_force_a_duplicate_is_still_refused(self, led):
        led("unit", "add", "--id", "auth")
        assert led("unit", "add", "--id", "auth").returncode == REFUSED


class TestSpecDrift:
    """The one new guard that checks a declaration against an EXTERNAL fact."""

    def test_a_modified_spec_refuses_implement(self, led):
        led("enter", "spec")
        led.spec.write_text(SPEC + "\n## Sneaky addition\nship it\n", encoding="utf-8")
        proc = led("enter", "implement")
        assert proc.returncode == REFUSED
        assert "changed since it was pinned" in proc.stderr

    def test_an_unreadable_spec_is_UNDETERMINED_not_refused(self, led):
        """"I could not check" is not "it failed"."""
        led("enter", "spec")
        led.spec.unlink()
        proc = led("enter", "implement")
        assert proc.returncode == UNDETERMINED
        assert "not a pass" in proc.stderr

    def test_repinning_clears_the_drift(self, led):
        led("enter", "spec")
        led.spec.write_text(SPEC + "\n## Addition\nagreed\n", encoding="utf-8")
        led("spec", "--file", led.spec)
        assert led("enter", "implement").returncode == OK

    def test_force_overrides_and_is_recorded(self, led):
        led("enter", "spec")
        led.spec.write_text(SPEC + "\nchanged\n", encoding="utf-8")
        assert led("enter", "implement", "--force").returncode == OK
        assert state_of(led)["events"][-1]["forced"] is True


class TestOpenQuestions:
    def test_an_open_question_is_UNDETERMINED_not_a_failure(self, led):
        """An unanswered input is literally an unestablished one, so it reuses the
        existing exit-code semantics rather than inventing a new meaning."""
        led("enter", "spec")
        led("question", "add", "--text", "which auth provider?")
        proc = led("enter", "implement")
        assert proc.returncode == UNDETERMINED
        assert "q1" in proc.stderr

    def test_answering_unblocks(self, led):
        led("enter", "spec")
        led("question", "add", "--text", "which provider?")
        led("question", "resolve", "--id", "q1", "--status", "answered", "--answer", "clerk")
        assert led("enter", "implement").returncode == OK

    def test_an_assumption_must_be_written_down(self, led):
        """You may proceed on an assumption - but only one that is recorded."""
        led("enter", "spec")
        led("question", "add", "--text", "which provider?")
        proc = led("question", "resolve", "--id", "q1", "--status", "assumed")
        assert proc.returncode == REFUSED
        assert "requires --answer" in proc.stderr

    def test_a_recorded_assumption_unblocks_and_stays_visible(self, led):
        led("enter", "spec")
        led("question", "add", "--text", "which provider?")
        led("question", "resolve", "--id", "q1", "--status", "assumed",
            "--answer", "assuming clerk, per the existing integration")
        assert led("enter", "implement").returncode == OK
        assert "assumed" in led("show").stdout

    def test_a_question_scoped_to_a_unit_blocks_only_that_unit(self, led):
        led("enter", "spec")
        led("unit", "add", "--id", "auth")
        led("unit", "add", "--id", "api")
        led("question", "add", "--text", "which provider?", "--unit", "auth")
        led("enter", "plan")
        assert led("enter", "implement", "--unit", "api").returncode == OK
        assert led("enter", "implement", "--unit", "auth").returncode == UNDETERMINED

    def test_a_run_with_no_questions_is_unaffected(self, led):
        led("enter", "spec")
        assert led("enter", "implement").returncode == OK


class TestCoverage:
    def test_a_required_criterion_covered_by_nothing_refuses_plan(self, led):
        led("enter", "spec")
        led("criterion", "add", "--id", "AC1", "--text", "login works")
        proc = led("enter", "plan")
        assert proc.returncode == REFUSED
        assert "AC1" in proc.stderr

    def test_the_refusal_says_it_checks_the_mapping_not_the_work(self, led):
        """The panel's shared objection, made binding on the implementation.

        Coverage is self-attestation: the orchestrator writes both sides. It still
        catches a requirement dropped by never declaring a unit, but it must never
        read as evidence that anything was implemented.
        """
        led("enter", "spec")
        led("criterion", "add", "--id", "AC1", "--text", "login works")
        assert "MAPPING only" in led("enter", "plan").stderr

    def test_covering_it_unblocks(self, led):
        led("enter", "spec")
        led("criterion", "add", "--id", "AC1", "--text", "login works")
        led("unit", "add", "--id", "auth", "--covers", "AC1")
        assert led("enter", "plan").returncode == OK

    def test_deferring_requires_a_reason_and_then_unblocks(self, led):
        led("enter", "spec")
        led("criterion", "add", "--id", "AC1", "--text", "login works")
        assert led("criterion", "defer", "--id", "AC1").returncode == REFUSED
        assert led("criterion", "defer", "--id", "AC1",
                   "--reason", "moved to the next phase").returncode == OK
        assert led("enter", "plan").returncode == OK

    def test_a_unit_cannot_claim_an_undeclared_criterion(self, led):
        proc = led("unit", "add", "--id", "auth", "--covers", "AC9")
        assert proc.returncode == REFUSED
        assert "undeclared" in proc.stderr

    def test_once_criteria_exist_a_unit_mapping_to_nothing_is_refused(self, led):
        """A unit that covers nothing is unplanned scope."""
        led("criterion", "add", "--id", "AC1", "--text", "login works")
        proc = led("unit", "add", "--id", "stray")
        assert proc.returncode == REFUSED
        assert "covers no acceptance criterion" in proc.stderr

    def test_a_run_that_declares_no_criteria_is_completely_unaffected(self, led):
        led("enter", "spec")
        assert led("unit", "add", "--id", "auth").returncode == OK
        assert led("enter", "plan").returncode == OK


class TestNextIsAdvisoryOnly:
    def test_next_mutates_nothing(self, led):
        led("enter", "spec")
        led("unit", "add", "--id", "auth")
        before = (led.tmp / ".lider" / "runs" / "i" / "run.json").read_bytes()
        led("next")
        led("next", "--json")
        assert (led.tmp / ".lider" / "runs" / "i" / "run.json").read_bytes() == before

    def test_it_reports_eligibility_that_matches_the_dependency_rule(self, led):
        led("enter", "spec")
        led("unit", "add", "--id", "auth")
        led("unit", "add", "--id", "api", "--depends-on", "auth")
        report = json.loads(led("next", "--json").stdout)
        by = {u["id"]: u for u in report["units"]}
        assert by["auth"]["eligible"] is True
        assert by["api"]["eligible"] is False and by["api"]["blocked_by"] == ["auth"]
        assert report["concurrency_width"] == 1

    def test_it_records_the_width_so_a_scheduler_decision_can_rest_on_data(self, led):
        """Its real job: nobody has looked at whether real runs even have units
        eligible concurrently. Decide against measurement, not intuition."""
        led("enter", "spec")
        led("unit", "add", "--id", "a")
        led("unit", "add", "--id", "b")
        led("next")
        rows = [json.loads(x) for x in
                (led.tmp / ".lider" / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]
        widths = [r for r in rows if r.get("kind") == "eligibility"]
        assert widths and widths[-1]["width"] == 2

    def test_a_flat_run_says_so_rather_than_reporting_nothing(self, led):
        led("enter", "spec")
        assert "single flat unit" in led("next").stdout


class TestWhatTheCrossFamilyReviewCaught:
    """Grok reviewed a Claude implementation and found three MAJORs, all real.

    One of them was a bug introduced *by the fix for another bug* - which is the
    argument for reviewer-differs-in-family, demonstrated on this very change.
    """

    def test_an_unpinned_spec_is_UNDETERMINED_not_a_pass(self, cli, tmp_path):
        """The doctrine failed inside the code enforcing the doctrine.

        `spec_drift` returned 'unpinned' and nothing handled it, so it fell through
        to OK: `enter implement` succeeded with no spec at all. Not established,
        rounded down to fine.
        """
        def run(*args):
            return cli("rungraph.py", "--dir", tmp_path, "--run", "n", *args)
        run("init", "--title", "no spec here")
        proc = run("enter", "implement")
        # the edge itself is illegal from init, so drive to spec-less implement
        run("enter", "spec", "--force")
        proc = run("enter", "implement")
        assert proc.returncode == UNDETERMINED
        assert "no spec is pinned" in proc.stderr

    def test_a_units_start_is_gated_by_the_runs_checks(self, led):
        """evaluate_run applied the check gate and evaluate_unit did not, so in the
        decomposed path - THE path for a multi-unit phase - a failing preflight did
        not stop a unit from starting."""
        led("enter", "spec")
        led("unit", "add", "--id", "auth")
        led("enter", "plan")
        led("check", "--name", "deploy-lock", "--verdict", "not-ok", "--evidence", "held")
        assert led("enter", "implement", "--unit", "auth").returncode == REFUSED
        led("check", "--name", "deploy-lock", "--verdict", "undetermined")
        assert led("enter", "implement", "--unit", "auth").returncode == UNDETERMINED
        led("check", "--name", "deploy-lock", "--verdict", "ok", "--evidence", "free")
        assert led("enter", "implement", "--unit", "auth").returncode == OK

    def test_force_redeclaring_a_unit_keeps_its_progress(self, led, tmp_path):
        """The first fix for the duplicate-id deadlock rebuilt the unit, so a
        mid-flight one snapped back to pending and lost its findings."""
        from conftest import findings_doc, write_json
        led("enter", "spec")
        led("unit", "add", "--id", "auth", "--title", "first")
        led("enter", "plan")
        led("enter", "implement", "--unit", "auth")
        led("enter", "review", "--unit", "auth")
        led("findings", "--unit", "auth", "--file",
            write_json(tmp_path / "f.json", findings_doc(("MINOR", "naming", "a.ts:1"))))
        led("unit", "add", "--id", "auth", "--title", "renamed", "--force")
        unit = state_of(led)["units"][0]
        assert unit["title"] == "renamed"        # the declaration was updated
        assert unit["node"] == "review"          # ...and the progress survived
        assert len(unit["findings"]) == 1
        assert len(unit["rounds"]) == 1

    def test_gate_reports_the_UNIT_node_when_asked_about_a_unit(self, led):
        led("enter", "spec")
        led("unit", "add", "--id", "auth")
        led("enter", "plan")
        out = led("gate", "implement", "--unit", "auth").stdout
        assert "[auth] pending -> implement" in out
