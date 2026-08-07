"""The run ledger's guards, exercised through the CLI.

Tested through the CLI on purpose: the exit codes ARE the contract. `0` ok,
`1` refused, `2` undetermined. A caller that reads those wrongly is the failure
this whole design exists to prevent, so the tests read them the way a caller does.
"""
import json

import pytest

from conftest import findings_doc, write_json

SPEC = "## Scope\nx\n## Hard constraints\ny\n## Mandatory verification\nnpm test\n"

OK, REFUSED, UNDETERMINED = 0, 1, 2


@pytest.fixture
def led(cli, tmp_path):
    """A ledger driver bound to a temp repo, plus a started run."""
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")

    def run(*args):
        return cli("rungraph.py", "--dir", tmp_path, "--run", "t", *args)

    # A high cap so the convergence guards are exercised on their own merits;
    # the cap itself gets a dedicated test below.
    run("init", "--title", "test", "--max-rounds", "9")
    run("spec", "--file", spec)
    run.tmp = tmp_path
    run.spec = spec
    return run


class TestEdges:
    def test_a_legal_edge_is_taken(self, led):
        assert led("enter", "spec").returncode == OK

    def test_an_illegal_edge_is_refused_and_names_the_legal_ones(self, led):
        led("enter", "spec")
        proc = led("enter", "commit")
        assert proc.returncode == REFUSED
        assert "not an edge" in proc.stderr
        assert "implement" in proc.stderr        # it says where you MAY go

    def test_commit_cannot_be_reached_without_verification(self, led):
        led("enter", "spec")
        led("assign", "--role", "implementer", "--engine", "claude")
        led("assign", "--role", "reviewer", "--engine", "grok")
        led("enter", "implement")
        led("enter", "review")
        assert led("enter", "commit").returncode == REFUSED

    def test_unknown_node_is_a_usage_error_not_a_refusal(self, led):
        assert led("enter", "nowhere").returncode == 3


class TestThreeOutcomes:
    def test_undetermined_blocks_exactly_like_a_failure(self, led):
        led("enter", "spec")
        led("check", "--name", "lock", "--verdict", "undetermined", "--evidence", "cli died")
        proc = led("enter", "implement")
        assert proc.returncode == UNDETERMINED
        assert "not a pass" in proc.stderr.lower()

    def test_a_failing_check_refuses(self, led):
        led("enter", "spec")
        led("check", "--name", "lock", "--verdict", "not-ok", "--evidence", "held elsewhere")
        assert led("enter", "implement").returncode == REFUSED

    def test_resolving_the_check_unblocks(self, led):
        led("enter", "spec")
        led("check", "--name", "lock", "--verdict", "undetermined")
        led("check", "--name", "lock", "--verdict", "ok", "--evidence", "held by me")
        assert led("enter", "implement").returncode == OK

    def test_check_returns_its_own_verdict_as_an_exit_code(self, led):
        assert led("check", "--name", "a", "--verdict", "ok").returncode == OK
        assert led("check", "--name", "b", "--verdict", "not-ok").returncode == REFUSED
        assert led("check", "--name", "c", "--verdict", "undetermined").returncode == UNDETERMINED


class TestEngineFamilyInvariant:
    def test_same_family_reviewer_is_refused(self, led):
        led("assign", "--role", "implementer", "--engine", "codex", "--model", "gpt-5.6-terra")
        proc = led("assign", "--role", "reviewer", "--engine", "gpt-5.6-sol")
        assert proc.returncode == REFUSED
        assert "openai" in proc.stderr

    def test_unknown_family_is_undetermined_not_different(self, led):
        """Not knowing is not the same as being different."""
        led("assign", "--role", "implementer", "--engine", "codex")
        proc = led("assign", "--role", "reviewer", "--engine", "some-new-engine")
        assert proc.returncode == UNDETERMINED

    def test_a_genuinely_different_family_is_accepted(self, led):
        led("assign", "--role", "implementer", "--engine", "claude", "--model", "opus")
        assert led("assign", "--role", "reviewer", "--engine", "grok").returncode == OK

    def test_force_records_the_override(self, led):
        led("assign", "--role", "implementer", "--engine", "codex")
        assert led("assign", "--role", "reviewer", "--engine", "gpt-5.6-sol",
                   "--force").returncode == OK
        state = json.loads((led.tmp / ".lider" / "runs" / "t" / "run.json")
                           .read_text(encoding="utf-8"))
        assert state["roles"]["reviewer"]["forced"] is True


class TestAdjudicationLoop:
    @staticmethod
    def _to_review(led):
        led("enter", "spec")
        led("assign", "--role", "implementer", "--engine", "claude")
        led("assign", "--role", "reviewer", "--engine", "grok")
        led("enter", "implement")
        led("enter", "review")

    @staticmethod
    def _round(led, tmp_path, name, *items):
        path = write_json(tmp_path / name, findings_doc(*items))
        led("findings", "--file", path)

    def test_verify_is_refused_while_severe_findings_are_undecided(self, led, tmp_path):
        self._to_review(led)
        self._round(led, tmp_path, "r1.json", ("BLOCKER", "boom", "a.ts:1"))
        led("enter", "adjudicate")
        proc = led("enter", "verify")
        assert proc.returncode == REFUSED
        assert "undecided" in proc.stderr

    def test_convergence_by_identity_allows_real_progress(self, led, tmp_path):
        """Two of three resolved is progress, even with a new defect appearing."""
        self._to_review(led)
        self._round(led, tmp_path, "r1.json",
                    ("BLOCKER", "race on the cache map", "cache.ts:40"),
                    ("MAJOR", "missing null check on user", "a.ts:5"),
                    ("MAJOR", "unvalidated input in handler", "b.ts:9"))
        led("enter", "adjudicate")
        for fid in ("r1-1", "r1-2", "r1-3"):
            led("adjudicate", "--finding", fid, "--decision", "return")
        assert led("enter", "implement").returncode == OK
        led("enter", "review")
        self._round(led, tmp_path, "r2.json",
                    ("BLOCKER", "data race on the cache map", "cache.ts:40"),
                    ("MAJOR", "new off-by-one in the retry loop", "c.ts:12"))
        led("enter", "adjudicate")
        for fid in ("r2-1", "r2-2"):
            led("adjudicate", "--finding", fid, "--decision", "return")
        assert led("enter", "implement").returncode == OK

    def test_a_defect_surviving_three_rounds_is_refused_by_name(self, led, tmp_path):
        """The case a counter reads as progress the whole way down."""
        self._to_review(led)
        for i, name in enumerate(("r1.json", "r2.json", "r3.json"), start=1):
            self._round(led, tmp_path, name,
                        ("BLOCKER", "race condition on the shared cache map", "cache.ts:40"))
            led("enter", "adjudicate")
            led("adjudicate", "--finding", "r%d-1" % i, "--decision", "return")
            proc = led("enter", "implement")
            if i < 3:
                led("enter", "review")
        assert proc.returncode == REFUSED
        assert "survived 3 rounds" in proc.stderr

    def test_the_round_cap_refuses_independently(self, cli, tmp_path):
        """The cap and the convergence guards must each stand on their own."""
        spec = tmp_path / "s.md"
        spec.write_text(SPEC, encoding="utf-8")

        def run(*args):
            return cli("rungraph.py", "--dir", tmp_path, "--run", "cap", *args)

        run("init", "--title", "t", "--max-rounds", "2")
        run("spec", "--file", spec)
        run("enter", "spec")
        run("assign", "--role", "implementer", "--engine", "claude")
        run("assign", "--role", "reviewer", "--engine", "grok")
        run("enter", "implement")
        run("enter", "review")
        for i in (1, 2):
            path = write_json(tmp_path / ("c%d.json" % i),
                              findings_doc(("BLOCKER", "issue %d in module %d" % (i, i),
                                            "m%d.ts:%d" % (i, i))))
            run("findings", "--file", path)
            run("enter", "adjudicate")
            run("adjudicate", "--finding", "r%d-1" % i, "--decision", "return")
            proc = run("enter", "implement")
            if i == 1:
                run("enter", "review")
        assert proc.returncode == REFUSED
        assert "round limit reached" in proc.stderr

    def test_escalating_is_always_available(self, led, tmp_path):
        self._to_review(led)
        self._round(led, tmp_path, "r1.json", ("BLOCKER", "boom", "a.ts:1"))
        led("enter", "adjudicate")
        assert led("enter", "escalated").returncode == OK


class TestGateAndResume:
    def test_gate_answers_without_moving(self, led):
        led("enter", "spec")
        before = led("show").stdout
        led("gate", "commit")
        assert led("show").stdout.split("path:")[0] == before.split("path:")[0]

    def test_show_restores_what_a_resumed_session_needs(self, led):
        led("enter", "spec")
        led("assign", "--role", "implementer", "--engine", "claude", "--model", "opus")
        out = led("show").stdout
        assert "node: spec" in out
        assert "implementer" in out and "anthropic" in out
        assert "spec:" in out                      # the pinned spec is recoverable

    def test_the_ledger_survives_a_new_process(self, led):
        led("enter", "spec")
        assert "node: spec" in led("show").stdout   # a separate subprocess each time


def test_a_spec_missing_its_verification_section_is_refused(cli, tmp_path):
    """A spec with no verification cannot produce a checkable outcome later."""
    thin = tmp_path / "thin.md"
    thin.write_text("## Scope\njust do it\n", encoding="utf-8")
    cli("rungraph.py", "--dir", tmp_path, "--run", "u", "init", "--title", "t")
    proc = cli("rungraph.py", "--dir", tmp_path, "--run", "u", "spec", "--file", thin)
    assert proc.returncode == REFUSED
    assert "verification" in proc.stderr


class TestDefectsTheFirstRealPhaseSurfaced:
    def test_the_spec_text_is_stored_not_just_a_path_to_it(self, led):
        """A ledger holding a path is one `git mv` away from pointing at nothing.

        Measured: moving the closed spec into docs/specs/ orphaned the run's only
        record of what had been decided before any code was written.
        """
        state = json.loads((led.tmp / ".lider" / "runs" / "t" / "run.json")
                           .read_text(encoding="utf-8"))
        assert state["spec"]["text"] == SPEC
        led.spec.unlink()                        # the file is gone
        assert SPEC in json.loads((led.tmp / ".lider" / "runs" / "t" / "run.json")
                                  .read_text(encoding="utf-8"))["spec"]["text"]

    def test_two_reviewers_in_ONE_round_are_not_a_recurrence(self, led, tmp_path):
        """Recurrence means "it came back", not "two people saw it at once".

        Deduping inside a round is the reducer's job; counting it here labelled a
        first sighting as a defect that had survived a return.
        """
        led("enter", "spec")
        led("assign", "--role", "implementer", "--engine", "claude")
        led("assign", "--role", "reviewer", "--engine", "grok")
        led("enter", "implement")
        led("enter", "review")
        path = write_json(tmp_path / "r1.json", findings_doc(
            ("BLOCKER", "race on the cache map", "cache.ts:40"),
            ("MAJOR", "data race on the cache map", "cache.ts:40")))
        out = led("findings", "--file", path).stdout
        assert "0 recurring" in out

    def test_a_defect_returning_in_a_LATER_round_is_a_recurrence(self, led, tmp_path):
        led("enter", "spec")
        led("assign", "--role", "implementer", "--engine", "claude")
        led("assign", "--role", "reviewer", "--engine", "grok")
        led("enter", "implement")
        led("enter", "review")
        led("findings", "--file", write_json(tmp_path / "a.json", findings_doc(
            ("BLOCKER", "race on the cache map", "cache.ts:40"))))
        led("enter", "adjudicate")
        led("adjudicate", "--finding", "r1-1", "--decision", "return")
        led("enter", "implement")
        led("enter", "review")
        out = led("findings", "--file", write_json(tmp_path / "b.json", findings_doc(
            ("BLOCKER", "data race on the cache map", "cache.ts:40")))).stdout
        assert "1 recurring" in out
