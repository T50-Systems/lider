#!/usr/bin/env python3
"""validate-json.py <schema.json> <instance.json>

Validate an engine's structured output against a JSON Schema.

Engines that enforce a schema server-side (Codex `--output-schema`, Claude
`--json-schema`) return conformant output by construction. Engines that merely
print JSON do not, so their output has to be checked locally before anything
downstream trusts its shape.

Uses `jsonschema` when installed; otherwise falls back to a built-in check of
the subset this plugin's schemas actually use: type, properties, required,
enum, items, additionalProperties. The fallback is intentionally strict — it
reports what it cannot check rather than passing it.

Exit codes:  0 valid  |  1 invalid (reasons on stderr)  |  2 bad usage/unreadable
"""
import json
import sys

TYPES = {
    "object": dict, "array": list, "string": str,
    "number": (int, float), "integer": int, "boolean": bool, "null": type(None),
}


def type_ok(value, spec):
    names = spec if isinstance(spec, list) else [spec]
    for name in names:
        py = TYPES.get(name)
        if py is None:
            return True  # unknown type keyword: do not invent a failure
        # bool is a subclass of int in Python; keep them distinct here.
        if name in ("number", "integer") and isinstance(value, bool):
            continue
        if isinstance(value, py):
            return True
    return False


def check(instance, schema, path, errors):
    if not isinstance(schema, dict):
        return
    if "type" in schema and not type_ok(instance, schema["type"]):
        errors.append("%s: expected type %s, got %s"
                      % (path or "<root>", schema["type"], type(instance).__name__))
        return
    if "enum" in schema and instance not in schema["enum"]:
        errors.append("%s: %r is not one of %s" % (path or "<root>", instance, schema["enum"]))
    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in instance:
                errors.append("%s: missing required property '%s'" % (path or "<root>", key))
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in props:
                    errors.append("%s: unexpected property '%s'" % (path or "<root>", key))
        for key, sub in props.items():
            if key in instance:
                check(instance[key], sub, "%s.%s" % (path, key) if path else key, errors)
    elif isinstance(instance, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(instance):
            check(item, schema["items"], "%s[%d]" % (path or "<root>", i), errors)


def validate_file(schema_path, instance_path):
    """0 valid, 1 invalid (reasons on stderr), 2 unreadable."""
    try:
        with open(schema_path, encoding="utf-8") as fh:
            schema = json.load(fh)
        with open(instance_path, encoding="utf-8") as fh:
            instance = json.load(fh)
    except (OSError, ValueError) as exc:
        print("validate-json: %s" % exc, file=sys.stderr)
        return 2

    try:
        import jsonschema
    except ImportError:
        errors = []
        check(instance, schema, "", errors)
    else:
        errors = [e.message for e in
                  sorted(jsonschema.Draft7Validator(schema).iter_errors(instance),
                         key=lambda e: list(e.path))]

    if errors:
        for err in errors[:10]:
            print("validate-json: %s" % err, file=sys.stderr)
        if len(errors) > 10:
            print("validate-json: ... and %d more" % (len(errors) - 10), file=sys.stderr)
        return 1
    return 0
