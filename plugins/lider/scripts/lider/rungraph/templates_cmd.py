"""G2: resolve / print role prompt templates (content, not structure)."""
from __future__ import annotations

import os
import sys

from .constants import OK, USAGE

ROLES = ("architect", "implementer", "reviewer", "challenger")


def templates_root():
    """plugins/lider/templates — sibling of scripts/."""
    here = os.path.dirname(os.path.abspath(__file__))
    # .../scripts/lider/rungraph -> .../plugins/lider/templates
    return os.path.normpath(os.path.join(here, "..", "..", "..", "templates"))


def role_template_path(role):
    return os.path.join(templates_root(), "roles", "%s.md" % role)


def cmd_template(args):
    """List or print a role template. Structure stays in constants/guards."""
    role = getattr(args, "role", None)
    path_only = bool(getattr(args, "path_only", False))
    list_all = bool(getattr(args, "list", False)) or not role

    if list_all and not role:
        root = templates_root()
        print("templates root: %s" % root)
        print("roles (G2 content — edit wording without changing edges):")
        for name in ROLES:
            path = role_template_path(name)
            mark = "ok" if os.path.isfile(path) else "MISSING"
            print("  [%s] %-12s %s" % (mark, name, path))
        print("docs: plugins/lider/templates/README.md  |  docs/PGE.md")
        return OK

    if role not in ROLES:
        print("rungraph: --role must be one of: %s" % ", ".join(ROLES),
              file=sys.stderr)
        return USAGE
    path = role_template_path(role)
    if path_only:
        print(path)
        return OK if os.path.isfile(path) else USAGE
    if not os.path.isfile(path):
        print("rungraph: missing template %s" % path, file=sys.stderr)
        return USAGE
    with open(path, encoding="utf-8") as fh:
        sys.stdout.write(fh.read())
    return OK
