"""Scribe-style incident timeline and the handover brief built from it.

Entries are written by code from events (an assessment, a proposal, an approval, a verification),
not by the model: cheap, deterministic, and impossible to hallucinate. The course's guidance for a
Scribe applies: record findings and decisions ("we decided A over B"), not every tool call.
The same timeline serves the handover brief, the postmortem draft, and later, memory.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from sre_agent.models import AgentResult, ProposedAction, TimelineEntry

Now = Callable[[], str]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def describe(p: ProposedAction) -> str:
    params = ", ".join(f"{k}={v}" for k, v in p.params.items() if k != "target_service")
    return f"{p.action}({params})"


def proposal_entry(now: str, p: ProposedAction) -> TimelineEntry:
    approval = f"Fast-track, one-click approval: {p.fast_track_reason}" if p.fast_track else "Awaiting approval."
    return TimelineEntry(
        now, "proposed", f"Proposed {describe(p)}. Risk {p.risk['level']}: {p.risk['description']} {approval}", "agent"
    )


def investigation_entries(
    now: str,
    previous: dict[str, Any] | None,
    assessment: dict[str, Any],
    stop_reason: str,
    cycle: int,
) -> list[TimelineEntry]:
    """What changed in this investigation, compared with the previous assessment."""
    out: list[TimelineEntry] = []
    impact = assessment.get("impact") or {}
    old_impact = (previous or {}).get("impact") or {}
    if impact and impact.get("suggested_severity") != old_impact.get("suggested_severity"):
        out.append(TimelineEntry(now, "impact", f"Impact: {impact['scope']}. Suggested {impact['suggested_severity']}.", "agent"))

    hypotheses = assessment.get("hypotheses") or []
    old = ((previous or {}).get("hypotheses") or [{}])[0].get("cause")
    if hypotheses and hypotheses[0]["cause"] != old:
        h = hypotheses[0]
        text = f"Leading hypothesis: {h['cause']} ({h['confidence']} confidence)."
        if old:
            text = f"Changed leading hypothesis from '{old}' to '{h['cause']}' ({h['confidence']} confidence)."
        out.append(TimelineEntry(now, "hypothesis", text, "agent"))

    if stop_reason not in ("assessment_submitted", "cycle_limit"):
        out.append(TimelineEntry(now, "stopped_early", f"Investigation (cycle {cycle}) stopped early: {stop_reason}.", "agent"))
    return out


def handover_brief(result: AgentResult, from_who: str, to_who: str) -> str:
    """What the incoming responder needs in the first minute. Built from state, not from memory of the call."""
    a = result.assessment or {}
    impact = a.get("impact") or {}
    hypotheses = a.get("hypotheses") or []
    pending = [p for p in result.proposed_actions if p.status == "pending_approval"]
    done = [p for p in result.proposed_actions if p.status != "pending_approval"]

    lines = [
        f"HANDOVER {result.incident_id}: from {from_who} to {to_who}",
        f"Impact: {impact.get('scope', 'unknown')}. Suggested severity {impact.get('suggested_severity', 'unknown')}.",
        f"Current read: {a.get('summary', 'no assessment yet')}",
    ]
    cov = (result.triage or {}).get("coverage")
    if cov:
        from sre_agent.coverage import summary as coverage_summary  # local: coverage imports nothing from here
        lines.append(coverage_summary(cov))
        lines += [f"  Limit: {limit}" for limit in cov["limits"]]
    if hypotheses:
        lines.append(f"Leading hypothesis: {hypotheses[0]['cause']} ({hypotheses[0]['confidence']}).")
        for h in hypotheses[1:]:
            reason = (h["evidence_against"] or ["no reason recorded"])[0]
            lines.append(f"  Less likely: {h['cause']} ({h['confidence']}): {reason}")
    if done:
        lines.append("Already tried:")
        for p in done:
            lines.append(f"  - {describe(p)}: {p.status}, verification {p.verification_verdict or 'n/a'}, by {p.decided_by}")
    lines.append("Waiting on you:" if pending else "Waiting on you: nothing pending.")
    for i, p in enumerate(pending, 1):
        lines.append(f"  [{i}] {describe(p)}. Risk {p.risk['level']}: {p.risk['description']}")
    for q in a.get("open_questions") or []:
        lines.append(f"Open question: {q}")
    lines.append("Timeline:")
    for t in result.timeline:
        lines.append(f"  {t.at}  [{t.actor}] {t.text}")
    return "\n".join(lines)
