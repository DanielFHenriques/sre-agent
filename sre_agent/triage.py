"""Severity and priority, decided by code from data, right after prefetch and before the model reasons.

Why deterministic: severity decides how big the response is (who joins, what the agent may suggest),
so it has to be fast, explainable, and the same every time for the same data. The model can argue for
a *higher* severity in its assessment; it can never lower one. Lowering is a human decision.

Signals, in order of weight:
  1. Business impact: how far the service's business KPI has dropped from baseline.
  2. Customer reports: support cases related to this service, opened after onset, compared with the
     normal case rate. A burst means the incident is customer-facing, whatever the graphs say.
     Enterprise accounts among them raise it further.
  3. PagerDuty urgency, for priority only.
All thresholds are illustrative; in practice they are per-tenant configuration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

SEVERITIES = ["SEV-1", "SEV-2", "SEV-3", "SEV-4", "SEV-5"]  # SEV-1 is the worst

MODES = {
    "major": "Major incident: get an Incident Commander on the call and bring in the subject matter experts "
    "for the affected services. Every action needs the Incident Commander's approval.",
    "standard": "The owning team's on-call handles it. The agent investigates and proposes; on-call approves.",
    "low": "Low impact, no customer reports: lean investigation. A pre-vetted fix for a recurring problem "
    "may be fast-tracked for one-click approval.",
}


def _n(severity: str) -> int:
    return SEVERITIES.index(severity) + 1


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def severity_from_kpi(change_pct: float) -> str:
    drop = -change_pct
    if drop >= 50:
        return "SEV-1"
    if drop >= 20:
        return "SEV-2"
    if drop >= 5:
        return "SEV-3"
    return "SEV-4" if drop > 0 else "SEV-5"


def cases_status(cases: dict[str, Any] | None) -> str:
    """ok | failed | not_integrated. Anything malformed (an error, truncated output) is unknown, never zero."""
    if cases is None:
        return "not_integrated"
    needed = ("related_count", "enterprise_count", "baseline_per_hour", "since")
    return "ok" if all(k in cases for k in needed) else "failed"


def triage(
    incident: dict[str, Any],
    kpi: dict[str, Any] | None,
    cases: dict[str, Any] | None,
) -> dict[str, Any]:
    status = cases_status(cases)
    signals: list[str] = []

    # 1. Business impact.
    kpi_change = None
    if kpi and "baseline" in kpi and kpi["baseline"]:
        kpi_change = round((kpi["current"] - kpi["baseline"]) / kpi["baseline"] * 100, 1)
        severity = severity_from_kpi(kpi_change)
        signals.append(f"Business KPI {kpi_change:+.1f}% vs baseline ({kpi['baseline']} -> {kpi['current']}): {severity}.")
    else:
        severity = "SEV-3"
        signals.append("Business KPI unavailable: defaulting to SEV-3 until impact is known.")

    # 2. Customer reports.
    related, enterprise, customer_facing = 0, 0, None
    if status == "ok" and cases is not None:
        # Counted by the source since onset; we only compare with what a normal window would bring.
        related, enterprise = cases["related_count"], cases["enterprise_count"]
        since = _ts(cases["since"])
        now = _ts(incident.get("now", cases["since"]))
        expected = cases["baseline_per_hour"] * max((now - since).total_seconds(), 60) / 3600
        customer_facing = related >= 3 and related > 3 * expected
        if customer_facing:
            floor = "SEV-2" if enterprise or related >= 20 else "SEV-3"
            if _n(floor) < _n(severity):
                signals.append(f"{related} related support cases since onset ({enterprise} enterprise): raised to {floor}.")
                severity = floor
            else:
                signals.append(f"{related} related support cases since onset ({enterprise} enterprise): customer-facing.")
        else:
            signals.append(f"{related} related support cases since onset (about {expected:.1f} expected): no customer burst.")
    elif status == "not_integrated":
        signals.append("No support-case integration for this service: can't tell whether customers are reporting it.")
    else:
        signals.append("Support cases could not be read: can't tell whether customers are reporting it.")

    return _with_priority_and_mode({
        "severity": severity,
        "customer_facing": customer_facing,
        "related_cases": related,
        "enterprise_cases": enterprise,
        "kpi_change_pct": kpi_change,
        "urgency": incident.get("urgency", "unknown"),
        "signals": signals,
    })


def _with_priority_and_mode(t: dict[str, Any]) -> dict[str, Any]:
    n = _n(t["severity"])
    n -= 1 if t["urgency"] == "high" else 0
    n -= 1 if t["enterprise_cases"] else 0
    t["priority"] = f"P{min(max(n, 1), 5)}"
    if t["severity"] in ("SEV-1", "SEV-2"):
        mode = "major"
    elif t["severity"] == "SEV-3" or t["customer_facing"] is not False:
        # SEV-4/5 counts as low only if we positively know customers aren't reporting it.
        mode = "standard"
    else:
        mode = "low"
    t["response_mode"] = mode
    t["recommendation"] = MODES[mode] + (" Customer-facing: bring in the customer liaison." if t["customer_facing"] else "")
    return t


def raise_only(t: dict[str, Any], suggested: str | None, reason: str) -> str | None:
    """Apply the model's suggested severity if it is worse. Returns a timeline note, or None."""
    if suggested not in SEVERITIES or _n(suggested) >= _n(t["severity"]):
        return None
    old = t["severity"]
    t["severity"] = suggested
    t["signals"].append(f"Raised from {old} to {suggested} after investigation: {reason}")
    _with_priority_and_mode(t)
    return f"Severity raised from {old} to {suggested} ({t['priority']}, {t['response_mode']}): {reason}"


def summary(t: dict[str, Any]) -> str:
    facing = {True: "customer-facing", False: "no customer reports", None: "customer impact unknown"}[t["customer_facing"]]
    return f"{t['severity']} / {t['priority']}, {facing}, response mode: {t['response_mode']}."


# How many past incidents on this service must have been fixed, and verified, by the same action.
FAST_TRACK_MIN_VERIFIED = 2


def fast_track_reason(
    t: dict[str, Any] | None, action: str, similar_incidents: list[dict[str, Any]], a_fix_already_failed: bool
) -> str | None:
    """Is this proposal a known-good, low-impact fix that can be offered as a one-click approval?

    It is still only a proposal: a human approves it. Fast-track changes how it is presented and who
    may approve it, not whether approval is needed. All conditions, decided by code:
      - it is a rollback to the previous (known stable) version, the lowest-risk action in the catalog;
      - triage says low impact: SEV-4/5 *and* we positively know customers aren't reporting it;
      - the same action fixed this service before, verified, at least FAST_TRACK_MIN_VERIFIED times
        (from PagerDuty's labelled history or the agent's memory, where "verified" was decided by code);
      - nothing has already failed in this incident (then we are not in a known situation).
    """
    if action != "rollback_deploy" or t is None or t["response_mode"] != "low" or a_fix_already_failed:
        return None
    # PagerDuty history and the agent's own memory can describe the same incident: count each once.
    verified = list({
        i["incident_id"]: i for i in similar_incidents if i.get("resolution_action") == action and i.get("verified")
    }.values())
    if len(verified) < FAST_TRACK_MIN_VERIFIED:
        return None
    ids = ", ".join(i["incident_id"] for i in verified)
    return (f"{t['severity']}, no customer reports, and the same rollback fixed this service "
            f"{len(verified)} times before, verified ({ids}).")
