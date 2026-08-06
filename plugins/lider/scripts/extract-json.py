#!/usr/bin/env python3
"""CLI shim: the logic lives in lider/extract.py. Usage: extract-json.py <log> <out>"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lider.extract import extract_to
if len(sys.argv) != 3:
    print("usage: extract-json.py <log> <out>", file=sys.stderr); sys.exit(2)
sys.exit(extract_to(sys.argv[1], sys.argv[2]))
