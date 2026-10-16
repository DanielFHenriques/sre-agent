import json
import unittest

from sre_agent.agent import SREAgent
from sre_agent.llm import MockReasoningLLM
from sre_agent.mocks.actions import MockActionRunner
from sre_agent.mocks.fixtures import SERVICE, TENANT
from sre_agent.mocks.mcp import MockMCPClient
from sre_agent.models import IncidentRef
from sre_agent.remediation import Remediator

INCIDENT = IncidentRef("PD-1001", TENANT, SERVICE)


def run(mcp, llm=None):
    return SREAgent(llm or MockReasoningLLM(), mcp).run(INCIDENT)


class OnlyOnePriorFix(MockMCPClient):
    def call_tool(self, server, tool, arguments, **kw):
        out = super().call_tool(server, tool, arguments, **kw)
        if tool == "pagerduty_search_similar_incidents":
            out = {"incidents": out["incidents"][:1]}
        return out


class SaysItIsWorse(MockReasoningLLM):
    """A model whose investigation finds the impact is worse than triage thought."""

    def complete(self, messages, tools, tool_choice="auto"):
        response = super().complete(messages, tools, tool_choice)
        for call in response["choices"][0]["message"]["tool_calls"]:
            if call["function"]["name"] == "submit_assessment":
                args = json.loads(call["function"]["arguments"])
                args["impact"]["suggested_severity"] = "SEV-2"
                args["impact"]["scope"] = "gift-card failures block all EU checkouts"
                call["function"]["arguments"] = json.dumps(args)
        return response


class FastTrackTest(unittest.TestCase):
    def test_low_impact_recurring_rollback_is_fast_tracked_but_still_needs_approval(self):
        mcp = MockMCPClient("low_sev_recurring")
        result = run(mcp)
        (p,) = result.proposed_actions
        self.assertTrue(p.fast_track)
        self.assertIn("PD-910, PD-955", p.fast_track_reason)
        self.assertEqual(p.status, "pending_approval")
        self.assertNotIn("rollback", " ".join(c["tool"] for c in mcp.calls))  # nothing ran
        self.assertIn("Fast-track", next(t.text for t in result.timeline if t.kind == "proposed"))

        verdict = Remediator(MockActionRunner(mcp), mcp, INCIDENT, sleep=lambda s: None).approve(p, "alice")
        self.assertEqual(verdict.verdict, "worked")

    def test_major_incident_rollback_is_not_fast_tracked(self):
        (p,) = run(MockMCPClient("bad_deploy")).proposed_actions
        self.assertEqual(p.action, "rollback_deploy")
        self.assertFalse(p.fast_track)

    def test_unknown_customer_impact_blocks_fast_track(self):
        result = run(MockMCPClient("low_sev_recurring", {"salesforce_search_cases"}))
        self.assertEqual(result.triage["response_mode"], "standard")
        self.assertFalse(result.proposed_actions[0].fast_track)

    def test_one_prior_fix_is_not_a_pattern(self):
        self.assertFalse(run(OnlyOnePriorFix("low_sev_recurring")).proposed_actions[0].fast_track)

    def test_raised_severity_withdraws_fast_track(self):
        result = run(MockMCPClient("low_sev_recurring"), llm=SaysItIsWorse())
        self.assertEqual(result.triage["severity"], "SEV-2")
        self.assertFalse(result.proposed_actions[0].fast_track)
        notes = [t.text for t in result.timeline if t.kind == "triage"]
        self.assertTrue(any("raised from SEV-4 to SEV-2" in n for n in notes))
        self.assertTrue(any("Fast-track withdrawn" in n for n in notes))

    def test_model_cannot_claim_fast_track(self):
        mcp = MockMCPClient("low_sev_recurring")
        agent = SREAgent(MockReasoningLLM(), mcp)
        executor = agent._executor(INCIDENT, run(MockMCPClient("bad_deploy")))
        args = {
            "action": "rollback_deploy", "target_service": SERVICE, "deploy_id": "dep-7731", "flag_name": None,
            "flag_value": None, "replicas": None, "team": None, "rationale": "r", "evidence": ["e"],
            "risk": {"level": "low", "description": "d"}, "expected_effect": "x",
            "how_to_verify": {"metric": "error_rate", "comparison": "below", "threshold": 0.01, "within_minutes": 10},
            "fast_track": True,
        }
        out = executor.execute("propose_action", json.dumps(args))
        self.assertEqual(out["error"], "rejected")
        self.assertIn("unexpected fields ['fast_track']", out["detail"])


if __name__ == "__main__":
    unittest.main()
