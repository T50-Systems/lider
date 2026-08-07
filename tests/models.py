"""The durable contracts, as executable models. Test-only - never shipped.

Three files outlive the process that wrote them and are read by something else:

  .lider/runs/<id>/run.json   the ledger - read by a resumed session, days later
  <log>.status.json           the supervisor - read by a watcher, live
  .lider/metrics.jsonl        the record - read by metrics-report, and appended
                              to by several tools that must agree on the shape

Nothing validates them at runtime, and deliberately so: the plugin has to work
with **nothing installed but Python**, which is why `rungraph.py` imports only
stdlib and why pydantic is a dev dependency that never ships. The runtime keeps
writing plain dicts.

What these models buy is the other half: a single readable definition of each
shape, checked against what the code ACTUALLY produced. That is enforcement
rather than documentation - the same distinction the plugin makes everywhere
else - and it is why every model below is **strict**. `extra="forbid"` means a
field added to the state without being added here fails the test. That is the
point: drift is caught in both directions, and a comment cannot do that.
"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict

STRICT = ConfigDict(extra="forbid")

Severity = Literal["BLOCKER", "MAJOR", "MINOR", "NIT"]
Verdict = Literal["ok", "not-ok", "undetermined"]
Decision = Literal["accept", "fix", "return", "respec", "reject", "escalate"]


class Spec(BaseModel):
    """The pinned spec. `text` is stored, not just pointed at: a ledger holding
    only a path is one `git mv` away from pointing at nothing."""
    model_config = STRICT
    path: str
    sha256: str
    bytes: int
    text: str
    at: int


class Role(BaseModel):
    model_config = STRICT
    engine: str
    model: Optional[str] = None
    family: Optional[str] = None      # None means "unknown", never "different"
    at: int
    forced: bool


class Check(BaseModel):
    model_config = STRICT
    verdict: Verdict
    evidence: str
    at: int
    node: str


class Finding(BaseModel):
    model_config = STRICT
    id: str
    round: int
    severity: Severity
    summary: str
    location: Optional[str] = None
    decision: Optional[Decision] = None
    engine: Optional[str] = None
    defect_id: str                    # stable across rounds; convergence reads it
    recurrence_of: Optional[str] = None
    rationale: Optional[str] = None
    decided_at: Optional[int] = None


class Round(BaseModel):
    model_config = STRICT
    round: int
    at: int
    ingested: int
    severe: int
    recurring: int
    severe_defects: List[str]         # identities, not just a count
    verdict: Optional[str] = None
    engine: Optional[str] = None


class Criterion(BaseModel):
    model_config = STRICT
    id: str
    text: str
    status: Literal["required", "deferred"]
    reason: Optional[str] = None      # required to defer: a descope must be visible
    at: int


class Question(BaseModel):
    model_config = STRICT
    id: str
    text: str
    status: Literal["open", "answered", "assumed"]
    answer: Optional[str] = None      # required when assumed
    unit: Optional[str] = None
    at: int


class Unit(BaseModel):
    """A unit carries the SAME shape as the run - findings, rounds, max_rounds -
    which is why every convergence rule works on either without knowing which."""
    model_config = STRICT
    id: str
    title: str
    depends_on: List[str]
    covers: List[str]
    node: Literal["pending", "implement", "review", "adjudicate",
                  "escalated", "done", "dropped"]
    path: List[str]
    findings: List[Finding]
    rounds: List[Round]
    max_rounds: int
    roles: Dict[str, Role]
    created_at: int


class Event(BaseModel):
    """Append-only audit trail. Deliberately open: an event may carry whatever
    the command that logged it found worth recording, and adding a field to one
    must not invalidate a ledger written by an older version."""
    model_config = ConfigDict(extra="allow")
    kind: str
    at: int
    node: str


class HandoffRef(BaseModel):
    """Pointer to a sealed inception handoff (construction import or seal out)."""
    model_config = STRICT
    path: str
    sha256: str
    id: Optional[str] = None
    inception_run: Optional[str] = None
    imported_at: Optional[int] = None
    at: Optional[int] = None


class InceptionHandoff(BaseModel):
    """Operational seal under .lider/handoffs/<id>.json. Self-hashed."""
    model_config = STRICT
    kind: Literal["lider.inception.handoff"]
    version: int
    id: str
    sealed_at: int
    inception_run: str
    strict: bool
    frame: Spec
    criteria: List[Criterion]
    questions: List[Question]
    units: List[Dict[str, Any]]       # slim unit rows, not full unit subgraphs
    challenge: Dict[str, Any]
    roles: Dict[str, Any]
    sha256: str


class OpsTarget(BaseModel):
    """Declared operational target. Live probes stay in check evidence rows."""
    model_config = STRICT
    env: str
    ref: str
    previous_ref: Optional[str] = None  # last known good — rollback target
    url: Optional[str] = None
    surfaces: List[str] = []
    notes: Optional[str] = None
    construction_run: Optional[str] = None
    at: int


class Run(BaseModel):
    model_config = STRICT
    schema_version: int
    run_id: str
    title: str
    kind: Literal["construction", "inception", "operations"] = "construction"
    strict: bool = False
    created_at: int
    updated_at: int
    node: str
    path: List[str]
    spec: Optional[Spec] = None
    roles: Dict[str, Role]
    checks: Dict[str, Check]
    findings: List[Finding]
    rounds: List[Round]
    units: List[Unit]
    criteria: List[Criterion]
    questions: List[Question]
    max_rounds: int
    events: List[Event]
    handoff: Optional[HandoffRef] = None
    handoff_out: Optional[HandoffRef] = None
    inception_frame: Optional[Spec] = None
    target: Optional[OpsTarget] = None


class Usage(BaseModel):
    """What an engine reported it consumed. Every field is optional and stays
    None when unreported - an unknown cost and a zero cost are opposite facts."""
    model_config = STRICT
    cost_usd: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    model_billed: Optional[str] = None
    turns: Optional[int] = None
    duration_ms: Optional[int] = None


class Status(BaseModel):
    """The live file a watcher polls. Its contract crosses a process boundary, so
    a silent change here breaks something nobody is running in this repo."""
    model_config = STRICT
    engine: str
    tool: str
    state: Literal["starting", "running", "retrying", "done", "failed", "cancelled"]
    attempt: int
    max_attempts: int
    pid: Optional[int] = None
    elapsed_s: int
    idle_s: int
    log_bytes: int
    exit: Optional[int] = None
    reason: str
    activity: str
    stall_watchdog: int               # 0 means unarmed - never mistake it for healthy
    startup_watchdog: int
    usage: Optional[Usage] = None
    started_at: int
    updated_at: int


class MetricRow(BaseModel):
    """One recorded event. Open by design - each `kind` carries its own fields,
    and `metrics.record` must never reject a caller that knows something new."""
    model_config = ConfigDict(extra="allow")
    v: int
    kind: str
    at: int
