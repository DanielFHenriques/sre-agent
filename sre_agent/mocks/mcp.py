"""In-memory stand-in for the MCP servers (pagerduty, datadog, changes, runbooks)."""

from __future__ import annotations

import copy
from typing import Any

from sre_agent.mocks.fixtures import SCENARIOS, TENANT


class MockMCPClient:
    def __init__(self, scenario: str, failing_tools: set[str] | None = None):
        self.data = SCENARIOS[scenario]
        self.failing_tools = failing_tools or set()
        self.calls: list[dict[str, Any]] = []

    def call_tool(
        self, server: str, tool: str, arguments: dict[str, Any], *, tenant_id: str, timeout_s: float
    ) -> dict[str, Any]:
        self.calls.append({"server": server, "tool": tool, "arguments": arguments, "tenant_id": tenant_id})
        if tenant_id != TENANT:
            # A real server would scope by tenant credentials; here other tenants simply have no data.
            raise PermissionError(f"no access for tenant '{tenant_id}'")
        if tool in self.failing_tools:
            raise TimeoutError(f"{server}.{tool} timed out")
        payload = self.data[tool]
        if tool == "datadog_query_metrics":
            return {"metric": arguments["metric"], **payload[arguments["metric"]]}
        return copy.deepcopy(payload)
