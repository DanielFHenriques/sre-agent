"""Canned MCP responses for two incidents that look identical from the pager.

Both page as "checkout-api 5xx error rate above 5%". They differ in the evidence:
  - bad_deploy:          a deploy 3 minutes before onset introduced a new exception. Fix: roll back.
  - dependency_outage:   no recent change; payments-gateway is failing. Rolling back would not help.
  - deploy_red_herring:  a harmless deploy lands just before a traffic spike exhausts the DB pool.
                         The evidence the agent looks at first says "roll back"; that fix fails, and
                         only a wider look (traffic, saturation) finds the real cause.
  - low_sev_recurring:   a release breaks the gift-card balance endpoint again. Barely any business
                         impact, no customer cases, and the same rollback fixed it twice before:
                         a candidate for a fast-tracked (one-click) rollback.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    # PagerDuty's responder roster for this incident. Approvals are checked against it.
    "responders": [
        {"user": "alice", "role": "incident_commander"},
        {"user": "bob", "role": "responder"},
        {"user": "carol", "role": "responder"},
    ],
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
                    "resolution_action": "rollback_deploy",
                    "verified": True,
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
                    "resolution_action": "toggle_feature_flag",
                    "verified": True,
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
                "resolution_action": "scale_out",
                "verified": True,
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


# --- Integrations and support cases ----------------------------------------------------------

# The client's integrations for checkout-api, as PagerDuty knows them. `config` is what the client set up
# when connecting: how *their* metrics are named in that vendor. The agent's connectors use it.
_DATADOG = {
    "vendor": "datadog",
    "provides": ["metrics", "logs", "apm", "monitors"],
    "config": {"metric_map": {
        "business_kpi": "sum:checkout.orders.completed{service:checkout-api}.as_rate()",
        "error_rate": "sum:trace.http.request.errors{service:checkout-api} / sum:trace.http.request.hits{service:checkout-api}",
        "latency_p99": "p99:trace.http.request.duration{service:checkout-api}",
        "request_rate": "sum:trace.http.request.hits{service:checkout-api}.as_rate()",
        "cpu": "avg:container.cpu.usage{service:checkout-api}",
        "memory": "avg:container.memory.usage{service:checkout-api}",
        "db_pool_usage": "avg:hikaricp.connections.usage{service:checkout-api}",
    }},
}
_NEWRELIC = {
    "vendor": "newrelic",
    "provides": ["metrics", "logs", "apm", "monitors"],
    "config": {"app_name": "checkout-api-prod", "metric_map": {
        "business_kpi": "SELECT rate(count(*), 1 minute) FROM OrderCompleted WHERE appName = 'checkout-api-prod'",
        "error_rate": "SELECT percentage(count(*), WHERE error IS true) / 100 FROM Transaction WHERE appName = 'checkout-api-prod'",
        "latency_p99": "SELECT percentile(duration, 99) * 1000 FROM Transaction WHERE appName = 'checkout-api-prod'",
        "request_rate": "SELECT rate(count(*), 1 minute) FROM Transaction WHERE appName = 'checkout-api-prod'",
        "cpu": "SELECT average(cpuPercent) / 100 FROM ProcessSample WHERE apmApplicationNames LIKE '%checkout-api-prod%'",
        "memory": "SELECT average(memoryUsedPercent) / 100 FROM ProcessSample WHERE apmApplicationNames LIKE '%checkout-api-prod%'",
        "db_pool_usage": "SELECT average(custom.hikari.usage) FROM Metric WHERE appName = 'checkout-api-prod'",
    }},
}
_REST = [
    {"vendor": "github_actions", "provides": ["deploys"]},
    {"vendor": "launchdarkly", "provides": ["feature_flags"]},  # no connector yet: listed, not usable
    {"vendor": "salesforce", "provides": ["support_cases"]},
]
_INTEGRATIONS = {"integrations": [_DATADOG, *_REST]}
_NEWRELIC_INTEGRATIONS = {"integrations": [_NEWRELIC, *_REST]}

# Noise every window has: a case unrelated to this service, opened before onset.
_UNRELATED_CASE = {
    "case_id": "500-0091", "created_at": "2026-10-16T02:31:00Z", "account": "Globex", "tier": "standard",
    "subject": "Invoice PDF missing line items", "related_service": "billing-api",
}


def _cases(n: int, subject: str, enterprise: int) -> dict[str, Any]:
    """n related cases opened after the 02:54 onset, `enterprise` of them from enterprise accounts.
    This is the raw case list the mock Salesforce server aggregates over (see mocks/mcp.py)."""
    first = datetime(2026, 10, 16, 2, 56, tzinfo=timezone.utc)
    cases = [
        {
            "case_id": f"500-{1100 + i}",
            "created_at": (first + timedelta(seconds=17 * i)).isoformat().replace("+00:00", "Z"),
            "account": f"Customer {i + 1}",
            "tier": "enterprise" if i < enterprise else "standard",
            "subject": subject,
            "related_service": SERVICE,
        }
        for i in range(n)
    ]
    return {"baseline_per_hour": 1.5, "cases": [_UNRELATED_CASE, *cases]}


for _name, _cases_result in {
    "bad_deploy": _cases(14, "Checkout fails with 'something went wrong'", enterprise=2),
    "dependency_outage": _cases(22, "Payment declined at checkout, card is fine", enterprise=3),
    "deploy_red_herring": _cases(9, "Checkout very slow then times out", enterprise=1),
}.items():
    SCENARIOS[_name]["pagerduty_list_service_integrations"] = _INTEGRATIONS
    SCENARIOS[_name]["salesforce_search_cases"] = _cases_result


# --- Low severity, recurring: the fast-track case ---------------------------------------------

SCENARIOS["low_sev_recurring"] = {
    "pagerduty_get_incident": {
        **_INCIDENT,
        "incident_id": "PD-1001",
        "title": "[Datadog] checkout-api gift-card balance endpoint error rate above 1%",
        "urgency": "low",
        "alert": {"monitor": "checkout-api gift-card errors", "threshold": 0.01, "value": 0.02},
    },
    "pagerduty_get_service_context": _SERVICE_CONTEXT,
    "pagerduty_list_service_integrations": _INTEGRATIONS,
    "changes_list_recent": {
        "changes": [
            {
                "type": "deploy",
                "deploy_id": "dep-7750",
                "version": "v2.16.0",
                "previous_version": "v2.15.3",
                "at": "2026-10-16T02:51:00Z",
                "author": "ci-bot",
                "summary": "Refactor gift-card balance lookup",
            }
        ]
    },
    "datadog_list_triggered_monitors": {
        "monitors": [{"name": "checkout-api gift-card errors", "service": SERVICE, "since": "2026-10-16T02:57:00Z"}]
    },
    "datadog_query_metrics": {
        "business_kpi": _metric(950, 941, None),  # gift cards are a sliver of checkouts
        "error_rate": _metric(0.004, 0.02, "2026-10-16T02:54:00Z"),
        "latency_p99": _metric(420, 425, None),
        "request_rate": _metric(1800, 1795, None),
        "cpu": _metric(0.41, 0.42, None),
        "memory": _metric(0.55, 0.55, None),
        "db_pool_usage": _metric(0.35, 0.35, None),
    },
    "datadog_search_logs": {
        "patterns": [
            {
                "template": "IllegalStateException in GiftCardBalanceHandler.lookup version=v2.16.0",
                "count": 310,
                "first_seen": "2026-10-16T02:53:30Z",
            }
        ]
    },
    "datadog_get_dependency_health": {
        "dependencies": [
            {"service": "payments-gateway", "error_rate": 0.002, "baseline_error_rate": 0.002, "p99_ms": 305},
            {"service": "inventory-svc", "error_rate": 0.001, "baseline_error_rate": 0.001, "p99_ms": 91},
            {"service": "checkout-db", "error_rate": 0.0, "baseline_error_rate": 0.0, "p99_ms": 12},
        ]
    },
    "pagerduty_search_similar_incidents": {
        "incidents": [
            {
                "incident_id": "PD-910",
                "title": "checkout-api gift-card balance errors after release v2.11.0",
                "root_cause": "Gift-card lookup regression",
                "resolution": "rollback_deploy; recovered in 4 min",
                "resolution_action": "rollback_deploy",
                "verified": True,
            },
            {
                "incident_id": "PD-955",
                "title": "checkout-api gift-card balance errors after release v2.13.0",
                "root_cause": "Gift-card lookup regression",
                "resolution": "rollback_deploy; recovered in 5 min",
                "resolution_action": "rollback_deploy",
                "verified": True,
            },
        ]
    },
    "runbook_get": _RUNBOOK,
    "salesforce_search_cases": {"baseline_per_hour": 1.5, "cases": [_UNRELATED_CASE]},
}

EFFECTS["low_sev_recurring"] = {
    "rollback_deploy": {
        "lag_reads": 1,
        "metrics": {"error_rate": _metric(0.004, 0.004, "2026-10-16T03:12:00Z")},
    },
}


# The same incident as bad_deploy, for a client that uses New Relic instead of Datadog. The agent's logic
# is identical; only the connector and the vendor's query language and response shapes differ.
SCENARIOS["bad_deploy_newrelic"] = {**SCENARIOS["bad_deploy"], "pagerduty_list_service_integrations": _NEWRELIC_INTEGRATIONS}
EFFECTS["bad_deploy_newrelic"] = EFFECTS["bad_deploy"]
