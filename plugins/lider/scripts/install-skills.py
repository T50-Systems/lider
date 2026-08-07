#!/usr/bin/env python3
"""Install Lider skills into harness discovery paths.

Lider's source of truth is plugins/lider/skills/. Hosts discover skills from
different directories. This copies each skill (SKILL.md tree) into every path
you select so OpenCode, Pi, Codex-compatible, Claude, and Grok can all load them.

Usage:
  python install-skills.py                 # all known harness paths under the repo
  python install-skills.py --harness pi    # only Pi-compatible locations
  python install-skills.py --user          # also install under the user home dirs

Does not delete anything outside the destination skill name folders it writes.
"""
from __future__ import print_function

import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)                 # plugins/lider
REPO = os.path.dirname(os.path.dirname(PLUGIN))  # repo root
SOURCE = os.path.join(PLUGIN, "skills")

# name -> list of relative dirs under repo (or absolute via home)
HARNESS_REPO_PATHS = {
    "agents": [".agents/skills"],          # OpenCode + Pi + agent-skills standard
    "opencode": [".opencode/skills"],
    "pi": [".pi/skills"],
    "claude": [".claude/skills"],
    "codex": [".codex/skills"],            # project-local if present
}

HARNESS_USER_PATHS = {
    "agents": [os.path.join("~", ".agents", "skills")],
    "opencode": [os.path.join("~", ".config", "opencode", "skills"),
                 os.path.join("~", ".claude", "skills")],
    "pi": [os.path.join("~", ".pi", "agent", "skills"),
           os.path.join("~", ".agents", "skills")],
    "claude": [os.path.join("~", ".claude", "skills")],
    "codex": [os.path.join("~", ".codex", "skills")],
    "grok": [],  # Grok uses plugin marketplace install, not skill copy
}


def expand(path):
    return os.path.abspath(os.path.expanduser(path))


def skill_names():
    return sorted(
        name for name in os.listdir(SOURCE)
        if os.path.isdir(os.path.join(SOURCE, name))
        and os.path.isfile(os.path.join(SOURCE, name, "SKILL.md"))
    )


def copy_skill(name, dest_root, dry_run):
    src = os.path.join(SOURCE, name)
    dst = os.path.join(dest_root, name)
    if dry_run:
        print("  would install %s -> %s" % (name, dst))
        return
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    # Drop a pointer so scripts can find the plugin even outside Claude/Grok env.
    marker = os.path.join(dst, "LIDER_PLUGIN_ROOT.txt")
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write(PLUGIN + "\n")
    print("  installed %s -> %s" % (name, dst))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--harness", action="append",
                    choices=sorted(set(HARNESS_REPO_PATHS) | set(HARNESS_USER_PATHS) | {"all"}),
                    help="which harness layout(s); default all repo layouts")
    ap.add_argument("--user", action="store_true",
                    help="also install into user home discovery paths")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--repo", default=REPO, help="repo root (default: detected)")
    args = ap.parse_args()

    names = skill_names()
    if not names:
        print("install-skills: no skills under %s" % SOURCE, file=sys.stderr)
        return 2

    harnesses = args.harness or ["all"]
    if "all" in harnesses:
        harnesses = sorted(HARNESS_REPO_PATHS)

    print("Lider plugin: %s" % PLUGIN)
    print("Skills: %s" % ", ".join(names))
    os.environ["LIDER_PLUGIN_ROOT"] = PLUGIN

    targets = []
    for h in harnesses:
        for rel in HARNESS_REPO_PATHS.get(h, []):
            targets.append(os.path.join(args.repo, rel.replace("/", os.sep)))
        if args.user:
            for rel in HARNESS_USER_PATHS.get(h, []):
                targets.append(expand(rel))

    # Dedup preserve order
    seen = set()
    uniq = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            uniq.append(t)

    for dest in uniq:
        print("-> %s" % dest)
        if not args.dry_run:
            os.makedirs(dest, exist_ok=True)
        for name in names:
            copy_skill(name, dest, args.dry_run)

    print("")
    print("Set LIDER_PLUGIN_ROOT=%s so skills can find scripts from any host." % PLUGIN)
    print("Claude Code / Grok: prefer plugin marketplace install (keeps CLAUDE_/GROK_PLUGIN_ROOT).")
    print("OpenCode / Pi / .agents: use the paths above after this install.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
