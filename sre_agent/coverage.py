"""What the agent can and can't see for a service, and what that means for what it may do.

Coverage comes from the client's integrations (via the router), never from the model. It is shown
up front (triage, timeline, briefing, handover), so nobody mistakes a blind spot for "all clear",
and it limits proposals in the executor: a fix the agent can't verify is a fix it doesn't propose.
"""

from __future__ import annotations

from typing import Any

LABELS = {
    "metrics": "metrics",
    "logs": "logs",
    "dependency_health": "dependency tracing",
    "alerts": "alerts",
    "changes": "change tracking",
    "support_cases": "support cases",
}

# What a missing capability means, in the responder's terms.
LIMITS = {
    "metrics": "No metrics: the agent can't measure impact or verify a fix, so it won't propose fixes for this "
               "service. It can investigate, advise and page.",
    "logs": "No logs: it can't see error messages, so it can't tie errors to a specific change or recall similar "
            "past incidents.",
    "dependency_health": "No dependency tracing: it can't tell whether a dependency is at fault.",
    "alerts": "No alerts: it can't see what else is firing, so it can't tell a local problem from a wider one.",
    "changes": "No change tracking: it can't see deploys or config changes, so it can't propose a rollback.",
    "support_cases": "No support cases: it can't tell whether customers are reporting it.",
}

# Capabilities each action needs. Every remediation must be verifiable (metrics); a rollback must target a
# deploy the agent actually saw (changes). Paging people needs nothing.
ACTION_NEEDS = {
    "rollback_deploy": {"metrics", "changes"},
    "restart_service": {"metrics"},
    "scale_out": {"metrics"},
    "toggle_feature_flag": {"metrics"},
    "page_team": set(),
}


def coverage(router: Any) -> dict[str, Any]:
    if router is None:
        return {
            "known": False,
            "covered": [],
            "missing": sorted(LABELS),
            "unsupported": [],
            "limits": ["The service's integrations couldn't be read, so the agent can't use any data source for it. "
                       "It can't propose fixes; it can advise and page."],
        }
    covered = router.covered()
    missing = [c for c in LABELS if c not in covered]
    return {
        "known": True,
        "covered": sorted(covered),
        "missing": missing,
        "unsupported": list(router.unsupported),
        "limits": [LIMITS[c] for c in missing],
    }


def summary(cov: dict[str, Any]) -> str:
    if not cov["known"]:
        return "Agent coverage: unknown (integrations unreadable)."
    parts = ", ".join(f"{LABELS[c]} {'yes' if c in cov['covered'] else 'NO'}" for c in LABELS)
    extra = f" Integrated but not usable yet: {', '.join(cov['unsupported'])}." if cov["unsupported"] else ""
    return f"Agent coverage: {parts}.{extra}"


def blocked_by(action: str, covered: set[str]) -> list[str]:
    """Capabilities an action needs that this service doesn't have."""
    return [c for c in LABELS if c in ACTION_NEEDS.get(action, set()) - covered]
