#!/usr/bin/env python3
"""CLI shim: the logic lives in lider/validate.py.

Usage: validate-json.py <schema.json> <instance.json>
Exit:  0 valid | 1 invalid | 2 unreadable or bad usage
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lider.validate import validate_file   # noqa: E402


def main():
    if len(sys.argv) != 3:
        print("usage: validate-json.py <schema.json> <instance.json>", file=sys.stderr)
        return 2
    return validate_file(sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    sys.exit(main())
