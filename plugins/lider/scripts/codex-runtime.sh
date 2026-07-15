#!/usr/bin/env bash
# codex-runtime.sh — shared supervision layer sourced by the Codex wrappers.
#
# Provides: preflight, and run_supervised — heartbeat + status file + inactivity
# watchdog + hardened process-tree kill + bounded retry. Also re-exports the
# isolation helper (codex-home-iso.sh) so wrappers source only this one file.
#
# Design (why it is built this way):
#  - `timeout -k 10` stays the HARD backstop around every launch: even if this
#    supervisor misbehaves, zombie behavior is never worse than the bare wrapper.
#  - The heartbeat goes to STDOUT and the status to <log>.status.json — NEVER to
#    <log> itself, so the inactivity watchdog (which measures <log> GROWTH from a
#    pre-launch baseline) is not fooled by our own writes or the log header.
#  - The polling loop IS the wrapper foreground; Codex runs in the background under
#    `timeout`, so there is no separate monitor process that could orphan.
#  - On SIGINT/SIGTERM to the wrapper, a trap tears the Codex tree down first and
#    records a terminal status/<done>, so killing the wrapper leaves nothing behind.

_CODEX_RUNTIME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=codex-home-iso.sh
. "$_CODEX_RUNTIME_DIR/codex-home-iso.sh"

# Coerce a value to a base-10 non-negative integer, else fall back to a default.
# The 10# guard prevents "08" being read as (invalid) octal; the length guard
# rejects absurdly long digit strings that would overflow signed arithmetic to a
# negative value and slip past later upper-bound clamps.
_int() {
  case "${1:-}" in
    ''|*[!0-9]*) echo "$2"; return ;;
  esac
  [ "${#1}" -gt 9 ] && { echo "$2"; return; }
  echo "$((10#$1))"
}

CODEX_POLL_S="$(_int "${CODEX_POLL_S:-}" 5)";       [ "$CODEX_POLL_S" -ge 1 ] || CODEX_POLL_S=5
CODEX_HEARTBEAT_S="$(_int "${CODEX_HEARTBEAT_S:-}" 10)"; [ "$CODEX_HEARTBEAT_S" -ge 1 ] || CODEX_HEARTBEAT_S=10

# Globals the signal trap needs to finalize cleanly.
_CODEX_CURRENT_TPID=""   # pid of the live `timeout` child
_CODEX_STATUS_FILE=""    # status file for the active run
_CODEX_TOOL=""           # tool label for the active run
_CODEX_DONE_FILE=""      # optional: wrapper sets this so the trap can mark <done>
_CODEX_STARTED=0         # run start epoch (for status started_at / resume checks)

# Output of _last_activity (display hint; authoritative in-flight is incremental).
LAST_ACT=""              # human "what Codex is doing now"
LF=$'\n'                 # newline, for line-aligned incremental parsing

# --- process helpers -------------------------------------------------------
_now() { date +%s; }

_fsize() { stat -c %s "$1" 2>/dev/null || wc -c <"$1" 2>/dev/null || echo 0; }

# Map an MSYS/Git-Bash pid to its Windows pid (WINPID column of `ps`).
_winpid() { ps 2>/dev/null | awk -v p="$1" '$1==p {print $4; exit}'; }

# Portable "pid ppid" pairs: Linux via `ps -eo`, MSYS via the column layout
# (bare `ps` on Linux puts TTY in column 2, so we cannot assume that there).
_pid_ppid_pairs() {
  ps -eo pid=,ppid= 2>/dev/null && return 0
  ps 2>/dev/null | awk 'NR>1 {print $1, $2}'
}

# Echo every pid descending from $1 (deepest first).
_descendants() {
  local parent="$1" child
  for child in $(_pid_ppid_pairs | awk -v pp="$parent" '$2==pp {print $1}'); do
    _descendants "$child"
    echo "$child"
  done
}

# Kill the whole tree rooted at an MSYS pid, leaving no orphans.
# For the real workload (a NATIVE codex.exe tree) `taskkill //F //T` on each
# node's winpid tears down the Windows subtree atomically, which is why the
# empirical external-kill test leaves zero codex.exe. The MSYS PPID walk is a
# supplement. We re-enumerate from the root on every phase (never signalling a
# stale, possibly-reused pid) and guard each signal with `kill -0`.
# Residual (accepted) limitation: for a PURE-MSYS child tree a grandchild
# reparented at the instant its parent dies can escape the walk — the wrappers
# run codex.exe (native), so this does not apply to them.
_kill_tree() {
  local root="$1" pass p wp
  [ -n "$root" ] || return 0
  for pass in 1 2 3; do
    for p in $root $(_descendants "$root"); do
      kill -0 "$p" 2>/dev/null || continue
      wp="$(_winpid "$p")"
      kill -TERM "$p" 2>/dev/null
      if [ -n "$wp" ] && command -v taskkill >/dev/null 2>&1; then
        taskkill //F //T //PID "$wp" >/dev/null 2>&1 || true
      fi
    done
    sleep 1
    # Re-enumerate fresh before the hard kill so a reused pid (no longer a
    # descendant of root) is never SIGKILLed.
    for p in $root $(_descendants "$root"); do
      kill -0 "$p" 2>/dev/null && kill -KILL "$p" 2>/dev/null || true
    done
    kill -0 "$root" 2>/dev/null || return 0
  done
}

# Trap handler: signalled wrapper → take Codex down and leave a terminal record.
_codex_on_signal() {
  _kill_tree "${_CODEX_CURRENT_TPID:-}"
  [ -n "${_CODEX_STATUS_FILE:-}" ] && \
    _write_status "$_CODEX_STATUS_FILE" "${_CODEX_TOOL:-codex}" "cancelled" 0 0 null 0 0 0 130 "signal" ""
  [ -n "${_CODEX_DONE_FILE:-}" ] && echo 130 >"$_CODEX_DONE_FILE" 2>/dev/null
  exit 130
}

# --- observability ---------------------------------------------------------
# JSON-safe string: reduce to printable ASCII (drops control bytes AND any
# high/UTF-8 bytes, so no multibyte char can be split by the length cut into an
# invalid sequence), truncate on those single bytes, then escape \ and ".
# The value is a human diagnostic hint, so dropping non-ASCII is acceptable.
_json_escape() {
  printf '%s' "${1:-}" | LC_ALL=C tr -d '\000-\037\177-\377' | cut -c1-180 | sed 's/\\/\\\\/g; s/"/\\"/g'
}

# Set LAST_ACT to a short human "what Codex is doing right now", read from its
# stream markers in the log tail (the most recent event wins). This is a DISPLAY
# hint only; the authoritative in-flight state for the watchdog is tracked
# incrementally by _inflight_delta (which the bounded tail here cannot lose).
_last_activity() {
  local log="$1"
  LAST_ACT=""
  [ -s "$log" ] || return 0
  # Bound the work by BYTES then lines so a few huge diff lines can't make each
  # poll scan a large suffix. Strip CSI/OSC terminal sequences (not just SGR).
  LAST_ACT="$(tail -c 20000 "$log" 2>/dev/null | tail -n 80 \
    | sed 's/\x1b\[[0-9;?]*[ -/]*[@-~]//g; s/\x1b\][^\x07]*\x07//g' \
    | awk '
        function nextline(  r) { c=""; r=getline c; if (r<=0) c=""; gsub(/^[ \t]+/,"",c); return c }
        /^exec$/                 { nextline(); if (c!="") act="exec: " c; next }
        /^\+\+\+ b\//            { f=$0; sub(/^\+\+\+ b\//,"",f); act="edit: " f; next }
        /^apply patch$/          { act="edit: applying patch"; next }
        / succeeded in [0-9]+ms/ { act="cmd ok"; next }
        / failed in [0-9]+ms/    { act="cmd failed"; next }
        / exited [0-9]+ /        { act="cmd exited"; next }
        /^codex$/                { nextline(); if (c!="") act="say: " c; next }
        /^tokens used$/          { act="finalizing (counting tokens)"; next }
        END { print act }
      ' | cut -c1-140)"
}

# Read a NEW log chunk on stdin and echo the last command open/close transition
# in it: "1" (a command started), "0" (one finished / Codex is talking again), or
# "-" (no transition — caller keeps the previous state). Tracking transitions
# incrementally over appended bytes — instead of recomputing from a bounded tail —
# means a verbose command that scrolls its `exec` marker out of the display window
# can never flip in-flight state and get a healthy command killed.
_inflight_delta() {
  awk '
    /^exec$/                 { st=1; seen=1; next }
    / succeeded in [0-9]+ms/ { st=0; seen=1; next }
    / failed in [0-9]+ms/    { st=0; seen=1; next }
    / exited [0-9]+ /        { st=0; seen=1; next }
    /^apply patch$/          { st=0; seen=1; next }
    /^codex$/                { st=0; seen=1; next }
    /^tokens used$/          { st=0; seen=1; next }
    END { print (seen ? st : "-") }
  '
}

# Heartbeat -> STDOUT (the background panel), never the measured <log>.
_heartbeat() {
  # tool attempt max elapsed idle bytes activity
  printf '[%s] codex/%s attempt=%s/%s elapsed=%ss idle=%ss log=%sB | %s\n' \
    "$(date +%H:%M:%S)" "$1" "$2" "$3" "$4" "$5" "$6" "${7:-...}"
}

# Atomic status write (temp + mv) so a reader never sees a half-written file.
_write_status() {
  # file tool state attempt max pid elapsed idle bytes exit reason activity
  local f="$1"; shift
  [ -n "$f" ] || return 0
  local rsn act; rsn="$(_json_escape "${10:-}")"; act="$(_json_escape "${11:-}")"
  local tmp="${f}.tmp.$$"
  # updated_at lets a resumed orchestrator tell a truly-running task (fresh
  # updated_at) from an orphaned status left by a dead wrapper (stale + dead pid).
  printf '{"tool":"%s","state":"%s","attempt":%s,"max_attempts":%s,"pid":%s,"elapsed_s":%s,"idle_s":%s,"log_bytes":%s,"exit":%s,"reason":"%s","activity":"%s","started_at":%s,"updated_at":%s}\n' \
    "$1" "$2" "$3" "$4" "${5:-null}" "${6:-0}" "${7:-0}" "${8:-0}" "${9:-null}" "$rsn" "$act" "${_CODEX_STARTED:-0}" "$(_now)" >"$tmp" 2>/dev/null
  mv -f "$tmp" "$f" 2>/dev/null || true
}

# --- supervised run --------------------------------------------------------
# One attempt: launch under `timeout`, monitor, enforce watchdogs, record status.
_supervise_once() {
  # tool timeout_s log status stall_s startup_s attempt max -- cmd...
  local tool="$1" timeout_s="$2" log="$3" status="$4" stall_s="$5" startup_s="$6" attempt="$7" max="$8"
  shift 8
  [ "${1:-}" = "--" ] && shift

  local start now elapsed idle bytes baseline prev_bytes last_out last_hb grew rc reason tpid final_state act inflight disp off chunk complete d
  start="$(_now)"; last_out="$start"; last_hb="$start"; grew=0; reason=""; act=""; inflight=0; disp=""
  baseline="$(_fsize "$log")"; prev_bytes="$baseline"; off="$baseline"   # ignore the pre-launch header

  _CODEX_STATUS_FILE="$status"; _CODEX_TOOL="$tool"
  _write_status "$status" "$tool" "starting" "$attempt" "$max" "null" 0 0 0 "null" "" ""

  # Codex in the background under the hard-timeout backstop.
  timeout -k 10 "$timeout_s" "$@" >>"$log" 2>&1 &
  tpid=$!
  _CODEX_CURRENT_TPID="$tpid"

  while :; do
    sleep "$CODEX_POLL_S"
    now="$(_now)"; elapsed=$((now - start))
    # Reap first: Codex may have exited during the sleep (avoids killing a
    # finished process and mis-reporting its result as a watchdog abort).
    if ! kill -0 "$tpid" 2>/dev/null; then
      wait "$tpid"; rc=$?; break
    fi
    bytes="$(_fsize "$log")"
    if [ "$bytes" -gt "$prev_bytes" ]; then last_out="$now"; prev_bytes="$bytes"; grew=1; fi
    idle=$((now - last_out))
    # In-flight tracked incrementally over the newly-appended bytes (robust to the
    # display window). Process ONLY through the last complete line and advance the
    # offset by exactly those bytes, so a line split across two polls is never
    # lost (its two halves would otherwise match no marker). Activity TEXT still
    # comes from the tail (a display hint).
    chunk="$(tail -c "+$((off + 1))" "$log" 2>/dev/null)"
    case "$chunk" in
      *"$LF"*)
        complete="${chunk%"$LF"*}$LF"
        off=$(( off + $(printf '%s' "$complete" | wc -c) ))
        d="$(printf '%s' "$complete" | _inflight_delta)"; [ "$d" != "-" ] && inflight="$d"
        ;;
      *) : ;;   # no complete line yet — wait, keep the offset
    esac
    _last_activity "$log"; [ -n "$LAST_ACT" ] && act="$LAST_ACT"   # sticky act
    disp="$act"; [ "$inflight" = "1" ] && disp="$act (running ${idle}s)"

    _write_status "$status" "$tool" "running" "$attempt" "$max" "$tpid" "$elapsed" "$idle" "$bytes" "null" "" "$disp"
    if [ $((now - last_hb)) -ge "$CODEX_HEARTBEAT_S" ]; then
      _heartbeat "$tool" "$attempt" "$max" "$elapsed" "$idle" "$bytes" "$disp"; last_hb="$now"
    fi

    # Watchdogs. The hard timeout is enforced by `timeout` itself (-> rc 124).
    if [ "$grew" -eq 0 ] && [ "$elapsed" -ge "$startup_s" ]; then reason="startup-failed"; break; fi
    # Stall only when Codex itself is idle — NOT while a shell command is running
    # (its silence is expected; the hard timeout bounds a runaway command).
    if [ "$grew" -eq 1 ] && [ "$inflight" != "1" ] && [ "$idle" -ge "$stall_s" ]; then reason="stalled"; break; fi
  done

  if [ -n "$reason" ]; then
    if kill -0 "$tpid" 2>/dev/null; then
      _kill_tree "$tpid"; wait "$tpid" 2>/dev/null || true
      rc=125
    else
      # Finished on its own between the last liveness check and the abort.
      reason=""; wait "$tpid"; rc=$?
    fi
  fi
  if [ -z "$reason" ]; then
    case "$rc" in
      0) reason="ok" ;;
      124) reason="timeout" ;;
      *) reason="exit-$rc" ;;
    esac
  fi
  _CODEX_CURRENT_TPID=""

  elapsed=$(( $(_now) - start ))
  bytes="$(_fsize "$log")"
  _last_activity "$log"; [ -n "$LAST_ACT" ] && act="$LAST_ACT"
  final_state="done"; [ "$rc" -ne 0 ] && final_state="failed"
  _write_status "$status" "$tool" "$final_state" "$attempt" "$max" "null" "$elapsed" 0 "$bytes" "$rc" "$reason" "$act"
  return "$rc"
}

# Classify an attempt's outcome for the retry decision, from the exit code and
# the log tail:  done | retry | auth | fatal.
#  - 124/125 (timeout/watchdog) are always transient → retry.
#  - 2/127 (bad usage / codex missing) are permanent → fatal.
#  - other non-zero: sniff the tail. Transient API/network signatures → retry;
#    auth-failure signatures → auth (actionable, NOT retried — retrying a 401
#    just burns attempts); anything else → fatal (a deterministic error recurs).
# NB (D, inverted): we deliberately do NOT re-copy auth.json between attempts.
# OAuth refresh tokens rotate inside the isolated home during an attempt; copying
# the older real-home token back over a rotated one could itself induce a 401.
_retry_class() {
  local rc="$1" log="$2" from="${3:-0}" t
  case "$rc" in
    0) echo done; return ;;
    124|125) echo retry; return ;;
    2|127) echo fatal; return ;;
  esac
  # Heuristic: classify only THIS attempt's output (from its start offset), and
  # only its error tail — never the cumulative transcript (a prior attempt's 429,
  # prompt text, or code could otherwise misclassify the current failure).
  t="$(tail -c "+$((from + 1))" "$log" 2>/dev/null | tail -c 2000 | tr 'A-Z' 'a-z')"
  case "$t" in
    *unauthorized*|*"not logged in"*|*"invalid api key"*|*"authentication failed"*|*"401 "*|*"token expired"*|*"codex login"*|*"please log in"*) echo auth; return ;;
  esac
  case "$t" in
    *" 429"*|*"429 "*|*"too many requests"*|*"rate limit"*|*" 500"*|*" 502"*|*" 503"*|*" 504"*|*" 408"*|*"internal server error"*|*"bad gateway"*|*"service unavailable"*|*"gateway timeout"*|*overloaded*|*econnreset*|*etimedout*|*"stream disconnected"*|*"connection reset"*|*"temporarily unavailable"*|*"timed out"*) echo retry; return ;;
  esac
  echo fatal
}

# Public entry point: retry-wrapped supervised run.
run_supervised() {
  # tool timeout_s log status stall_s startup_s retries backoff_s -- cmd...
  local tool="$1" timeout_s="$2" log="$3" status="$4" stall_s="$5" startup_s="$6" retries="$7" backoff_s="$8"
  shift 8
  [ "${1:-}" = "--" ] && shift

  # Validate inputs so `timeout` cannot emit its own 125 (bad duration) and the
  # arithmetic below cannot fail under set -u.
  case "$timeout_s" in ''|*[!0-9]*) echo "run_supervised: invalid timeout '$timeout_s'" >&2; return 2 ;; esac
  [ "$timeout_s" -ge 1 ] || { echo "run_supervised: timeout must be >= 1s" >&2; return 2; }
  stall_s="$(_int "$stall_s" 300)"; startup_s="$(_int "$startup_s" 60)"
  retries="$(_int "$retries" 0)"; [ "$retries" -gt 10 ] && retries=10
  backoff_s="$(_int "$backoff_s" 5)"; [ "$backoff_s" -gt 60 ] && backoff_s=60

  _CODEX_STARTED="$(_now)"
  trap '_codex_on_signal' INT TERM
  local maxa=$((retries + 1)) attempt=0 rc=0 class delay jitter expn astart
  while :; do
    attempt=$((attempt + 1))
    astart="$(_fsize "$log")"   # this attempt's start offset (for classification)
    _supervise_once "$tool" "$timeout_s" "$log" "$status" "$stall_s" "$startup_s" "$attempt" "$maxa" -- "$@"
    rc=$?
    class="$(_retry_class "$rc" "$log" "$astart")"
    case "$class" in
      done) break ;;
      auth) echo "codex/$tool: authentication failed — run 'codex login', then retry. Not auto-retrying." >&2; break ;;
      fatal) break ;;
      retry) : ;;
    esac
    [ "$attempt" -ge "$maxa" ] && break
    # Optional retry precondition (e.g. reset a dirty working tree to a clean
    # checkpoint). If it can't be satisfied, do NOT retry — retrying over a
    # half-written tree is unsafe.
    if [ -n "${CODEX_RETRY_HOOK:-}" ] && ! "$CODEX_RETRY_HOOK"; then
      echo "codex/$tool: retry precondition failed; not retrying." >&2; break
    fi
    # Exponential backoff with jitter, capped, so retries don't hammer a
    # rate-limited API in lock-step. Cap the exponent (retries<=10) and saturate
    # BEFORE and AFTER jitter so the total never exceeds 60s or overflows.
    expn=$((attempt - 1)); [ "$expn" -gt 6 ] && expn=6
    delay=$(( backoff_s * (1 << expn) ))
    [ "$delay" -gt 60 ] && delay=60
    jitter=$(( backoff_s > 0 ? RANDOM % backoff_s : 0 ))
    delay=$(( delay + jitter )); [ "$delay" -gt 60 ] && delay=60
    printf '[%s] codex/%s attempt %s hit exit %s (%s); retrying in %ss...\n' \
      "$(date +%H:%M:%S)" "$tool" "$attempt" "$rc" "$class" "$delay"
    _write_status "$status" "$tool" "retrying" "$attempt" "$maxa" "null" 0 0 0 "$rc" "retry" ""
    sleep "$delay"
  done
  # Intentionally leave the INT/TERM trap installed: it must still cover the
  # caller's post-return work (e.g. codex-implement writing <done>) so a signal
  # in that window can't leave a watcher without a completion marker. _kill_tree
  # is a no-op once _CODEX_CURRENT_TPID is cleared.
  return "$rc"
}

# --- preflight -------------------------------------------------------------
# Fail fast on obvious pre-launch problems. Optional arg: a required schema file.
preflight() {
  local real_home="${CODEX_HOME:-${HOME:-}/.codex}"
  if [ "$real_home" = "/.codex" ] || [ ! -f "$real_home/auth.json" ]; then
    echo "codex preflight: WARNING no auth.json under '$real_home' — Codex may fail to authenticate." >&2
  fi
  if [ -n "${1:-}" ] && [ ! -f "$1" ]; then
    echo "codex preflight: required schema not found: $1" >&2
    return 2
  fi
  return 0
}
