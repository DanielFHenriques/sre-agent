"""Canned MCP responses for two incidents that look identical from the pager.

Both page as "checkout-api 5xx error rate above 5%". They differ in the evidence:
  - bad_deploy:          a deploy 3 minutes before onset introduced a new exception. Fix: roll back.
  - dependency_outage:   no recent change; payments-gateway is failing. Rolling back would not help.
  - deploy_red_herring:  a harmless deploy lands just before a traffic spike exhausts the DB pool.
                         The evidence the agent looks at first says "roll back"; that fix fails, and
                         only a wider look (traffic, saturation) finds the real cause.
"""

from __future__ import annotations

from typing import Any

TENANT = "acme-corp"
SERVICE = "checkout-api"

_INCIDENT = {
    "incident_id": "PD-1001",
    "title": "[Datadog] checkout-api 5xx error rate above 5%",
    "service_id": SERVICE,
    "urgency": "high",
    "status": "triggered",
    "triggered_at": "2026-10-16T03:02:00Z",
    "onset_estimate": "2026-10-16T02:54:00Z",
    "now": "2026-10-16T03:05:00Z",
    "alert": {"monitor": "checkout-api error rate", "threshold": 0.05, "value": 0.21},
}

_SERVICE_CONTEXT = {
    "service_id": SERVICE,
    "owning_team": "team-checkout",
    "business_kpi": "successful checkouts per minute",
    "on_call": {"primary": "responder@acme.example", "escalation_policy": "checkout-ep"},
    "upstream": ["web-frontend", "mobile-bff"],
    "downstream": ["payments-gateway", "inventory-svc", "checkout-db"],
    "runbook_url": "https://runbooks.acme.example/checkout-api",
}

_RUNBOOK = {
    "service_id": SERVICE,
    "failure_modes": [
        {
            "symptom": "5xx spike right after a deploy",
            "remediation": "rollback_deploy to the previous version; confirm error_rate recovers within 10 min",
        },
        {
            "symptom": "Timeouts calling payments-gateway",
            "remediation": "toggle_feature_flag checkout.payments_async_queue=true (queues payments for retry); "
            "page team-payments",
        },
        {"symptom": "db_pool_usage above 90%", "remediation": "scale_out to 12 replicas"},
    ],
}


def _metric(baseline: float, current: float, change_point: str | None) -> dict[str, Any]:
    return {"baseline": baseline, "current": current, "change_point": change_point}


SCENARIOS: dict[str, dict[str, Any]] = {
    "bad_deploy": {
        "pagerduty_get_incident": _INCIDENT,
        "pagerduty_get_service_context": _SERVICE_CONTEXT,
        "changes_list_recent": {
            "changes": [
                {
                    "type": "deploy",
                    "deploy_id": "dep-7731",
                    "version": "v2.14.0",
                    "previous_version": "v2.13.4",
                    "at": "2026-10-16T02:51:00Z",
                    "author": "ci-bot",
                    "summary": "Refactor PaymentRequestMapper; add BNPL fields",
                },
                {
                    "type": "deploy",
                    "deploy_id": "dep-7702",
                    "version": "v2.13.4",
                    "previous_version": "v2.13.3",
                    "at": "2026-10-15T16:10:00Z",
                    "author": "ci-bot",
                    "summary": "Bump http client",
                },
            ]
        },
        "datadog_list_triggered_monitors": {
            "monitors": [{"name": "checkout-api error rate", "service": SERVICE, "since": "2026-10-16T02:56:00Z"}]
        },
        "datadog_query_metrics": {
            "business_kpi": _metric(950, 760, "2026-10-16T02:54:00Z"),
            "error_rate": _metric(0.004, 0.21, "2026-10-16T02:54:00Z"),
            "latency_p99": _metric(420, 460, None),
            "request_rate": _metric(1800, 1790, None),
            "cpu": _metric(0.41, 0.43, None),
            "memory": _metric(0.55, 0.56, None),
            "db_pool_usage": _metric(0.35, 0.33, None),
        },
        "datadog_search_logs": {
            "patterns": [
                {
                    "template": "NullPointerException at PaymentRequestMapper.map(PaymentRequestMapper.java:88) "
                    "version=v2.14.0",
                    "count": 5120,
                    "first_seen": "2026-10-16T02:53:40Z",
                },
                {"template": "Retrying inventory reservation", "count": 40, "first_seen": "2026-10-14T09:00:00Z"},
            ]
        },
        "datadog_get_dependency_health": {
            "dependencies": [
                {"service": "payments-gateway", "error_rate": 0.002, "baseline_error_rate": 0.002, "p99_ms": 310},
                {"service": "inventory-svc", "error_rate": 0.001, "baseline_error_rate": 0.001, "p99_ms": 90},
                {"service": "checkout-db", "error_rate": 0.0, "baseline_error_rate": 0.0, "p99_ms": 12},
            ]
        },
        "pagerduty_search_similar_incidents": {
            "incidents": [
                {
                    "incident_id": "PD-882",
                    "title": "checkout-api 5xx after release v2.9.0",
                    "root_cause": "Serialization bug in new release",
                    "resolution": "rollback_deploy; recovered in 6 min",
                }
            ]
        },
        "runbook_get": _RUNBOOK,
    },
    "dependency_outage": {
        "pagerduty_get_incident": _INCIDENT,
        "pagerduty_get_service_context": _SERVICE_CONTEXT,
        "changes_list_recent": {
            "changes": [
                {
                    "type": "deploy",
                    "deploy_id": "dep-7702",
                    "version": "v2.13.4",
                    "previous_version": "v2.13.3",
                    "at": "2026-10-15T16:10:00Z",
                    "author": "ci-bot",
                    "summary": "Bump http client",
                }
            ]
        },
        "datadog_list_triggered_monitors": {
            "monitors": [
                {"name": "checkout-api error rate", "service": SERVICE, "since": "2026-10-16T02:56:00Z"},
                {"name": "payments-gateway p99 latency", "service": "payments-gateway", "since": "2026-10-16T02:52:00Z"},
            ]
        },
        "datadog_query_metrics": {
            "business_kpi": _metric(950, 740, "2026-10-16T02:54:00Z"),
            "error_rate": _metric(0.004, 0.19, "2026-10-16T02:54:00Z"),
            "latency_p99": _metric(420, 5100, "2026-10-16T02:53:00Z"),
            "request_rate": _metric(1800, 1810, None),
            "cpu": _metric(0.41, 0.38, None),
            "memory": _metric(0.55, 0.57, None),
            "db_pool_usage": _metric(0.35, 0.36, None),
        },
        "datadog_search_logs": {
            "patterns": [
                {
                    "template": "Upstream timeout calling payments-gateway POST /v1/charges after 5000ms",
                    "count": 4380,
                    "first_seen": "2026-10-16T02:53:10Z",
                },
                {"template": "Retrying inventory reservation", "count": 38, "first_seen": "2026-10-14T09:00:00Z"},
            ]
        },
        "datadog_get_dependency_health": {
            "dependencies": [
                {"service": "payments-gateway", "error_rate": 0.41, "baseline_error_rate": 0.002, "p99_ms": 5000},
                {"service": "inventory-svc", "error_rate": 0.001, "baseline_error_rate": 0.001, "p99_ms": 92},
                {"service": "checkout-db", "error_rate": 0.0, "baseline_error_rate": 0.0, "p99_ms": 12},
            ]
        },
        "pagerduty_search_similar_incidents": {
            "incidents": [
                {
                    "incident_id": "PD-640",
                    "title": "checkout-api errors during payments-gateway brownout",
                    "root_cause": "payments-gateway degraded (provider issue)",
                    "resolution": "toggle_feature_flag checkout.payments_async_queue=true; paged team-payments",
                }
            ]
        },
        "runbook_get": _RUNBOOK,
    },
}

# What the world looks like after an approved action lands. `lag_reads` is how many metric reads
# still show the old value (the rollout takes a while). An action with no entry changes nothing.
EFFECTS: dict[str, dict[str, dict[str, Any]]] = {
    "bad_deploy": {
        "rollback_deploy": {
            "lag_reads": 1,
            "metrics": {
                "error_rate": _metric(0.004, 0.003, "2026-10-16T03:14:00Z"),
                "business_kpi": _metric(950, 945, "2026-10-16T03:14:00Z"),
            },
        },
    },
    "dependency_outage": {
        "toggle_feature_flag": {
            "lag_reads": 1,
            "metrics": {
                "error_rate": _metric(0.004, 0.008, "2026-10-16T03:13:00Z"),
                "business_kpi": _metric(950, 930, "2026-10-16T03:13:00Z"),
            },
        },
        # Rolling back a healthy service does not fix a sick dependency: no entry, nothing changes.
    },
}


SCENARIOS["deploy_red_herring"] = {
    "pagerduty_get_incident": _INCIDENT,
    "pagerduty_get_service_context": _SERVICE_CONTEXT,
    "changes_list_recent": {
        "changes": [
            {
                "type": "deploy",
                "deploy_id": "dep-7740",
                "version": "v2.15.0",
                "previous_version": "v2.14.2",
                "at": "2026-10-16T02:50:00Z",
                "author": "ci-bot",
                "summary": "Copy change on the order confirmation page",
            }
        ]
    },
    "datadog_list_triggered_monitors": {
        "monitors": [{"name": "checkout-api error rate", "service": SERVICE, "since": "2026-10-16T02:56:00Z"}]
    },
    "datadog_query_metrics": {
        "business_kpi": _metric(950, 700, "2026-10-16T02:54:00Z"),
        "error_rate": _metric(0.004, 0.17, "2026-10-16T02:54:00Z"),
        "latency_p99": _metric(420, 2900, "2026-10-16T02:53:00Z"),
        "request_rate": _metric(1800, 3400, "2026-10-16T02:52:00Z"),  # promo email went out
        "cpu": _metric(0.41, 0.72, "2026-10-16T02:52:00Z"),
        "memory": _metric(0.55, 0.58, None),
        "db_pool_usage": _metric(0.35, 0.99, "2026-10-16T02:53:00Z"),
    },
    "datadog_search_logs": {
        "patterns": [
            {
                "template": "Timeout acquiring connection from pool checkout-db after 3000ms",
                "count": 3900,
                "first_seen": "2026-10-16T02:53:50Z",
            }
        ]
    },
    "datadog_get_dependency_health": {
        "dependencies": [
            {"service": "payments-gateway", "error_rate": 0.002, "baseline_error_rate": 0.002, "p99_ms": 300},
            {"service": "inventory-svc", "error_rate": 0.001, "baseline_error_rate": 0.001, "p99_ms": 95},
            {"service": "checkout-db", "error_rate": 0.0, "baseline_error_rate": 0.0, "p99_ms": 40},
        ]
    },
    "pagerduty_search_similar_incidents": {
        "incidents": [
            {
                "incident_id": "PD-701",
                "title": "checkout-api DB pool exhaustion during Black Friday promo",
                "root_cause": "Traffic 2x normal; pool sized for baseline",
                "resolution": "scale_out to 12 replicas",
            }
        ]
    },
    "runbook_get": _RUNBOOK,
}

EFFECTS["deploy_red_herring"] = {
    # The rollback is not in here: it runs fine and changes nothing.
    "scale_out": {
        "lag_reads": 2,
        "metrics": {
            "error_rate": _metric(0.004, 0.005, "2026-10-16T03:24:00Z"),
            "db_pool_usage": _metric(0.35, 0.62, "2026-10-16T03:24:00Z"),
            "business_kpi": _metric(950, 1780, "2026-10-16T03:24:00Z"),
        },
    },
}
