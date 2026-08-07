#!/usr/bin/env python3
"""CLI shim: the logic lives in lider/extract.py.

Usage: extract-json.py <log> <out>
Exit:  0 wrote <out> | 3 nothing extractable | 2 bad usage
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lider.extract import extract_to   # noqa: E402


def main():
    if len(sys.argv) != 3:
        print("usage: extract-json.py <log> <out>", file=sys.stderr)
        return 2
    return extract_to(sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    sys.exit(main())
