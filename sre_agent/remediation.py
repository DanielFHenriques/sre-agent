"""Human approval, execution, and verification of proposed actions.

This is deliberately outside the model's reach: no tool in the registry maps here. The only way an
action runs is a human calling `approve`. Verification is plain code comparing a metric with the
proposal's `how_to_verify`, so the model cannot talk itself into "the fix worked".
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Protocol

from sre_agent.models import IncidentRef, ProposedAction, Verification
from sre_agent.tools.executor import MCPClient, ToolExecutor


class ActionRunner(Protocol):
    def run(self, proposal: ProposedAction, *, tenant_id: str, approved_by: str) -> dict[str, Any]: ...


class ApprovalError(Exception):
    pass


class Remediator:
    def __init__(
        self,
        runner: ActionRunner,
        mcp: MCPClient,
        incident: IncidentRef,
        poll_interval_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.runner = runner
        self.incident = incident
        self.poll_interval_s = poll_interval_s
        self.sleep = sleep
        # Verification reads go through the same policy gate as the agent's: tenant-scoped and logged.
        self.executor = ToolExecutor(mcp, incident)

    def reject(self, proposal: ProposedAction, rejected_by: str) -> None:
        self._require_pending(proposal)
        proposal.status = "rejected"
        proposal.decided_by = rejected_by

    def approve(
        self, proposal: ProposedAction, approved_by: str, transcript: list[dict[str, Any]] | None = None
    ) -> Verification:
        """Run an approved proposal, then check its how_to_verify. Appends the outcome to the transcript
        (if given) so the next model turn starts from what actually happened."""
        self._require_pending(proposal)
        proposal.status = "approved"
        proposal.decided_by = approved_by
        try:
            self.runner.run(proposal, tenant_id=self.incident.tenant_id, approved_by=approved_by)
        except Exception as e:
            proposal.status = "execution_failed"
            result = Verification(proposal.action, "not_executed", f"Execution failed: {e}")
        else:
            proposal.status = "executed"
            result = self._verify(proposal)
        if transcript is not None:
            transcript.append({"role": "user", "content": _transcript_note(proposal, result)})
        return result

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
