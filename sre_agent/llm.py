"""Model clients. Both speak OpenAI Chat Completions: messages in, a chat.completion dict out.

- MockReasoningLLM: deterministic stand-in that follows the playbook in the system prompt by reading
  the tool results in the transcript. Lets the loop, guardrails and tests run with no API key.
- OpenAIChatClient: the real thing, used only if the `openai` package and an API key are present.
"""

from __future__ import annotations

import itertools
import json
import os
import re
from datetime import datetime, timedelta
from typing import Any, Protocol

ToolChoice = str | dict[str, Any]


class LLMClient(Protocol):
    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: ToolChoice = "auto"
    ) -> dict[str, Any]: ...


class OpenAIChatClient:
    def __init__(self, model: str | None = None):
        from openai import OpenAI  # optional dependency

        self._client = OpenAI()
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4.1")

    def complete(self, messages, tools, tool_choice="auto"):
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=True,
            temperature=0,
        )
        return response.model_dump()


# --- Mock ---------------------------------------------------------------------------------------


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class MockReasoningLLM:
    """Plays the model's part. Not smart: a hard-coded version of the decision table in DESIGN.md."""

    model = "mock-sre-reasoner"

    def __init__(self) -> None:
        self._ids = itertools.count(1)

    def complete(self, messages, tools, tool_choice="auto"):
        seen = _tool_results(messages)
        forced = isinstance(tool_choice, dict) and tool_choice["function"]["name"] == "submit_assessment"
        incident = seen.latest("pagerduty_get_incident") or {}
        service = incident.get("service_id", "unknown")
        window = 30

        if not forced and not seen.called("datadog_search_logs"):
            return self._calls(
                "Sizing up customer impact, error and latency trend, error logs, and dependency health in parallel.",
                [
                    ("datadog_query_metrics", {"service_id": service, "metric": "business_kpi", "lookback_minutes": window}),
                    ("datadog_query_metrics", {"service_id": service, "metric": "error_rate", "lookback_minutes": window}),
                    ("datadog_query_metrics", {"service_id": service, "metric": "latency_p99", "lookback_minutes": window}),
                    ("datadog_search_logs", {"service_id": service, "level": "error", "query": None, "lookback_minutes": window}),
                    ("datadog_get_dependency_health", {"service_id": service}),
                ],
            )

        if not forced and not seen.called("runbook_get"):
            logs = seen.latest("datadog_search_logs") or {}
            top = (logs.get("patterns") or [{}])[0].get("template", "elevated 5xx error rate")
            return self._calls(
                "Looking for similar past incidents and the runbook.",
                [
                    ("pagerduty_search_similar_incidents", {"service_id": service, "symptoms": top}),
                    ("runbook_get", {"service_id": service}),
                ],
            )

        decision = _decide(seen, incident)
        if not forced and decision["proposals"] and not seen.called("propose_action"):
            return self._calls(decision["note"], [("propose_action", p) for p in decision["proposals"]])
        return self._calls(None, [("submit_assessment", decision["assessment"])])

    def _calls(self, content: str | None, calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
        tool_calls = [
            {"id": f"call_{next(self._ids)}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
            for n, a in calls
        ]
        return {
            "object": "chat.completion",
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {"role": "assistant", "content": content, "tool_calls": tool_calls},
                }
            ],
        }


class _Seen:
    """Tool results in the transcript, by tool name, in call order."""

    def __init__(self) -> None:
        self.by_tool: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}

    def called(self, name: str) -> bool:
        return name in self.by_tool

    def latest(self, name: str) -> dict[str, Any] | None:
        entries = self.by_tool.get(name)
        return entries[-1][1] if entries else None

    def metric(self, name: str) -> dict[str, Any] | None:
        for args, out in reversed(self.by_tool.get("datadog_query_metrics", [])):
            if args.get("metric") == name:
                return out
        return None

    def failed(self) -> list[str]:
        return [n for n, entries in self.by_tool.items() if any("error" in out for _, out in entries)]


def _tool_results(messages: list[dict[str, Any]]) -> _Seen:
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    seen = _Seen()
    for m in messages:
        for c in m.get("tool_calls") or []:
            calls[c["id"]] = (c["function"]["name"], json.loads(c["function"]["arguments"]))
        if m["role"] == "tool" and m["tool_call_id"] in calls:
            name, args = calls[m["tool_call_id"]]
            seen.by_tool.setdefault(name, []).append((args, json.loads(m["content"])))
    return seen


def _decide(seen: _Seen, incident: dict[str, Any]) -> dict[str, Any]:
    service = incident.get("service_id", "unknown")
    onset = _ts(incident["onset_estimate"]) if "onset_estimate" in incident else None
    failed = seen.failed()

    changes = (seen.latest("changes_list_recent") or {}).get("changes", [])
    logs = seen.latest("datadog_search_logs") or {}
    deps_result = seen.latest("datadog_get_dependency_health") or {}
    deps_known = "dependencies" in deps_result
    runbook = seen.latest("runbook_get") or {}

    suspect = next(
        (c for c in changes if onset and onset - timedelta(minutes=30) <= _ts(c["at"]) <= onset), None
    )
    new_patterns = [
        p for p in logs.get("patterns", []) if suspect and _ts(p["first_seen"]) >= _ts(suspect["at"])
    ]
    degraded = [
        d for d in deps_result.get("dependencies", [])
        if d["error_rate"] >= 0.05 and d["error_rate"] >= 10 * max(d["baseline_error_rate"], 0.001)
    ]

    impact = _impact(seen)
    hypotheses: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    open_questions = [f"{name} failed; that evidence is missing." for name in failed]
    blank = {"deploy_id": None, "flag_name": None, "flag_value": None, "replicas": None, "team": None}

    if suspect and deps_known and not degraded:
        evidence = [f"Deploy {suspect['deploy_id']} ({suspect['version']}) at {suspect['at']}, onset {incident['onset_estimate']}"]
        evidence += [f"New error pattern first seen {p['first_seen']}: {p['template']}" for p in new_patterns]
        evidence.append("All downstream dependencies at baseline error rates")
        confidence = "high" if new_patterns else "medium"
        hypotheses.append({
            "cause": f"Bad deploy {suspect['version']}",
            "confidence": confidence,
            "evidence_for": evidence,
            "evidence_against": [] if new_patterns else ["Could not confirm the errors come from the new code"],
        })
        proposals.append({
            "action": "rollback_deploy",
            "target_service": service,
            **blank,
            "deploy_id": suspect["deploy_id"],
            "rationale": f"Errors started {int((onset - _ts(suspect['at'])).total_seconds() // 60)} min after "
            f"{suspect['version']} shipped; dependencies are healthy. "
            f"Roll back to {suspect['previous_version']}.",
            "evidence": evidence,
            "risk": {
                "level": "low",
                "description": f"Reverts everything in {suspect['version']} ({suspect['summary']}). "
                "If it included a schema migration, check that first. Rolling restart, no downtime expected.",
            },
            "expected_effect": f"error_rate back near baseline ({_baseline(seen, 'error_rate')}) within about "
            "5 min of the rollout finishing.",
            "how_to_verify": {"metric": "error_rate", "comparison": "below", "threshold": 0.01, "within_minutes": 10},
        })
        next_step = f"Approve rollback of {suspect['deploy_id']} to {suspect['previous_version']}, then watch error_rate for 10 min."
        note = f"{suspect['version']} shipped just before onset and dependencies are healthy. Proposing a rollback."
    elif degraded:
        dep = degraded[0]
        evidence = [
            f"{dep['service']} error rate {dep['error_rate']:.0%} vs baseline {dep['baseline_error_rate']:.1%}",
            *[f"Log pattern: {p['template']}" for p in logs.get("patterns", [])[:1]],
            "No deploy of this service in the 30 min before onset" if not suspect else "",
        ]
        evidence = [e for e in evidence if e]
        hypotheses.append({
            "cause": f"Downstream dependency {dep['service']} degraded",
            "confidence": "high",
            "evidence_for": evidence,
            "evidence_against": [],
        })
        hypotheses.append({
            "cause": "Bad change in this service",
            "confidence": "low",
            "evidence_for": [],
            "evidence_against": ["No recent deploy before onset", "Errors are timeouts calling the dependency"],
        })
        flag = re.search(r"toggle_feature_flag ([\w.]+)=(true|false)", json.dumps(runbook))
        if flag:
            proposals.append({
                "action": "toggle_feature_flag",
                "target_service": service,
                **blank,
                "flag_name": flag.group(1),
                "flag_value": flag.group(2) == "true",
                "rationale": f"Runbook mitigation for {dep['service']} failures: queue payments for retry instead of failing checkout.",
                "evidence": evidence,
                "risk": {
                    "level": "medium",
                    "description": "Orders are accepted before payment is confirmed. Some will fail later and "
                    "need customer follow-up. Turn the flag off once payments-gateway recovers.",
                },
                "expected_effect": "Checkout stops failing on payment timeouts; error_rate drops within a few minutes.",
                "how_to_verify": {"metric": "error_rate", "comparison": "below", "threshold": 0.02, "within_minutes": 5},
            })
        team = re.search(r"page (team-[\w-]+)", json.dumps(runbook))
        if team:
            proposals.append({
                "action": "page_team",
                "target_service": service,
                **blank,
                "team": team.group(1),
                "rationale": f"{dep['service']} is the source; its owners need to be engaged.",
                "evidence": evidence[:1],
                "risk": {"level": "low", "description": "Wakes up the payments on-call. No change to production."},
                "expected_effect": f"{team.group(1)} acknowledges and starts on {dep['service']}.",
                "how_to_verify": None,
            })
        next_step = "Do not roll back. Approve the fallback flag and page the dependency owner."
        note = f"{dep['service']} is failing, not us. Proposing the runbook mitigation instead of a rollback."
    else:
        next_step = "No safe action yet. Check the open questions first."
        note = ""
        open_questions.append("No change or dependency explains the onset; check traffic and saturation next.")

    confidence = hypotheses[0]["confidence"] if hypotheses else "low"
    if failed and confidence == "high":
        confidence = "medium"  # never high confidence on partial evidence
    summary = (
        f"{incident.get('title', 'Incident')}. "
        + (f"Impact: {impact['scope']}. " if impact["change_pct"] is not None else "")
        + (f"Most likely: {hypotheses[0]['cause']}. " if hypotheses else "Cause unclear. ")
        + f"Next: {next_step}"
    )
    return {
        "note": note,
        "proposals": proposals,
        "assessment": {
            "summary": summary,
            "impact": impact,
            "hypotheses": hypotheses,
            "recommended_next_step": next_step,
            "confidence": confidence,
            "open_questions": open_questions,
        },
    }


def _baseline(seen: _Seen, metric: str) -> Any:
    return (seen.metric(metric) or {}).get("baseline", "unknown")


def _impact(seen: _Seen) -> dict[str, Any]:
    """Severity from the business metric, per PagerDuty's guidance. Thresholds are illustrative."""
    label = (seen.latest("pagerduty_get_service_context") or {}).get("business_kpi", "business KPI")
    kpi = seen.metric("business_kpi") or {}
    if "baseline" not in kpi:
        return {"business_metric": label, "baseline": None, "current": None, "change_pct": None,
                "scope": "Unknown: the business metric could not be read", "suggested_severity": "unknown"}
    change = round((kpi["current"] - kpi["baseline"]) / kpi["baseline"] * 100, 1)
    drop = -change
    severity = ("SEV-1" if drop >= 50 else "SEV-2" if drop >= 20 else "SEV-3" if drop >= 5
                else "SEV-4" if drop > 0 else "SEV-5")
    return {
        "business_metric": label,
        "baseline": kpi["baseline"],
        "current": kpi["current"],
        "change_pct": change,
        "scope": f"{label} down {drop:.0f}% ({kpi['baseline']} -> {kpi['current']}), all customers of this service",
        "suggested_severity": severity,
    }
