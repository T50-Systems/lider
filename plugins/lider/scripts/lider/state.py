"""lider.state - atomic state management with generation IDs.

Provides a reliable way to persist and validate state across engine invocations,
preventing stale-state bugs and enabling safe crash recovery.
"""
import json
import os
import tempfile
import time
from pathlib import Path


DEFAULT_STATE_PATH = ".lider/STATE.md"
DEFAULT_CONTEXT_PATH = ".lider/CONTEXT.md"
MAX_CONTEXT_SIZE = 1024  # 1KB limit for CONTEXT.md


def get_state_path(root=None):
    """Get the full path to the state file."""
    if root is None:
        root = os.getcwd()
    return os.path.join(root, DEFAULT_STATE_PATH)


def get_context_path(root=None):
    """Get the full path to the context file."""
    if root is None:
        root = os.getcwd()
    return os.path.join(root, DEFAULT_CONTEXT_PATH)


def ensure_lider_dir(root=None):
    """Ensure the .lider directory exists."""
    if root is None:
        root = os.getcwd()
    lider_dir = os.path.join(root, ".lider")
    os.makedirs(lider_dir, exist_ok=True)
    return lider_dir


def read_state(root=None):
    """Read and validate STATE.md, returning the state dict or None if invalid/missing."""
    path = get_state_path(root)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        # Validate required fields
        required = ["generation_id", "updated_at", "phase"]
        if not all(k in state for k in required):
            return None
        return state
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def write_state_atomic(state, root=None):
    """Write STATE.md atomically using temp file + rename."""
    path = get_state_path(root)
    ensure_lider_dir(root)
    # Ensure required fields
    if "generation_id" not in state:
        state["generation_id"] = 1
    if "updated_at" not in state:
        state["updated_at"] = int(time.time())
    if "phase" not in state:
        state["phase"] = "unknown"
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
        return True
    except OSError:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


def increment_generation(state):
    """Increment the generation ID and update timestamp."""
    state["generation_id"] = state.get("generation_id", 0) + 1
    state["updated_at"] = int(time.time())
    return state


def validate_generation(state, expected_gen):
    """Check if state generation matches expected. Returns (valid, message)."""
    if state is None:
        return False, "STATE.md missing or unreadable"
    actual = state.get("generation_id", 0)
    if actual != expected_gen:
        return False, f"STATE.md generation mismatch: expected {expected_gen}, got {actual}"
    return True, "OK"


def load_context(root=None):
    """Load CONTEXT.md if it exists and is within size limit."""
    path = get_context_path(root)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        if len(content) > MAX_CONTEXT_SIZE:
            # Truncate with a marker
            return content[:MAX_CONTEXT_SIZE] + "\n... [truncated]"
        return content
    except OSError:
        return None


def write_context(content, root=None):
    """Write CONTEXT.md atomically."""
    path = get_context_path(root)
    ensure_lider_dir(root)
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
        return True
    except OSError:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


def create_initial_state(phase="discuss", root=None):
    """Create a fresh initial state."""
    state = {
        "generation_id": 1,
        "updated_at": int(time.time()),
        "phase": phase,
        "decisions": [],
        "open_questions": [],
        "verified_facts": [],
        "metadata": {
            "engine_used": None,
            "total_cost_usd": None,
            "tokens_used": None,
        }
    }
    write_state_atomic(state, root)
    return state


def add_decision(state, decision_id, content, source=None):
    """Add a decision to the state."""
    state["decisions"].append({
        "id": decision_id,
        "content": content,
        "source": source,
        "at": int(time.time()),
    })
    return state


def add_open_question(state, question_id, content, blocking_unit=None):
    """Add an open question to the state."""
    state["open_questions"].append({
        "id": question_id,
        "content": content,
        "blocking_unit": blocking_unit,
        "at": int(time.time()),
    })
    return state


def add_verified_fact(state, fact, validated_by=None):
    """Add a verified fact to the state."""
    state["verified_facts"].append({
        "fact": fact,
        "validated_by": validated_by,
        "at": int(time.time()),
    })
    return state


def update_metadata(state, engine_used=None, cost_usd=None, tokens_used=None):
    """Update metadata fields if provided."""
    if engine_used is not None:
        state["metadata"]["engine_used"] = engine_used
    if cost_usd is not None:
        state["metadata"]["total_cost_usd"] = cost_usd
    if tokens_used is not None:
        state["metadata"]["tokens_used"] = tokens_used
    return state


def get_env_context(state):
    """Extract context suitable for environment variable injection."""
    if state is None:
        return ""
    parts = []
    if state.get("decisions"):
        parts.append("Decisions:")
        for d in state["decisions"][-3:]:  # Last 3 decisions
            parts.append(f"  - {d['id']}: {d['content'][:100]}")
    if state.get("open_questions"):
        parts.append("Open Questions:")
        for q in state["open_questions"][-3:]:
            parts.append(f"  - {q['id']}: {q['content'][:100]}")
    if state.get("verified_facts"):
        parts.append("Verified Facts:")
        for f in state["verified_facts"][-3:]:
            parts.append(f"  - {f['fact'][:100]}")
    return "\n".join(parts)[:MAX_CONTEXT_SIZE]