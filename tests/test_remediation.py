import unittest

from sre_agent.agent import SREAgent
from sre_agent.llm import MockReasoningLLM
from sre_agent.mocks.actions import MockActionRunner
from sre_agent.mocks.fixtures import SERVICE, TENANT
from sre_agent.mocks.mcp import MockMCPClient
from sre_agent.models import IncidentRef, ProposedAction
from sre_agent.remediation import ApprovalError, ApprovalPolicy, Remediator

INCIDENT = IncidentRef("PD-1001", TENANT, SERVICE)


def investigate(scenario):
    mcp = MockMCPClient(scenario)
    result = SREAgent(MockReasoningLLM(), mcp).run(INCIDENT)
    return result, mcp


def make_remediator(state, mcp, runner=None):
    sleeps = []
    remediator = Remediator(runner or MockActionRunner(mcp), mcp, INCIDENT, sleep=sleeps.append)
    remediator.policy = ApprovalPolicy(state)
    return remediator, sleeps


def wrong_rollback():
    """A plausible-looking fix for the dependency outage that cannot work."""
    return ProposedAction(
        action="rollback_deploy",
        params={"target_service": SERVICE, "deploy_id": "dep-7702"},
        rationale="Roll back the last deploy",
        evidence=["dep-7702 is the latest deploy"],
        risk={"level": "low", "description": "Reverts the http client bump."},
        expected_effect="error_rate back to baseline",
        how_to_verify={"metric": "error_rate", "comparison": "below", "threshold": 0.01, "within_minutes": 5},
    )


class RemediationTest(unittest.TestCase):
    def test_approved_rollback_is_executed_and_verified(self):
        result, mcp = investigate("bad_deploy")
        runner = MockActionRunner(mcp)
        remediator, sleeps = make_remediator(result, mcp, runner)
        proposal = result.proposed_actions[0]

        v = remediator.approve(proposal, "alice", transcript=result.messages)

        self.assertEqual(v.verdict, "worked")
        self.assertEqual(proposal.status, "executed")
        self.assertEqual(proposal.decided_by, "alice")
        self.assertEqual(runner.executed[0]["action"], "rollback_deploy")
        self.assertEqual(len(v.readings), 2)  # first read still shows the old value
        self.assertEqual(len(sleeps), 1)
        self.assertIn("Verification: worked", result.messages[-1]["content"])

    def test_wrong_fix_is_reported_as_not_working(self):
        result, mcp = investigate("dependency_outage")
        remediator, sleeps = make_remediator(result, mcp)

        v = remediator.approve(wrong_rollback(), "alice")

        self.assertEqual(v.verdict, "did_not_work")
        self.assertEqual(len(v.readings), 5)  # polled every minute for the whole window
        self.assertEqual(len(sleeps), 4)
        self.assertIn("still 0.19", v.detail)

    def test_unreadable_metric_means_unknown_not_success(self):
        result, _ = investigate("bad_deploy")
        mcp = MockMCPClient("bad_deploy", failing_tools={"datadog_query_metrics"})
        remediator, _ = make_remediator(result, mcp)

        v = remediator.approve(result.proposed_actions[0], "alice")

        self.assertEqual(v.verdict, "unknown")

    def test_execution_failure_is_not_verified(self):
        result, mcp = investigate("bad_deploy")
        remediator, _ = make_remediator(result, mcp, MockActionRunner(mcp, fail=True))
        proposal = result.proposed_actions[0]

        v = remediator.approve(proposal, "alice")

        self.assertEqual(v.verdict, "not_executed")
        self.assertEqual(proposal.status, "execution_failed")

    def test_page_team_has_nothing_to_verify(self):
        result, mcp = investigate("dependency_outage")
        page = next(p for p in result.proposed_actions if p.action == "page_team")
        remediator, _ = make_remediator(result, mcp)

        self.assertEqual(remediator.approve(page, "alice").verdict, "not_applicable")

    def test_cannot_approve_twice_or_after_rejection(self):
        result, mcp = investigate("dependency_outage")
        flag, page = result.proposed_actions
        remediator, _ = make_remediator(result, mcp)

        remediator.approve(flag, "alice")
        with self.assertRaises(ApprovalError):
            remediator.approve(flag, "bob")

        remediator.reject(page, "alice")
        with self.assertRaises(ApprovalError):
            remediator.approve(page, "bob")
        self.assertEqual(page.status, "rejected")

    def test_verification_reads_are_tenant_scoped_and_logged(self):
        result, mcp = investigate("bad_deploy")
        remediator, _ = make_remediator(result, mcp)
        remediator.approve(result.proposed_actions[0], "alice")

        self.assertEqual({c["tenant_id"] for c in mcp.calls}, {TENANT})
        self.assertTrue(all(r.tool == "datadog_query_metrics" for r in remediator.executor.log))


if __name__ == "__main__":
    unittest.main()
