"""Human approval, execution, and verification of proposed actions.

This is deliberately outside the model's reach: no tool in the registry maps here. The only way an
action runs is a human calling `approve`. Verification is plain code comparing a metric with the
proposal's `how_to_verify`, so the model cannot talk itself into "the fix worked".
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Protocol

from sre_agent.models import AgentResult, IncidentRef, ProposedAction, TimelineEntry, Verification
from sre_agent.timeline import Now, describe, utc_now
from sre_agent.tools.executor import MCPClient, ToolExecutor


class ActionRunner(Protocol):
    def run(self, proposal: ProposedAction, *, tenant_id: str, approved_by: str) -> dict[str, Any]: ...


class ApprovalError(Exception):
    pass


class NotAuthorized(ApprovalError):
    pass


class ApprovalPolicy:
    """Who may approve an action on this incident. Read live from the incident at approval time, so a
    severity raised mid-incident tightens it immediately.

    - Only people on the incident's responder roster (from PagerDuty) can approve or reject.
    - In a major incident (SEV-1/2), only the Incident Commander can approve.
    - No roster, no approvals: fail closed.

    Identity (`approver`) is assumed to come from the surface's login (Slack/Teams/PagerDuty SSO), never
    from text in a chat message, and never from a password or code typed to the agent. For high-risk
    actions the surface could ask for a fresh SSO login before the approval counts.
    """

    def __init__(self, state: AgentResult):
        self.state = state

    def roster(self) -> dict[str, str]:
        for m in self.state.messages:
            if m["role"] == "tool" and '"responders"' in str(m.get("content")):
                try:
                    return {r["user"]: r["role"] for r in json.loads(m["content"])["responders"]}
                except (json.JSONDecodeError, KeyError, TypeError):
                    return {}
        return {}

    def check(self, approver: str, approving: bool) -> None:
        roster = self.roster()
        incident = self.state.incident_id
        if not roster:
            raise NotAuthorized(f"no responder roster for {incident}; approvals are refused until it is known")
        role = roster.get(approver)
        if role is None:
            raise NotAuthorized(f"{approver} is not an assigned responder on {incident}")
        mode = (self.state.triage or {}).get("response_mode", "major")  # unknown severity: strictest rule
        if approving and mode == "major" and role != "incident_commander":
            raise NotAuthorized(
                f"{incident} is a major incident: only the Incident Commander can approve actions ({approver} is a {role})"
            )


class Remediator:
    def __init__(
        self,
        runner: ActionRunner,
        mcp: MCPClient,
        incident: IncidentRef,
        poll_interval_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        now: Now = utc_now,
    ):
        self.runner = runner
        self.incident = incident
        self.poll_interval_s = poll_interval_s
        self.sleep = sleep
        self.now = now
        # Must be set (ApprovalPolicy(state)) before anything can be approved. Fail closed.
        self.policy: ApprovalPolicy | None = None
        # Verification reads go through the same policy gate as the agent's: tenant-scoped and logged.
        self.executor = ToolExecutor(mcp, incident)

    def reject(self, proposal: ProposedAction, rejected_by: str, timeline: list[TimelineEntry] | None = None) -> None:
        self._require_pending(proposal)
        self._authorize(proposal, rejected_by, timeline, approving=False)
        proposal.status = "rejected"
        proposal.decided_by = rejected_by
        self._log(timeline, "rejected", f"Rejected {describe(proposal)}.", rejected_by)

    def approve(
        self,
        proposal: ProposedAction,
        approved_by: str,
        transcript: list[dict[str, Any]] | None = None,
        timeline: list[TimelineEntry] | None = None,
    ) -> Verification:
        """Run an approved proposal, then check its how_to_verify. Appends the outcome to the transcript
        (if given) so the next model turn starts from what actually happened, and to the timeline."""
        self._require_pending(proposal)
        self._authorize(proposal, approved_by, timeline, approving=True)
        proposal.status = "approved"
        proposal.decided_by = approved_by
        self._log(timeline, "approved", f"Approved {describe(proposal)}.", approved_by)
        try:
            self.runner.run(proposal, tenant_id=self.incident.tenant_id, approved_by=approved_by)
        except Exception as e:
            proposal.status = "execution_failed"
            result = Verification(proposal.action, "not_executed", f"Execution failed: {e}")
            self._log(timeline, "executed", f"{describe(proposal)} failed to run: {e}", "automation")
        else:
            proposal.status = "executed"
            self._log(timeline, "executed", f"Ran {describe(proposal)}.", "automation")
            result = self._verify(proposal)
            self._log(timeline, "verified", f"Verification of {proposal.action}: {result.verdict}. {result.detail}",
                      "automation")
        proposal.verification_verdict = result.verdict
        if transcript is not None:
            transcript.append({"role": "user", "content": _transcript_note(proposal, result)})
        return result

    def _authorize(self, proposal, who: str, timeline, approving: bool) -> None:
        if self.policy is None:
            raise NotAuthorized("no approval policy loaded; refusing to run anything")
        try:
            self.policy.check(who, approving)
        except NotAuthorized as e:
            verb = "Approval" if approving else "Rejection"
            self._log(timeline, "approval_refused", f"{verb} of {describe(proposal)} by {who} refused: {e}", who)
            raise

    def _log(self, timeline: list[TimelineEntry] | None, kind: str, text: str, actor: str) -> None:
        if timeline is not None:
            timeline.append(TimelineEntry(self.now(), kind, text, actor))

    def _require_pending(self, proposal: ProposedAction) -> None:
        if proposal.status != "pending_approval":
            raise ApprovalError(f"{proposal.action} is '{proposal.status}', not pending approval")

    def _verify(self, proposal: ProposedAction) -> Verification:
        check = proposal.how_to_verify
        if check is None:
            return Verification(proposal.action, "not_applicable", "No metric check for this action.")

        metric, comparison, threshold = check["metric"], check["comparison"], check["threshold"]
        attempts = max(1, int(check["within_minutes"] * 60 // self.poll_interval_s))
        readings: list[dict[str, Any]] = []
        for attempt in range(1, attempts + 1):
            out = self.executor.execute(
                "datadog_query_metrics",
                json.dumps({
                    "service_id": self.incident.service_id,
                    "metric": metric,
                    "lookback_minutes": check["within_minutes"],
                }),
            )
            if "error" in out:
                readings.append({"attempt": attempt, "error": out.get("detail", out["error"])})
            else:
                value = out["current"]
                passed = value < threshold if comparison == "below" else value > threshold
                readings.append({"attempt": attempt, "value": value, "passed": passed})
                if passed:
                    return Verification(
                        proposal.action, "worked",
                        f"{metric} is {value}, {comparison} {threshold} (check {attempt} of {attempts}).",
                        readings,
                    )
            if attempt < attempts:
                self.sleep(self.poll_interval_s)

        values = [r["value"] for r in readings if "value" in r]
        if not values:
            return Verification(proposal.action, "unknown", f"Could not read {metric} to verify.", readings)
        return Verification(
            proposal.action, "did_not_work",
            f"{metric} still {values[-1]} after {check['within_minutes']} min; expected {comparison} {threshold}.",
            readings,
        )


def _transcript_note(proposal: ProposedAction, result: Verification) -> str:
    return (
        f"[automation] {proposal.action} {proposal.params} approved by {proposal.decided_by}, "
        f"status {proposal.status}. Verification: {result.verdict}. {result.detail}"
    )
