"""Connectors: how the agent talks to whatever a client has integrated.

The model only ever sees capabilities (metrics, logs, dependency health, alerts, changes, support
cases). For each service, the client's integrations decide which vendor answers each capability, and
the integration's config says how that client names things (which query is their business KPI, etc.).
A connector translates one capability call into that vendor's MCP tool and query, and normalizes the
answer back into the agent's shape, tagged with its source.

If a client has a connector, the agent knows how to use it; if not, the capability is simply absent
for that service, and the agent is told so instead of guessing.

Adding a vendor = one connector class here, registered in CONNECTORS. Nothing else changes: triage,
verification, memory and guardrails all work on the normalized shapes.
"""

from __future__ import annotations

from typing import Any

from sre_agent.tools.executor_types import MCPClient

# What an integration can provide, as named in the integration record, and the capability it serves.
CAPABILITIES = {
    "metrics": "metrics",
    "logs": "logs",
    "apm": "dependency_health",
    "monitors": "alerts",
    "deploys": "changes",
    "support_cases": "support_cases",
}


class ConnectorError(Exception):
    """The integration exists but can't answer this (e.g. the client never mapped that metric)."""


class Connector:
    vendor = ""
    server = ""  # the vendor's MCP server

    def __init__(self, config: dict[str, Any]):
        self.config = config or {}

    def call(self, capability: str, args: dict[str, Any], mcp: MCPClient, tenant_id: str, timeout_s: float) -> dict[str, Any]:
        handler = getattr(self, capability, None)
        if handler is None:
            raise ConnectorError(f"{self.vendor} connector does not implement {capability}")
        result = handler(args, lambda tool, a: mcp.call_tool(self.server, tool, a, tenant_id=tenant_id, timeout_s=timeout_s))
        return {**result, "source": self.vendor}

    def _metric_query(self, metric: str) -> str:
        query = self.config.get("metric_map", {}).get(metric)
        if not query:
            raise ConnectorError(f"no '{metric}' mapping in this service's {self.vendor} integration")
        return query


class DatadogConnector(Connector):
    vendor, server = "datadog", "datadog"

    def metrics(self, args, call):
        out = call("datadog_query_metrics", {"query": self._metric_query(args["metric"]), "lookback_minutes": args["lookback_minutes"]})
        return {"metric": args["metric"], "baseline": out["baseline"], "current": out["current"], "change_point": out["change_point"]}

    def logs(self, args, call):
        out = call("datadog_search_logs", {"service": args["service_id"], "status": args["level"],
                                           "query": args["query"], "lookback_minutes": args["lookback_minutes"]})
        return {"patterns": out["patterns"]}

    def dependency_health(self, args, call):
        return {"dependencies": call("datadog_get_dependency_health", {"service": args["service_id"]})["dependencies"]}

    def alerts(self, args, call):
        return {"monitors": call("datadog_list_triggered_monitors", {"service": args["service_id"]})["monitors"]}


class NewRelicConnector(Connector):
    """Different query language (NRQL) and response shapes, same capabilities."""

    vendor, server = "newrelic", "newrelic"

    def metrics(self, args, call):
        out = call("nrql_query", {"nrql": f"{self._metric_query(args['metric'])} SINCE {args['lookback_minutes']} minutes ago"})
        r = out["results"][0]
        return {"metric": args["metric"], "baseline": r["baseline"], "current": r["latest"], "change_point": r.get("changePoint")}

    def logs(self, args, call):
        app = self.config.get("app_name", args["service_id"])
        out = call("nrql_query", {"nrql": f"SELECT count(*) FROM Log WHERE appName = '{app}' AND level = '{args['level']}' "
                                          f"FACET message SINCE {args['lookback_minutes']} minutes ago"})
        return {"patterns": [{"template": f["name"], "count": f["count"], "first_seen": f["earliest"]} for f in out["facets"]]}

    def dependency_health(self, args, call):
        out = call("service_map", {"app_name": self.config.get("app_name", args["service_id"])})
        return {"dependencies": [
            {"service": e["name"], "error_rate": e["errorRate"], "baseline_error_rate": e["baselineErrorRate"], "p99_ms": e["p99Ms"]}
            for e in out["entities"]
        ]}

    def alerts(self, args, call):
        out = call("open_issues", {"app_name": self.config.get("app_name", args["service_id"])})
        return {"monitors": [{"name": i["title"], "service": i["entity"], "since": i["activatedAt"]} for i in out["issues"]]}


class GitHubActionsConnector(Connector):
    vendor, server = "github_actions", "changes"

    def changes(self, args, call):
        return {"changes": call("changes_list_recent", {"service_id": args["service_id"], "lookback_minutes": args["lookback_minutes"]})["changes"]}


class SalesforceConnector(Connector):
    vendor, server = "salesforce", "salesforce"

    def support_cases(self, args, call):
        return call("salesforce_search_cases", {"service_id": args["service_id"], "since": args["since"]})


CONNECTORS: dict[str, type[Connector]] = {
    c.vendor: c for c in (DatadogConnector, NewRelicConnector, GitHubActionsConnector, SalesforceConnector)
}


class Router:
    """Capability -> connector, for one service, built from its integrations (in the client's order)."""

    def __init__(self, integrations: list[dict[str, Any]]):
        self.routes: dict[str, Connector] = {}
        self.unsupported: list[str] = []  # integrations we have no connector for (yet)
        for integration in integrations:
            cls = CONNECTORS.get(integration.get("vendor", ""))
            if cls is None:
                self.unsupported.append(integration.get("vendor", "unknown"))
                continue
            connector = cls(integration.get("config", {}))
            for provided in integration.get("provides", []):
                capability = CAPABILITIES.get(provided)
                if capability and capability not in self.routes:
                    self.routes[capability] = connector

    def covered(self) -> set[str]:
        return set(self.routes)

    def call(self, capability: str, args: dict[str, Any], mcp: MCPClient, tenant_id: str, timeout_s: float) -> dict[str, Any]:
        connector = self.routes.get(capability)
        if connector is None:
            return {"error": "not_integrated",
                    "detail": f"no integration on {args.get('service_id', 'this service')} provides {capability}"}
        return connector.call(capability, args, mcp, tenant_id, timeout_s)
