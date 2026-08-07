#!/usr/bin/env python3
"""extract-json.py <log> <out>

Pull a structured result out of an engine run log and write it to <out>.

Engines differ in how they hand back an answer. Some write a JSON file directly
(nothing to do here). Others print to stdout, wrapped in an envelope, wrapped in
a markdown fence, or surrounded by prose and terminal escapes. This recovers the
payload from all of those without knowing any single engine's format:

  1. strip terminal escape sequences;
  2. scan for balanced top-level JSON objects (string- and escape-aware, so a
     brace inside a quoted string never ends an object early);
  3. take the LAST one — an engine's final answer follows its progress chatter;
  4. unwrap result envelopes, including a payload delivered as a JSON *string*
     inside one, and including a fenced ```json block inside that string.

Exit codes:  0 wrote <out>   |   3 nothing extractable   |   2 bad usage
Exit 3 is deliberately distinct from 0: "I could not find it" must never be
reported as "there was nothing there".
"""
import json
import re
import sys

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
# The same block, but found anywhere inside surrounding prose.
FENCE_ANY = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# Keys an engine may hide the real payload behind, most specific first.
ENVELOPE_KEYS = (
    "structured_output", "structuredOutput", "structured_result",
    "result", "output", "response", "content", "text", "message",
)


def balanced_objects(text):
    """Yield every balanced top-level {...} span, ignoring braces inside strings."""
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    yield text[start:i + 1]
                    start = None


def parse_maybe(s):
    """Recover JSON from a string an engine put its answer inside.

    MEASURED: grok returns `{"text": "<prose> ```json {...} ``` "}` - the payload
    is fenced, but the fence does not span the whole string. Matching only an
    anchored fence made a chatty engine's answer unreachable and failed the run
    with exit 3 even though the engine had answered correctly.

    Three attempts, most specific first, so a fenced answer always beats an
    object the engine merely quoted while reasoning:
      1. the whole string is JSON, or a fence around it;
      2. a fenced ```json block anywhere inside the prose - the LAST one, since
         an engine that shows its work states its conclusion at the end;
      3. the last balanced JSON object embedded anywhere in the string.
    """
    if not isinstance(s, str):
        return None

    match = FENCE.match(s)
    body = match.group(1).strip() if match else s.strip()
    if body.startswith(("{", "[")):
        try:
            return json.loads(body)
        except ValueError:
            pass

    for fenced in reversed(FENCE_ANY.findall(s)):
        try:
            return json.loads(fenced.strip())
        except ValueError:
            continue

    for span in reversed(list(balanced_objects(s))):
        try:
            return json.loads(span)
        except ValueError:
            continue
    return None


def unwrap(obj, depth=0):
    """Peel result envelopes until what is left is the payload itself."""
    if depth > 6 or not isinstance(obj, dict):
        return obj
    for key in ENVELOPE_KEYS:
        if key not in obj:
            continue
        val = obj[key]
        if isinstance(val, dict):
            return unwrap(val, depth + 1)
        inner = parse_maybe(val)
        if inner is not None:
            return unwrap(inner, depth + 1) if isinstance(inner, dict) else inner
    return obj


def extract_to(log_path, out_path):
    """Recover a structured result from a run log. 0 wrote it, 3 nothing usable.

    Exit 3 is deliberately distinct from 0: "I could not find it" must never be
    reported as "there was nothing there".
    """
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            text = ANSI.sub("", fh.read())
    except OSError as exc:
        print("extract-json: cannot read %s: %s" % (log_path, exc), file=sys.stderr)
        return 3

    # Last balanced object that actually parses. Walking backwards means the
    # engine's final answer wins over any JSON it echoed while working.
    for span in reversed(list(balanced_objects(text))):
        try:
            obj = json.loads(span)
        except ValueError:
            continue
        payload = unwrap(obj)
        if not isinstance(payload, (dict, list)):
            continue
        try:
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
        except OSError as exc:
            print("extract-json: cannot write %s: %s" % (out_path, exc), file=sys.stderr)
            return 3
        return 0

    print("extract-json: no parseable JSON object found in %s" % log_path, file=sys.stderr)
    return 3
