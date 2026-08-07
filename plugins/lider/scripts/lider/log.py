"""lider.log - where output goes, and why it goes there.

This plugin has THREE output destinations with different jobs. Mixing them up is
not cosmetic: two of the three have load-bearing constraints.

  1. STDOUT - the live narration. Heartbeats and progress. Claude Code streams a
     background task's stdout into the task panel, so this is the only channel
     the operator actually watches while work runs. It must be unbuffered and it
     must never be swallowed.

       RULE: a task that produces no stdout is indistinguishable from a hang.
       Never capture a child's stdout without re-emitting it. If you need it on
       disk too, TEE it - do not redirect it.

  2. STDERR - problems and diagnostics. Always-on warnings, plus a debug channel
     gated on LIDER_DEBUG=1 for the things you only want when something is
     misbehaving (resolved adapter, binary, argv, interpreter).

  3. THE RUN LOG (<log>) - the ENGINE'S transcript, and nothing else.

       RULE: never write to <log> while a run is in flight. The stall watchdog
       decides whether the engine is alive by measuring that file's GROWTH from
       a pre-launch baseline. Our own writes would read as engine activity and
       keep a dead run looking healthy for as long as we kept talking. Headers
       and footers are fine - they happen before launch and after exit.

Deliberately not the `logging` module: these are CLIs whose stdout contract IS
the product (the panel reads it, and exit codes carry the verdicts). Levels,
handlers and formatters would add indirection over three fixed destinations
without changing what any of them does.
"""
import os
import sys
import threading

_LOCK = threading.Lock()          # concurrent lenses write to one stdout
_PREFIX = threading.local()

DEBUG = os.environ.get("LIDER_DEBUG") in ("1", "true", "yes")

# MEASURED: an engine's own activity text contained "->" as U+2192, `emit` wrote it
# to a cp1252 Windows console, and the UnicodeEncodeError propagated out of the
# supervisor and killed the whole run - an expensive planning call lost to a
# character. Narration must never be able to do that.
#
# We do NOT sanitise our own strings and hope: everything that reaches these
# streams is ultimately engine-derived, so the guard belongs on the stream. Two
# layers, because the first is not available everywhere.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError, OSError):
        pass


def _safe(text):
    """Render `text` in whatever the stream can actually encode."""
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(encoding, "strict")
        return text
    except (UnicodeEncodeError, LookupError):
        return text.encode(encoding, "backslashreplace").decode(encoding, "replace")


def set_prefix(prefix):
    """Tag this thread's output, so concurrent runs stay attributable."""
    _PREFIX.value = prefix


def _tag():
    return getattr(_PREFIX, "value", "") or ""


def emit(message):
    """Live narration -> stdout, flushed. The operator is watching this."""
    with _LOCK:
        sys.stdout.write(_safe("%s%s\n" % (_tag(), message)))
        sys.stdout.flush()


def warn(message):
    """A problem worth reporting whether or not anyone asked for detail."""
    with _LOCK:
        sys.stderr.write(_safe("%s%s\n" % (_tag(), message)))
        sys.stderr.flush()


def diag(message):
    """Detail for when something is misbehaving. Gated on LIDER_DEBUG=1."""
    if not DEBUG:
        return
    with _LOCK:
        sys.stderr.write(_safe("%s[debug] %s\n" % (_tag(), message)))
        sys.stderr.flush()


def diag_argv(label, argv, limit=160):
    """Log a command line with its prompt truncated.

    The prompt is usually the largest argument by far and is never what you are
    debugging; the flags around it are.
    """
    if not DEBUG:
        return
    shown = [a if len(a) <= limit else a[:limit] + "...<%d chars>" % len(a) for a in argv]
    diag("%s: %s" % (label, " ".join(shown)))
