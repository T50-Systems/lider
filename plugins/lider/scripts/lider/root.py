"""Resolve the Lider plugin root across hosts.

Hosts set different env vars:

  LIDER_PLUGIN_ROOT   explicit (install-skills, docs)
  GROK_PLUGIN_ROOT    Grok Build plugin harness
  CLAUDE_PLUGIN_ROOT  Claude Code plugin harness (Grok also aliases this)

When none are set (OpenCode, Pi, Codex skill copies), fall back to this package
layout: .../plugins/lider/scripts/lider/root.py -> .../plugins/lider
"""
import os


def plugin_root():
    for key in ("LIDER_PLUGIN_ROOT", "GROK_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        value = os.environ.get(key)
        if value and os.path.isdir(os.path.join(value, "scripts")):
            return os.path.abspath(value)
    # scripts/lider/root.py -> scripts -> plugins/lider
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(here))
