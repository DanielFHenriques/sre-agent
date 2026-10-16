"""In-memory stand-in for the MCP servers: PagerDuty, runbooks, and each vendor's own server
(Datadog, New Relic, the deploy tool, Salesforce), each speaking its own query language and shapes."""

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
        if server == "newrelic":
            return self._newrelic(tool, arguments)
        payload = self.data[tool]
        if tool == "salesforce_search_cases":
            return _aggregate_cases(payload, arguments["service_id"], arguments["since"])
        if tool == "datadog_query_metrics":
            return self._metric(self._metric_for(arguments["query"], "datadog"))
        return copy.deepcopy(payload)

    def _metric(self, metric: str) -> dict[str, Any]:
        payload = self.data["datadog_query_metrics"]
        if metric in self._pending_metrics:
            if self._lag_reads > 0:
                self._lag_reads -= 1
            else:
                payload = {**payload, metric: self._pending_metrics[metric]}
        return {"metric": metric, **payload[metric]}

    def _metric_for(self, query: str, vendor: str) -> str:
        """The vendor only sees its own query; map it back via the client's integration config."""
        for integration in self.data["pagerduty_list_service_integrations"]["integrations"]:
            if integration["vendor"] == vendor:
                for metric, mapped in integration["config"]["metric_map"].items():
                    if query.startswith(mapped):
                        return metric
        raise ValueError(f"{vendor}: unknown query {query!r}")

    def _newrelic(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """New Relic's shapes: NRQL results and facets, service map entities, issues."""
        if tool == "nrql_query":
            nrql = arguments["nrql"]
            if " FROM Log " in nrql:
                return {"facets": [{"name": p["template"], "count": p["count"], "earliest": p["first_seen"]}
                                   for p in self.data["datadog_search_logs"]["patterns"]]}
            m = self._metric(self._metric_for(nrql, "newrelic"))
            return {"results": [{"baseline": m["baseline"], "latest": m["current"], "changePoint": m["change_point"]}]}
        if tool == "service_map":
            return {"entities": [{"name": d["service"], "errorRate": d["error_rate"],
                                  "baselineErrorRate": d["baseline_error_rate"], "p99Ms": d["p99_ms"]}
                                 for d in self.data["datadog_get_dependency_health"]["dependencies"]]}
        if tool == "open_issues":
            return {"issues": [{"title": m["name"], "entity": m["service"], "activatedAt": m["since"]}
                               for m in self.data["datadog_list_triggered_monitors"]["monitors"]]}
        raise ValueError(f"newrelic: unknown tool {tool}")


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
