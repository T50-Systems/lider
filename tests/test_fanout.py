"""The fan-out: many lenses, then many skeptics.

The newest, most complex, and only money-spending part of the plugin - and it had
zero automated coverage. Everything below was previously verified by hand, which
is the same as not being verified once anyone changes it.

No engine is called. A fake one answers from a small Python script, and the
loop-until-dry cases work by having it exhaust a scripted repertoire, which is
exactly how real discovery is supposed to go quiet.
"""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

from conftest import FINDINGS_SCHEMA, SCRIPTS, read_json, write_json


def load_fanout():
    """Import fanout.py despite the dash in its name."""
    spec = importlib.util.spec_from_file_location("fanout", os.path.join(SCRIPTS, "fanout.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fanout = load_fanout()


# --------------------------------------------------------------- pure pieces
class TestLensSpecs:
    def test_a_bare_name_takes_the_default_engine(self):
        assert fanout.parse_lens("security", "claude") == ("security", "claude", None)

    def test_engine_and_model_are_both_optional_and_ordered(self):
        assert fanout.parse_lens("security:grok", "claude") == ("security", "grok", None)
        assert fanout.parse_lens("correctness:claude:opus", "grok") == \
            ("correctness", "claude", "opus")

    def test_an_empty_field_falls_back_rather_than_passing_an_empty_string(self):
        assert fanout.parse_lens("security::opus", "claude") == ("security", "claude", "opus")

    def test_a_lens_name_becomes_a_safe_filename(self):
        assert fanout.slugify("cross-cutting concerns/v2") == "cross-cutting_concerns_v2"


class TestThePrompt:
    def test_a_known_lens_gets_its_specific_question(self):
        prompt = fanout.build_prompt("security", "grok", "the diff", [])
        assert "injection" in prompt and "the diff" in prompt

    def test_an_unknown_lens_still_works_as_free_form(self):
        prompt = fanout.build_prompt("i18n", "grok", "the diff", [])
        assert "lens of: i18n" in prompt

    def test_later_waves_are_told_what_is_already_known(self):
        """Without this a second wave is the first wave again: same lens, same
        code, same findings, all thrown away by the dedupe - and the loop reports
        'nothing new' while never having looked anywhere new."""
        seen = [{"summary": "race on the cache map", "location": "cache.ts:40"}]
        prompt = fanout.build_prompt("correctness", "claude", "the diff", seen)
        assert "ALREADY REPORTED" in prompt
        assert "race on the cache map" in prompt

    def test_the_first_wave_carries_no_such_section(self):
        assert "ALREADY REPORTED" not in fanout.build_prompt("correctness", "c", "d", [])

    def test_a_long_history_is_truncated_and_says_so(self):
        seen = [{"summary": "defect %d" % i, "location": "f%d.ts:1" % i} for i in range(60)]
        prompt = fanout.build_prompt("correctness", "c", "d", seen)
        assert "and 20 more" in prompt


class TestPruningIsNeverSilent:
    def _rows(self, tmp_path, *rows):
        from lider import metrics
        for row in rows:
            metrics.record(tmp_path, "lens", **row)
        return tmp_path

    def test_a_lens_with_a_sustained_zero_is_dropped(self, tmp_path):
        """Needs a survivor: pruning refuses to empty the fan-out entirely, so a
        one-lens run can never demonstrate a drop."""
        self._rows(tmp_path,
                   *[{"lens": "echo", "unique": 0} for _ in range(4)],
                   *[{"lens": "correctness", "unique": 2} for _ in range(4)])
        lenses = [("echo", "claude", None), ("correctness", "claude", None)]
        kept, dropped = fanout.prune_lenses(lenses, tmp_path, 3)
        assert [k[0] for k in kept] == ["correctness"]
        assert [d[0] for d in dropped] == ["echo"]

    def test_too_little_evidence_keeps_the_lens(self, tmp_path):
        """Absence of evidence is not evidence of uselessness."""
        self._rows(tmp_path, {"lens": "security", "unique": 0}, {"lens": "security", "unique": 0})
        lenses = [("security", "grok", None), ("correctness", "claude", None)]
        kept, dropped = fanout.prune_lenses(lenses, tmp_path, 3)
        assert [k[0] for k in kept] == ["security", "correctness"]
        assert dropped == []

    def test_a_contributing_lens_is_never_dropped(self, tmp_path):
        self._rows(tmp_path, *[{"lens": "correctness", "unique": 2} for _ in range(5)])
        kept, dropped = fanout.prune_lenses([("correctness", "claude", None)], tmp_path, 3)
        assert len(kept) == 1 and dropped == []

    def test_pruning_never_empties_the_fan_out(self, tmp_path):
        """A review with no lenses is not a cheap review, it is no review."""
        self._rows(tmp_path, *[{"lens": "only", "unique": 0} for _ in range(5)])
        kept, dropped = fanout.prune_lenses([("only", "claude", None)], tmp_path, 3)
        assert len(kept) == 1 and dropped == []

    def test_with_no_history_nothing_is_pruned(self, tmp_path):
        lenses = [("a", "claude", None), ("b", "grok", None)]
        kept, dropped = fanout.prune_lenses(lenses, tmp_path, 3)
        assert kept == lenses and dropped == []


# --------------------------------------------------------------- end to end
@pytest.fixture
def fake_fanout(cli, tmp_path, monkeypatch):
    """Drive fanout.py's `main()` in process against a scripted fake engine.

    In process, not as a subprocess: `main()` is the boundary, and a subprocess
    would put the whole file out of reach of the coverage measurement while being
    exercised thoroughly. The engine underneath is still a real child process -
    that part IS the thing under test.
    """
    def run(body, *args, **kw):
        script = tmp_path / "engine.py"
        script.write_text("import sys\n" + body, encoding="utf-8")
        monkeypatch.setenv("LIDER_ENGINE", "generic")
        monkeypatch.setenv("LIDER_BIN", sys.executable)
        monkeypatch.setenv("LIDER_ARGS_REVIEW", str(script))
        monkeypatch.setenv("LIDER_EXTRACT_JSON", "1")
        monkeypatch.setenv("LIDER_RETRIES", "0")
        monkeypatch.setenv("LIDER_METRICS_DIR", str(tmp_path))
        monkeypatch.setenv("LIDER_SCHEMA", FINDINGS_SCHEMA)
        out = tmp_path / kw.get("out", "run")
        proc = cli("fanout.py", "--out", out, "--timeout", "40", "--concurrency", "3", *args)
        doc = read_json(out / "round.json") if (out / "round.json").exists() else None
        return proc.returncode, doc, proc.stdout + proc.stderr
    return run


ONE_FINDING = (
    "prompt = sys.argv[-1]\n"
    "print('{\"engine\":\"fake\",\"verdict\":\"request_changes\",\"findings\":"
    "[{\"severity\":\"BLOCKER\",\"summary\":\"race on the cache map\","
    "\"location\":\"cache.ts:40\",\"suggestion\":null}]}')\n")

NOTHING = ("print('{\"engine\":\"fake\",\"verdict\":\"approve\",\"findings\":[]}')\n")

# The engine exhausts a two-item repertoire, which is how real discovery is meant
# to go quiet. The sentinel must NOT be a phrase any built-in lens prompt already
# uses: "off-by-one" appears verbatim in the `correctness` prompt, which silently
# matched the wrong branch on the FIRST wave and made the loop look like it had
# converged instantly. A fake engine keyed on prompt content has to avoid the
# prompt's own vocabulary.
EXHAUSTIBLE = (
    "prompt = sys.argv[-1]\n"
    "if 'ZWEITEDEFEKT' in prompt:\n"
    "    print('{\"engine\":\"fake\",\"verdict\":\"approve\",\"findings\":[]}')\n"
    "elif 'ALREADY REPORTED' in prompt:\n"
    "    print('{\"engine\":\"fake\",\"verdict\":\"request_changes\",\"findings\":"
    "[{\"severity\":\"MAJOR\",\"summary\":\"ZWEITEDEFEKT in the retry loop\","
    "\"location\":\"c.ts:12\",\"suggestion\":null}]}')\n"
    "else:\n"
    "    print('{\"engine\":\"fake\",\"verdict\":\"request_changes\",\"findings\":"
    "[{\"severity\":\"BLOCKER\",\"summary\":\"race on the cache map\","
    "\"location\":\"cache.ts:40\",\"suggestion\":null}]}')\n")


class TestOneWave:
    def test_two_lenses_agreeing_collapse_to_one_corroborated_finding(self, fake_fanout):
        rc, doc, _ = fake_fanout(ONE_FINDING, "review", "--scope", "the diff",
                                 "--lens", "correctness", "--lens", "regression")
        assert rc == 0
        assert len(doc["findings"]) == 1
        assert doc["findings"][0]["corroboration"]["lenses"] == 2

    def test_a_lens_that_dies_makes_the_round_undetermined(self, fake_fanout):
        """Five lenses of which two crashed is not broad coverage - it is three
        lenses and two unanswered questions."""
        rc, doc, out = fake_fanout("import sys; sys.exit(9)\n", "review",
                                   "--scope", "d", "--lens", "correctness")
        assert rc == 2
        assert doc["coverage"] == "undetermined"
        assert doc["missing"]

    def test_a_lens_that_found_nothing_is_not_a_gap(self, fake_fanout):
        rc, doc, _ = fake_fanout(NOTHING, "review", "--scope", "d", "--lens", "security")
        assert rc == 0
        assert doc["coverage"] == "complete" and doc["missing"] == []
        assert doc["reviewers"][0]["lens"] == "security"

    def test_each_lens_narration_is_attributed(self, fake_fanout):
        """A mute fan-out is indistinguishable from a hang; each line says whose."""
        _, _, out = fake_fanout(ONE_FINDING, "review", "--scope", "d",
                                "--lens", "correctness", "--lens", "tests")
        assert "-> correctness" in out and "-> tests" in out


class TestLoopUntilDry:
    def test_it_stops_when_two_consecutive_waves_find_nothing_new(self, fake_fanout):
        rc, doc, out = fake_fanout(EXHAUSTIBLE, "review", "--scope", "d",
                                   "--until-dry", "2", "--max-rounds", "6",
                                   "--lens", "correctness")
        assert rc == 0
        assert "discovery dry" in out
        assert doc["rounds"] >= 3
        assert len(doc["findings"]) == 2      # the two distinct defects, deduped

    def test_a_single_wave_is_still_the_default(self, fake_fanout):
        rc, doc, _ = fake_fanout(ONE_FINDING, "review", "--scope", "d",
                                 "--lens", "correctness")
        assert rc == 0 and doc.get("rounds") in (None, 1)

    def test_hitting_the_cap_while_still_finding_is_reported_as_incomplete(self, fake_fanout):
        """A bounded search reported without its bound reads as an exhaustive one."""
        endless = (
            "import sys, os\n"
            "n = len([f for f in os.listdir('.') if f.startswith('tick')]) + 1\n"
            "open('tick%d' % n, 'w').write('x')\n"
            "print('{\"engine\":\"fake\",\"verdict\":\"request_changes\",\"findings\":"
            "[{\"severity\":\"MAJOR\",\"summary\":\"defect number %d in module %d\","
            "\"location\":\"m%d.ts:%d\",\"suggestion\":null}]}' % (n, n, n, n))\n")
        rc, doc, out = fake_fanout(endless, "review", "--scope", "d",
                                   "--until-dry", "2", "--max-rounds", "2",
                                   "--lens", "correctness")
        assert rc == 2
        assert doc["coverage"] == "undetermined"
        assert any("cap" in m.get("reason", "") for m in doc["missing"])


class TestRefute:
    def test_only_severe_claims_are_put_to_a_vote(self, fake_fanout, tmp_path):
        rnd = write_json(tmp_path / "round.json", {
            "engine": "x", "verdict": "approve_with_nits",
            "findings": [{"severity": "NIT", "summary": "naming", "location": "a:1",
                          "suggestion": None}]})
        rc, _, out = fake_fanout(NOTHING, "refute", "--round", rnd, "--votes", "3")
        assert rc == 0
        assert "no BLOCKER/MAJOR claims" in out

    def test_a_claim_survives_when_the_skeptics_do_not_refute_it(self, fake_fanout, tmp_path):
        rnd = write_json(tmp_path / "round.json", {
            "engine": "x", "verdict": "request_changes",
            "findings": [{"severity": "BLOCKER", "summary": "real defect",
                          "location": "a.ts:1", "suggestion": None}]})
        ballot = ("print('{\"engine\":\"fake\",\"refuted\":false,\"confidence\":\"high\","
                  "\"reason\":\"confirmed by reading the code\",\"evidence\":null}')\n")
        rc, _, _ = fake_fanout(ballot, "refute", "--round", rnd, "--votes", "3")
        verified = read_json(tmp_path / "round.verified.json")
        assert len(verified["findings"]) == 1
        assert verified["findings"][0]["refutation"]["status"] == "upheld"

    def test_a_confident_majority_drops_the_claim(self, fake_fanout, tmp_path):
        rnd = write_json(tmp_path / "round.json", {
            "engine": "x", "verdict": "request_changes",
            "findings": [{"severity": "MAJOR", "summary": "already handled upstream",
                          "location": "a.ts:1", "suggestion": None}]})
        ballot = ("print('{\"engine\":\"fake\",\"refuted\":true,\"confidence\":\"high\","
                  "\"reason\":\"the caller already validates this\",\"evidence\":null}')\n")
        rc, _, _ = fake_fanout(ballot, "refute", "--round", rnd, "--votes", "3")
        verified = read_json(tmp_path / "round.verified.json")
        assert verified["findings"] == []
        assert len(verified["dropped"]) == 1


class TestUsageAndMetrics:
    def test_the_round_records_a_row_per_lens_and_one_for_itself(self, fake_fanout, tmp_path):
        fake_fanout(ONE_FINDING, "review", "--scope", "d",
                    "--lens", "correctness", "--lens", "tests")
        rows = [json.loads(x) for x in
                (tmp_path / ".lider" / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len([r for r in rows if r["kind"] == "lens"]) == 2
        assert len([r for r in rows if r["kind"] == "round"]) == 1

    def test_an_engine_that_reports_no_cost_is_recorded_as_unmeasured(self, fake_fanout, tmp_path):
        fake_fanout(ONE_FINDING, "review", "--scope", "d", "--lens", "correctness")
        rows = [json.loads(x) for x in
                (tmp_path / ".lider" / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]
        lens = [r for r in rows if r["kind"] == "lens"][0]
        assert lens["cost_usd"] is None and lens["measured"] is False

    def test_a_missing_status_file_yields_an_empty_usage_not_a_crash(self, tmp_path):
        assert fanout.read_usage(str(tmp_path / "nope.log")) == {}
