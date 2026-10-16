"""Which tools exist, what kind of side effect they have, and which MCP server backs them.

The side-effect tag here, not the prompt, decides what the executor does with a call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sre_agent.models import SideEffect
from sre_agent.tools import specs


@dataclass(frozen=True)
class ToolDef:
    spec: dict[str, Any]
    side_effect: SideEffect
    mcp_server: str | None = None  # None: handled locally by the executor
    timeout_s: float = 5.0

    @property
    def name(self) -> str:
        return self.spec["function"]["name"]

    @property
    def parameters(self) -> dict[str, Any]:
        return self.spec["function"]["parameters"]


_TOOLS = [
    ToolDef(specs.GET_INCIDENT, SideEffect.READ, "pagerduty"),
    ToolDef(specs.GET_SERVICE_CONTEXT, SideEffect.READ, "pagerduty"),
    ToolDef(specs.LIST_SERVICE_INTEGRATIONS, SideEffect.READ, "pagerduty"),
    ToolDef(specs.SEARCH_SUPPORT_CASES, SideEffect.READ, "salesforce"),
    ToolDef(specs.LIST_RECENT_CHANGES, SideEffect.READ, "changes"),
    ToolDef(specs.LIST_TRIGGERED_MONITORS, SideEffect.READ, "datadog"),
    ToolDef(specs.QUERY_METRICS, SideEffect.READ, "datadog"),
    ToolDef(specs.SEARCH_LOGS, SideEffect.READ, "datadog", timeout_s=10.0),
    ToolDef(specs.GET_DEPENDENCY_HEALTH, SideEffect.READ, "datadog"),
    ToolDef(specs.SEARCH_SIMILAR_INCIDENTS, SideEffect.READ, "pagerduty"),
    ToolDef(specs.GET_RUNBOOK, SideEffect.READ, "runbooks"),
    ToolDef(specs.PROPOSE_ACTION, SideEffect.APPROVAL_REQUIRED),
    ToolDef(specs.SUBMIT_ASSESSMENT, SideEffect.TERMINAL),
]

REGISTRY: dict[str, ToolDef] = {t.name: t for t in _TOOLS}


def openai_tools() -> list[dict[str, Any]]:
    """The `tools=[...]` argument for chat.completions.create."""
    return [t.spec for t in _TOOLS]
