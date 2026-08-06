#!/usr/bin/env python3
"""CLI shim: the logic lives in lider/validate.py. Usage: validate-json.py <schema> <instance>"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lider.validate import validate_file
if len(sys.argv) != 3:
    print("usage: validate-json.py <schema.json> <instance.json>", file=sys.stderr); sys.exit(2)
sys.exit(validate_file(sys.argv[1], sys.argv[2]))
