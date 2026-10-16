"""Tool definitions in OpenAI Chat Completions function-calling format.

All schemas use strict mode: every property is listed in `required`, optional values are
nullable, and `additionalProperties` is false. Note there is no `tenant_id` anywhere: the
executor injects it, so the model cannot address another tenant's data.
"""

from __future__ import annotations

from typing import Any

LOOKBACK = {
    "type": "integer",
    "description": "Minutes before now to look back. Anchor it on the incident onset, e.g. onset minus 30.",
}
SERVICE_ID = {"type": "string", "description": "Service id from the incident, e.g. 'checkout-api'."}


def function_tool(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


# --- Read-only context tools (each maps to an MCP server tool) -------------------------------

GET_INCIDENT = function_tool(
    "pagerduty_get_incident",
    "Fetch the incident: triggering alert, service, urgency, onset time, current status.",
    {"incident_id": {"type": "string"}},
)

GET_SERVICE_CONTEXT = function_tool(
    "pagerduty_get_service_context",
    "Ownership and topology for a service: owning team, current on-call, upstream and downstream "
    "dependencies, runbook link.",
    {"service_id": SERVICE_ID},
)

LIST_RECENT_CHANGES = function_tool(
    "changes_list_recent",
    "Deploys, config changes and feature-flag flips for a service in the lookback window, newest first.",
    {"service_id": SERVICE_ID, "lookback_minutes": LOOKBACK},
)

LIST_TRIGGERED_MONITORS = function_tool(
    "datadog_list_triggered_monitors",
    "Monitors currently in alert for this service and for its dependencies. Use it to tell a "
    "service-local problem from a wider one.",
    {"service_id": SERVICE_ID},
)

METRICS = ["business_kpi", "error_rate", "latency_p99", "request_rate", "cpu", "memory", "db_pool_usage"]

QUERY_METRICS = function_tool(
    "datadog_query_metrics",
    "Time series for one service metric with a summary (baseline, current, change point). "
    "Look for step changes and when they started, not just high values. 'business_kpi' is the "
    "service's business metric (named in the service context); use it to size customer impact.",
    {
        "service_id": SERVICE_ID,
        "metric": {"type": "string", "enum": METRICS},
        "lookback_minutes": LOOKBACK,
    },
)

SEARCH_LOGS = function_tool(
    "datadog_search_logs",
    "Search the service's application logs. Returns error patterns grouped by message template, "
    "with count and first_seen time, not raw lines.",
    {
        "service_id": SERVICE_ID,
        "level": {"type": "string", "enum": ["error", "warn"]},
        "query": {
            "type": ["string", "null"],
            "description": "Optional extra filter, Datadog log query syntax. null for none.",
        },
        "lookback_minutes": LOOKBACK,
    },
)

GET_DEPENDENCY_HEALTH = function_tool(
    "datadog_get_dependency_health",
    "APM view of the service's downstream dependencies: error rate and p99 latency per dependency, "
    "current vs baseline. Use it to decide whether the problem is ours or a dependency's.",
    {"service_id": SERVICE_ID},
)

SEARCH_SIMILAR_INCIDENTS = function_tool(
    "pagerduty_search_similar_incidents",
    "Past resolved incidents on this service with similar symptoms, including root cause and what fixed it.",
    {
        "service_id": SERVICE_ID,
        "symptoms": {"type": "string", "description": "Short description of what is observed."},
    },
)

GET_RUNBOOK = function_tool(
    "runbook_get",
    "The service's runbook: known failure modes and the approved remediations for each.",
    {"service_id": SERVICE_ID},
)

# --- Approval-gated action proposal -----------------------------------------------------------

ACTION_CATALOG = ["rollback_deploy", "restart_service", "scale_out", "toggle_feature_flag", "page_team"]

PROPOSE_ACTION = function_tool(
    "propose_action",
    "Propose ONE remediation from the approved catalog. This does NOT execute anything: it creates a "
    "request a human must approve. Fill only the parameters the action needs, null the rest.",
    {
        "action": {"type": "string", "enum": ACTION_CATALOG},
        "target_service": SERVICE_ID,
        "deploy_id": {"type": ["string", "null"], "description": "rollback_deploy: the deploy to roll back."},
        "flag_name": {"type": ["string", "null"], "description": "toggle_feature_flag: flag key."},
        "flag_value": {"type": ["boolean", "null"], "description": "toggle_feature_flag: desired value."},
        "replicas": {"type": ["integer", "null"], "description": "scale_out: target replica count."},
        "team": {"type": ["string", "null"], "description": "page_team: team id to page."},
        "rationale": {"type": "string", "description": "One or two sentences a tired human can check quickly."},
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete facts from tool results that support this action.",
        },
        "risk": {
            "type": "object",
            "description": "What could go wrong if this is approved, and how far it would spread.",
            "properties": {
                "level": {"type": "string", "enum": ["low", "medium", "high"]},
                "description": {"type": "string"},
            },
            "required": ["level", "description"],
            "additionalProperties": False,
        },
        "expected_effect": {
            "type": "string",
            "description": "What should change if this works, and how quickly.",
        },
        "how_to_verify": {
            "description": "A measurable check that the action worked, so it can be verified afterwards. "
            "Required for remediations; null for page_team.",
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string", "enum": METRICS},
                        "comparison": {"type": "string", "enum": ["below", "above"]},
                        "threshold": {"type": "number"},
                        "within_minutes": {"type": "integer"},
                    },
                    "required": ["metric", "comparison", "threshold", "within_minutes"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ],
        },
    },
)

# --- Terminal tool ----------------------------------------------------------------------------

HYPOTHESIS = {
    "type": "object",
    "properties": {
        "cause": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "evidence_for": {"type": "array", "items": {"type": "string"}},
        "evidence_against": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["cause", "confidence", "evidence_for", "evidence_against"],
    "additionalProperties": False,
}

SUBMIT_ASSESSMENT = function_tool(
    "submit_assessment",
    "Finish the investigation. Call exactly once, last. Customer impact first, then ranked hypotheses, "
    "the recommended next step, and what is still unknown (including any data source that failed).",
    {
        "summary": {"type": "string", "description": "Three sentences max, for the responder's first read."},
        "impact": {
            "type": "object",
            "description": "Size-up in business terms: who is affected and how much. Not CPU graphs.",
            "properties": {
                "business_metric": {"type": "string", "description": "e.g. 'successful checkouts per minute'."},
                "baseline": {"type": ["number", "null"]},
                "current": {"type": ["number", "null"]},
                "change_pct": {"type": ["number", "null"], "description": "Negative means a drop."},
                "scope": {"type": "string", "description": "Who or what is affected, in plain words."},
                "suggested_severity": {
                    "type": "string",
                    "enum": ["SEV-1", "SEV-2", "SEV-3", "SEV-4", "SEV-5", "unknown"],
                    "description": "A suggestion for the Incident Commander to confirm; SEV-1 is the worst.",
                },
            },
            "required": ["business_metric", "baseline", "current", "change_pct", "scope", "suggested_severity"],
            "additionalProperties": False,
        },
        "hypotheses": {"type": "array", "items": HYPOTHESIS},
        "recommended_next_step": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
)
