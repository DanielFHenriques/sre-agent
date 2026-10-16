from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SideEffect(str, Enum):
    READ = "read"
    # Recorded as a pending request for a human. Never executed by this system.
    APPROVAL_REQUIRED = "approval_required"
    # Ends the loop; handled locally, no external call.
    TERMINAL = "terminal"


@dataclass(frozen=True)
class IncidentRef:
    """What the trigger webhook gives us. tenant_id comes from the authenticated webhook, never the model."""

    incident_id: str
    tenant_id: str
    service_id: str


@dataclass
class ProposedAction:
    action: str
    params: dict[str, Any]
    rationale: str
    evidence: list[str]
    risk: dict[str, Any]
    expected_effect: str
    how_to_verify: dict[str, Any] | None  # machine-checkable, so a verify step can run it later
    # pending_approval -> approved -> executed | execution_failed ; or pending_approval -> rejected
    status: str = "pending_approval"
    decided_by: str | None = None
    verification_verdict: str | None = None
    # Decided by code (triage.fast_track_reason), never by the model. Still needs a human's approval.
    fast_track: bool = False
    fast_track_reason: str | None = None


@dataclass
class Verification:
    """Did the approved action work? Decided by code from the metric, not by the model."""

    action: str
    verdict: str  # worked | did_not_work | unknown | not_executed | not_applicable
    detail: str
    readings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolCallRecord:
    """One line of the agent's own call log (audit trail)."""

    tool: str
    arguments: dict[str, Any]
    status: str  # ok | error | rejected | pending_approval
    latency_ms: float
    output_chars: int
    error: str | None = None


@dataclass
class TimelineEntry:
    """Scribe-style: what was found or decided, by whom, when. Not a dictation of every tool call."""

    at: str  # ISO 8601, UTC
    kind: str  # triggered | impact | hypothesis | proposed | approved | rejected | executed | verified | reinvestigating | escalated | stopped_early | restarted | handover
    text: str
    actor: str  # "agent", "automation", or the human's id


@dataclass
class AgentResult:
    """Everything about one incident's investigation. Also the unit that gets persisted.

    stop_reason "in_progress" means a checkpoint taken mid-investigation (the process may have died).
    """

    incident_id: str
    stop_reason: str
    steps: int
    assessment: dict[str, Any] | None
    proposed_actions: list[ProposedAction] = field(default_factory=list)
    tool_log: list[ToolCallRecord] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    cycle: int = 1  # 1 = first investigation, 2+ = re-investigation after a failed fix
    timeline: list[TimelineEntry] = field(default_factory=list)
    triage: dict[str, Any] | None = None  # severity, priority, response mode; see triage.py
    version: int = 0  # bumped on every save; used to detect two writers (optimistic concurrency)
