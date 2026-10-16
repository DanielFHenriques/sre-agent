"""In-memory stand-in for the MCP servers (pagerduty, datadog, changes, runbooks)."""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from sre_agent.mocks.fixtures import EFFECTS, SCENARIOS, TENANT


class MockMCPClient:
    def __init__(self, scenario: str, failing_tools: set[str] | None = None):
        self.scenario = scenario
        self.data = SCENARIOS[scenario]
        self.failing_tools = failing_tools or set()
        self.calls: list[dict[str, Any]] = []
        self._pending_metrics: dict[str, dict[str, Any]] = {}
        self._lag_reads = 0

    def apply_effect(self, action: str) -> None:
        """Called by the mock action runner once a human-approved action has run."""
        effect = EFFECTS[self.scenario].get(action)
        if effect:
            self._pending_metrics.update(effect["metrics"])
            self._lag_reads = effect["lag_reads"]

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
        if tool == "salesforce_search_cases":
            return _aggregate_cases(payload, arguments["service_id"], arguments["since"])
        if tool == "datadog_query_metrics":
            metric = arguments["metric"]
            if metric in self._pending_metrics:
                if self._lag_reads > 0:
                    self._lag_reads -= 1
                else:
                    payload = {**payload, metric: self._pending_metrics[metric]}
            return {"metric": metric, **payload[metric]}
        return copy.deepcopy(payload)


def _aggregate_cases(payload: dict[str, Any], service_id: str, since: str) -> dict[str, Any]:
    """What a well-built Salesforce MCP tool returns: counts at the source plus a small sample."""
    since_ts = datetime.fromisoformat(since.replace("Z", "+00:00"))
    related = [
        c for c in payload["cases"]
        if c["related_service"] == service_id and datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")) >= since_ts
    ]
    return {
        "since": since,
        "related_count": len(related),
        "enterprise_count": sum(1 for c in related if c["tier"] == "enterprise"),
        "baseline_per_hour": payload["baseline_per_hour"],
        "sample": [{k: c[k] for k in ("case_id", "created_at", "tier", "subject")} for c in related[:3]],
    }
