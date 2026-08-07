"""The record, and the report that reads it.

This module existed for a while with 231 lines and zero tests - which is the real
maintenance burden, more than the line count. Untested code is not an asset you
own, it is a liability you hope about.

The rule under everything here: **a quantity that could not be measured is `null`,
never `0`.** An unknown cost and a zero cost are opposite facts, and averaging the
second as the first makes an expensive engine look free.
"""
import json

import pytest

from lider import metrics

OK, UNDETERMINED = 0, 2


@pytest.fixture
def store(tmp_path):
    """Write rows into a metrics store and return a reader/reporter pair."""
    def write(*rows):
        for row in rows:
            metrics.record(tmp_path, row.pop("kind"), **row)
        return tmp_path
    write.dir = tmp_path
    return write


@pytest.fixture
def report(cli, store):
    def run(section=None, as_json=True):
        args = ["metrics-report.py", "--dir", store.dir]
        if section:
            args += ["--section", section]
        if as_json:
            args += ["--json"]
        proc = cli(*args)
        if as_json and proc.returncode == OK:
            return json.loads(proc.stdout)
        return proc
    return run


class TestAggNeverTurnsUnknownIntoZero:
    def test_an_unknown_input_is_counted_not_added(self):
        agg = metrics.Agg()
        agg.add(1.0)
        agg.add(None)
        agg.add(2.0)
        assert agg.total == 3.0
        assert agg.n == 2 and agg.unknown == 1

    def test_a_mean_over_nothing_is_none_not_zero(self):
        agg = metrics.Agg()
        agg.add(None)
        assert agg.mean is None
        assert "unknown" in agg.fmt()

    def test_the_format_surfaces_what_it_could_not_add(self):
        agg = metrics.Agg()
        agg.add(2.5)
        agg.add(None)
        text = agg.fmt(" USD")
        assert "2.5" in text and "1 unmeasured" in text

    def test_a_real_zero_is_not_an_unknown(self):
        """The distinction the whole rule exists to protect."""
        agg = metrics.Agg()
        agg.add(0.0)
        assert agg.n == 1 and agg.unknown == 0
        assert "unknown" not in agg.fmt()


class TestTheStore:
    def test_recording_never_raises_on_junk(self, tmp_path):
        """Metrics must not be able to break a run."""
        assert metrics.record(tmp_path, "run", value=object()) is False
        assert metrics.record(tmp_path, "run", value=1) is True

    def test_a_torn_line_costs_one_event_not_the_store(self, tmp_path):
        metrics.record(tmp_path, "run", engine="a")
        with open(metrics.store_path(tmp_path), "a", encoding="utf-8") as fh:
            fh.write('{"kind": "run", "engine": broken\n')
        metrics.record(tmp_path, "run", engine="b")
        engines = [e.get("engine") for e in metrics.read(tmp_path)]
        assert engines == ["a", "b"]

    def test_reading_an_absent_store_is_empty_not_an_error(self, tmp_path):
        assert metrics.read(tmp_path) == []


class TestReportSections:
    def test_no_data_at_all_exits_2(self, report):
        proc = report(as_json=False)
        assert proc.returncode == UNDETERMINED

    def test_routing_reports_cost_and_acceptance_per_engine(self, store, report):
        store({"kind": "run", "engine": "claude", "exit": 0, "cost_usd": 1.0, "elapsed_s": 10},
              {"kind": "run", "engine": "claude", "exit": 1, "cost_usd": 2.0, "elapsed_s": 20},
              {"kind": "run", "engine": "grok", "exit": 0, "cost_usd": 0.5, "elapsed_s": 5})
        rows = {r["engine"]: r for r in report("routing")["routing"]}
        assert rows["claude"]["runs"] == 2 and rows["claude"]["ok_rate"] == "50%"
        assert "3.0" in rows["claude"]["cost"]

    def test_an_engine_that_never_reported_cost_reads_unknown_not_free(self, store, report):
        """Grok's usage parser was blind for a while; every run recorded no cost.

        The aggregate must say so rather than make it look like the cheap option.
        """
        store({"kind": "run", "engine": "mystery", "exit": 0, "cost_usd": None,
               "elapsed_s": 3, "measured": False})
        row = report("routing")["routing"][0]
        assert "unknown" in row["cost"]
        assert "0.0000 USD" not in row["cost"]

    def test_drift_catches_a_model_that_was_not_the_one_asked_for(self, store, report):
        """MEASURED: --model haiku billed claude-sonnet-5 at ~$0.27 for two lines."""
        store({"kind": "run", "engine": "claude", "model": "haiku",
               "model_billed": "claude-sonnet-5", "cost_usd": 0.2751, "exit": 0})
        rows = report("drift")["drift"]
        assert len(rows) == 1
        assert rows[0]["asked"] == "haiku" and rows[0]["billed"] == "claude-sonnet-5"

    def test_drift_stays_silent_when_the_pin_was_honoured(self, store, report):
        store({"kind": "run", "engine": "claude", "model": "opus",
               "model_billed": "claude-opus-5", "exit": 0})
        assert report("drift")["drift"] == []

    def test_lenses_shows_what_each_contributed_that_nobody_else_did(self, store, report):
        """The pruning signal: a lens with sustained zero unique is paying to echo."""
        store({"kind": "lens", "lens": "correctness", "engine": "claude",
               "unique": 2, "shared": 1, "cost_usd": 1.0, "exit": 0},
              {"kind": "lens", "lens": "echo", "engine": "grok",
               "unique": 0, "shared": 1, "cost_usd": 0.5, "exit": 0})
        rows = {r["lens"]: r for r in report("lenses")["lenses"]}
        assert rows["correctness"]["unique"] == 2
        assert rows["echo"]["unique"] == 0

    def test_a_lens_that_died_is_counted_as_failed(self, store, report):
        store({"kind": "lens", "lens": "security", "engine": "grok", "exit": 124})
        assert report("lenses")["lenses"][0]["failed"] == 1

    def test_timing_separates_a_timeout_from_a_watchdog_abort(self, store, report):
        store({"kind": "run", "engine": "a", "exit": 124, "elapsed_s": 300},
              {"kind": "run", "engine": "a", "exit": 125, "elapsed_s": 20},
              {"kind": "run", "engine": "a", "exit": 0, "elapsed_s": 30})
        row = report("timing")["timing"][0]
        assert row["timeouts_124"] == 1 and row["watchdog_125"] == 1
        assert row["max_s"] == 300

    def test_health_counts_undetermined_rounds(self, store, report):
        store({"kind": "run", "engine": "a", "exit": 3, "measured": False},
              {"kind": "round", "coverage": "undetermined"},
              {"kind": "round", "coverage": "complete"})
        row = report("health")["health"][0]
        assert row["schema_failures_3"] == 1
        assert row["rounds_undetermined"] == 1
        assert row["unmeasured_cost"] == 1

    def test_votes_reports_whether_the_extra_ballot_decided_anything(self, store, report):
        store({"kind": "round", "refutation": {"claims": 1, "upheld": 1, "refuted": 0,
                                               "undetermined": 0, "votes_per_claim": 3,
                                               "quorum": 2}})
        row = report("votes")["votes"][0]
        assert row["claims"] == 1 and row["votes"] == 3


class TestParallelismClosesTheSchedulerQuestion:
    """`next` recorded a width on every call and nothing read it - a measurement
    nobody can see is not a measurement. This section is the consumer."""

    def test_a_max_width_of_one_says_a_scheduler_had_nothing_to_do(self, store, report):
        store({"kind": "eligibility", "run": "r", "units": 3, "width": 1},
              {"kind": "eligibility", "run": "r", "units": 3, "width": 1})
        row = report("parallelism")["parallelism"][0]
        assert row["max_width"] == 1 and row["ever_above_1"] == 0
        assert "nothing to schedule" in row["verdict"]

    def test_real_parallelism_is_reported_as_such(self, store, report):
        store({"kind": "eligibility", "run": "r", "units": 4, "width": 1},
              {"kind": "eligibility", "run": "r", "units": 4, "width": 3})
        row = report("parallelism")["parallelism"][0]
        assert row["max_width"] == 3 and row["ever_above_1"] == 1
        assert "real parallelism" in row["verdict"]

    def test_no_observations_yet_reports_nothing_rather_than_a_verdict(self, store, report):
        store({"kind": "run", "engine": "a", "exit": 0})
        assert report("parallelism")["parallelism"] == []


class TestReportTextAndEdges:
    """JSON was covered; the human table path and empty-section edges were not.

    A report nobody can read from a terminal is not a report - and empty sections
    must say so rather than crashing or inventing rows.
    """

    def test_text_mode_renders_every_section_as_a_table(self, store, report):
        store({"kind": "run", "engine": "claude", "exit": 0, "cost_usd": 1.0,
               "elapsed_s": 10, "model": "opus", "model_billed": "claude-opus-5"},
              {"kind": "lens", "lens": "correctness", "engine": "claude",
               "unique": 1, "shared": 0, "cost_usd": 0.5, "exit": 0},
              {"kind": "round", "coverage": "complete",
               "refutation": {"claims": 1, "upheld": 1, "refuted": 0,
                              "undetermined": 0, "votes_per_claim": 3, "quorum": 2}},
              {"kind": "eligibility", "run": "r", "units": 2, "width": 1})
        proc = report(as_json=False)
        assert proc.returncode == OK
        text = proc.stdout
        assert "event(s) from" in text
        for name in ("routing", "lenses", "votes", "timing", "health",
                     "drift", "parallelism"):
            assert "== %s" % name in text
        # Column headers from the routing table land in the text path.
        assert "engine" in text and "ok_rate" in text

    def test_text_mode_for_an_empty_section_says_no_data(self, store, report):
        """drift with nothing mismatched must not invent a row or explode."""
        store({"kind": "run", "engine": "claude", "exit": 0, "model": "opus",
               "model_billed": "claude-opus-5"})
        proc = report("drift", as_json=False)
        assert proc.returncode == OK
        assert "(no data yet)" in proc.stdout

    def test_timing_with_only_non_run_events_is_empty(self, store, report):
        store({"kind": "round", "coverage": "complete"})
        assert report("timing")["timing"] == []

    def test_votes_skips_rounds_that_have_no_refutation_block(self, store, report):
        store({"kind": "round", "coverage": "complete"},
              {"kind": "round", "coverage": "complete",
               "refutation": {"claims": 2, "upheld": 1, "refuted": 1,
                              "undetermined": 0, "votes_per_claim": 3, "quorum": 2}})
        rows = report("votes")["votes"]
        assert len(rows) == 1 and rows[0]["claims"] == 2

    def test_a_single_named_section_does_not_emit_the_others(self, store, report):
        store({"kind": "run", "engine": "a", "exit": 0, "elapsed_s": 1})
        doc = report("health")
        assert list(doc.keys()) == ["health"]
