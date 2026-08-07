"""Recovering an answer from however an engine chose to wrap it.

Every case here is a real shape a real engine produced. The last one cost a
review that had already been paid for: grok answered correctly and the run
failed with exit 3 because the payload was fenced *inside* prose.
"""
import json

import pytest

from lider.extract import extract_to, parse_maybe, balanced_objects

PAYLOAD = {"engine": "x", "verdict": "approve", "findings": []}


def write(tmp_path, text, name="run.log"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def extract(tmp_path, text):
    log = write(tmp_path, text)
    out = tmp_path / "out.json"
    rc = extract_to(str(log), str(out))
    return rc, (json.loads(out.read_text(encoding="utf-8")) if rc == 0 else None)


class TestShapesEnginesActuallyProduce:
    def test_bare_object(self, tmp_path):
        rc, got = extract(tmp_path, json.dumps(PAYLOAD))
        assert rc == 0 and got == PAYLOAD

    def test_prose_then_object(self, tmp_path):
        rc, got = extract(tmp_path, "thinking...\nworking\n" + json.dumps(PAYLOAD))
        assert rc == 0 and got == PAYLOAD

    def test_result_envelope(self, tmp_path):
        rc, got = extract(tmp_path, json.dumps({"type": "result", "result": PAYLOAD}))
        assert rc == 0 and got == PAYLOAD

    def test_envelope_holding_a_json_string(self, tmp_path):
        rc, got = extract(tmp_path, json.dumps({"result": json.dumps(PAYLOAD)}))
        assert rc == 0 and got == PAYLOAD

    def test_envelope_holding_a_fenced_block(self, tmp_path):
        wrapped = {"result": "```json\n" + json.dumps(PAYLOAD) + "\n```"}
        rc, got = extract(tmp_path, json.dumps(wrapped))
        assert rc == 0 and got == PAYLOAD

    def test_grok_shape_fence_inside_prose(self, tmp_path):
        """MEASURED: grok returns {"text": "<prose> ```json {...} ``` "}.

        `parse_maybe` used to match only a fence spanning the WHOLE string, so
        this correct answer failed the run with exit 3.
        """
        wrapped = {"text": "I'll review the change. Reading the files.## Findings\n\n"
                           "```json\n" + json.dumps(PAYLOAD) + "\n```\n\nThat is all."}
        rc, got = extract(tmp_path, json.dumps(wrapped))
        assert rc == 0 and got == PAYLOAD

    def test_terminal_escapes_are_stripped(self, tmp_path):
        rc, got = extract(tmp_path, "\x1b[32mworking\x1b[0m\n" + json.dumps(PAYLOAD))
        assert rc == 0 and got == PAYLOAD

    def test_last_answer_wins_over_earlier_chatter(self, tmp_path):
        early = json.dumps({"engine": "x", "verdict": "approve", "findings": ["stale"]})
        rc, got = extract(tmp_path, early + "\nreconsidering\n" + json.dumps(PAYLOAD))
        assert rc == 0 and got == PAYLOAD

    def test_conclusion_fence_wins_over_a_quoted_one(self, tmp_path):
        """An engine that shows its work states its conclusion last."""
        draft = {"engine": "x", "verdict": "approve", "findings": ["draft"]}
        wrapped = {"text": "First I considered:\n```json\n%s\n```\nBut finally:\n```json\n%s\n```"
                           % (json.dumps(draft), json.dumps(PAYLOAD))}
        rc, got = extract(tmp_path, json.dumps(wrapped))
        assert rc == 0 and got == PAYLOAD


class TestCouldNotFindIsNotNothingThere:
    def test_no_json_at_all_is_exit_3(self, tmp_path):
        rc, _ = extract(tmp_path, "the engine only spoke prose today")
        assert rc == 3

    def test_unreadable_log_is_exit_3_not_0(self, tmp_path):
        out = tmp_path / "out.json"
        assert extract_to(str(tmp_path / "nope.log"), str(out)) == 3

    def test_empty_log_is_exit_3(self, tmp_path):
        rc, _ = extract(tmp_path, "")
        assert rc == 3


class TestBalancedScanning:
    def test_a_brace_inside_a_string_does_not_end_the_object(self):
        text = '{"summary": "use {} for an empty dict", "n": 1}'
        assert list(balanced_objects(text)) == [text]

    def test_escaped_quote_inside_a_string(self):
        text = '{"summary": "he said \\"hi\\" loudly"}'
        assert list(balanced_objects(text)) == [text]

    def test_nested_objects_yield_only_the_outer_span(self):
        text = '{"a": {"b": 1}}'
        assert list(balanced_objects(text)) == [text]


@pytest.mark.parametrize("value", [None, 42, [], {"a": 1}])
def test_parse_maybe_rejects_non_strings(value):
    assert parse_maybe(value) is None
