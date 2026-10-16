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
    mcp_server: str | None = None  # a fixed server (PagerDuty, runbooks, the agent's memory); None: local
    timeout_s: float = 5.0
    # Integration-agnostic tools name a capability instead of a server: the service's integrations decide
    # which vendor's connector answers (see connectors.py).
    capability: str | None = None

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
    ToolDef(specs.SEARCH_SUPPORT_CASES, SideEffect.READ, capability="support_cases"),
    ToolDef(specs.LIST_RECENT_CHANGES, SideEffect.READ, capability="changes"),
    ToolDef(specs.LIST_TRIGGERED_MONITORS, SideEffect.READ, capability="alerts"),
    ToolDef(specs.QUERY_METRICS, SideEffect.READ, capability="metrics"),
    ToolDef(specs.SEARCH_LOGS, SideEffect.READ, timeout_s=10.0, capability="logs"),
    ToolDef(specs.GET_DEPENDENCY_HEALTH, SideEffect.READ, capability="dependency_health"),
    ToolDef(specs.SEARCH_SIMILAR_INCIDENTS, SideEffect.READ, "pagerduty"),
    ToolDef(specs.GET_RUNBOOK, SideEffect.READ, "runbooks"),
    # First-party: served by the agent's own memory store, not an MCP server. Still tenant-injected.
    ToolDef(specs.MEMORY_RECALL, SideEffect.READ, "agent_memory"),
    ToolDef(specs.PROPOSE_ACTION, SideEffect.APPROVAL_REQUIRED),
    ToolDef(specs.SUBMIT_ASSESSMENT, SideEffect.TERMINAL),
]

REGISTRY: dict[str, ToolDef] = {t.name: t for t in _TOOLS}


def openai_tools(covered: set[str] | None = None) -> list[dict[str, Any]]:
    """The `tools=[...]` argument for chat.completions.create.

    With `covered`, capability tools are offered only if this service has an integration providing them.
    """
    return [t.spec for t in _TOOLS if t.capability is None or covered is None or t.capability in covered]
