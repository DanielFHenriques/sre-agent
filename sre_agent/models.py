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
    status: str = "pending_approval"


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
class AgentResult:
    incident_id: str
    stop_reason: str
    steps: int
    assessment: dict[str, Any] | None
    proposed_actions: list[ProposedAction] = field(default_factory=list)
    tool_log: list[ToolCallRecord] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
