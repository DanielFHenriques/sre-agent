import unittest

from sre_agent.agent import SREAgent
from sre_agent.llm import MockReasoningLLM
from sre_agent.mocks.actions import MockActionRunner
from sre_agent.mocks.fixtures import SERVICE, TENANT
from sre_agent.mocks.mcp import MockMCPClient
from sre_agent.models import IncidentRef
from sre_agent.remediation import ApprovalPolicy, NotAuthorized, Remediator
from sre_agent.triage import raise_only

INCIDENT = IncidentRef("PD-1001", TENANT, SERVICE)


class NoRoster(MockMCPClient):
    def call_tool(self, server, tool, arguments, **kw):
        out = super().call_tool(server, tool, arguments, **kw)
        if tool == "pagerduty_get_incident":
            out = {k: v for k, v in out.items() if k != "responders"}
        return out


def setup(scenario, mcp=None, with_policy=True):
    mcp = mcp or MockMCPClient(scenario)
    state = SREAgent(MockReasoningLLM(), mcp).run(INCIDENT)
    runner = MockActionRunner(mcp)
    remediator = Remediator(runner, mcp, INCIDENT, sleep=lambda s: None)
    if with_policy:
        remediator.policy = ApprovalPolicy(state)
    return state, remediator, runner


class AuthorizationTest(unittest.TestCase):
    def assert_refused(self, remediator, runner, state, who, contains):
        proposal = state.proposed_actions[0]
        with self.assertRaises(NotAuthorized) as caught:
            remediator.approve(proposal, who, timeline=state.timeline)
        self.assertIn(contains, str(caught.exception))
        self.assertEqual(runner.executed, [])  # nothing ran
        self.assertEqual(proposal.status, "pending_approval")  # still waiting for the right person
        self.assertEqual(state.timeline[-1].kind, "approval_refused")

    def test_no_policy_means_nothing_runs(self):
        state, remediator, runner = setup("bad_deploy", with_policy=False)
        with self.assertRaises(NotAuthorized):
            remediator.approve(state.proposed_actions[0], "alice")
        self.assertEqual(runner.executed, [])

    def test_outsider_cannot_approve(self):
        state, remediator, runner = setup("bad_deploy")
        self.assert_refused(remediator, runner, state, "mallory", "not an assigned responder")

    def test_major_incident_needs_the_incident_commander(self):
        state, remediator, runner = setup("bad_deploy")  # SEV-2: major
        self.assert_refused(remediator, runner, state, "bob", "only the Incident Commander")
        self.assertEqual(remediator.approve(state.proposed_actions[0], "alice").verdict, "worked")
        self.assertEqual(runner.executed[0]["approved_by"], "alice")

    def test_low_severity_any_assigned_responder_can_approve(self):
        state, remediator, runner = setup("low_sev_recurring")
        self.assertEqual(remediator.approve(state.proposed_actions[0], "bob").verdict, "worked")

    def test_raised_severity_tightens_approval_immediately(self):
        state, remediator, runner = setup("low_sev_recurring")
        raise_only(state.triage, "SEV-2", "EU checkouts failing")
        self.assert_refused(remediator, runner, state, "bob", "only the Incident Commander")

    def test_no_roster_means_no_approvals(self):
        state, remediator, runner = setup("bad_deploy", mcp=NoRoster("bad_deploy"))
        self.assert_refused(remediator, runner, state, "alice", "no responder roster")

    def test_rejecting_needs_a_responder_but_not_the_commander(self):
        state, remediator, _ = setup("bad_deploy")
        with self.assertRaises(NotAuthorized):
            remediator.reject(state.proposed_actions[0], "mallory")
        remediator.reject(state.proposed_actions[0], "bob")
        self.assertEqual(state.proposed_actions[0].status, "rejected")


if __name__ == "__main__":
    unittest.main()
