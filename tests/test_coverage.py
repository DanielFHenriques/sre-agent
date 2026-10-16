import json
import unittest

from sre_agent.agent import SREAgent, briefing
from sre_agent.connectors import Router
from sre_agent.coverage import coverage, summary
from sre_agent.llm import MockReasoningLLM
from sre_agent.mocks.fixtures import SERVICE, TENANT
from sre_agent.mocks.mcp import MockMCPClient
from sre_agent.models import IncidentRef
from sre_agent.timeline import handover_brief
from sre_agent.tools.executor import ToolExecutor

INCIDENT = IncidentRef("PD-1001", TENANT, SERVICE)

ROLLBACK = {
    "action": "rollback_deploy", "target_service": SERVICE, "deploy_id": "dep-7731", "flag_name": None,
    "flag_value": None, "replicas": None, "team": None, "rationale": "r", "evidence": ["e"],
    "risk": {"level": "low", "description": "d"}, "expected_effect": "x",
    "how_to_verify": {"metric": "error_rate", "comparison": "below", "threshold": 0.01, "within_minutes": 10},
}
PAGE = {**ROLLBACK, "action": "page_team", "deploy_id": None, "team": "team-checkout", "how_to_verify": None}


def run(scenario="logs_only"):
    mcp = MockMCPClient(scenario)
    return SREAgent(MockReasoningLLM(), mcp).run(INCIDENT), mcp


class CoverageReportTest(unittest.TestCase):
    def test_logs_only_service_states_what_the_agent_cannot_see(self):
        state, _ = run()
        cov = state.triage["coverage"]
        self.assertEqual(cov["covered"], ["changes", "logs", "support_cases"])
        self.assertEqual(cov["missing"], ["metrics", "dependency_health", "alerts"])
        self.assertTrue(any("won't propose fixes" in limit for limit in cov["limits"]))
        self.assertEqual(cov["unsupported"], ["launchdarkly"])

    def test_triage_says_where_severity_came_from_without_metrics(self):
        state, _ = run()
        t = state.triage
        self.assertEqual((t["severity"], t["customer_facing"]), ("SEV-3", True))
        self.assertIn("No metrics integration", t["signals"][0])

    def test_coverage_is_in_transcript_timeline_briefing_and_handover(self):
        state, _ = run()
        self.assertTrue(any("[coverage] Agent coverage: metrics NO" in str(m.get("content")) for m in state.messages))
        self.assertIn("coverage", [t.kind for t in state.timeline])
        self.assertEqual(briefing(state, INCIDENT)["coverage"]["missing"], ["metrics", "dependency_health", "alerts"])
        brief = handover_brief(state, "alice", "bob")
        self.assertIn("Agent coverage: metrics NO", brief)
        self.assertIn("Limit: No metrics", brief)

    def test_full_coverage_has_no_limits(self):
        state, _ = run("bad_deploy")
        self.assertEqual(state.triage["coverage"]["missing"], [])
        self.assertEqual(state.triage["coverage"]["limits"], [])

    def test_unreadable_integrations_are_unknown_coverage(self):
        cov = coverage(None)
        self.assertFalse(cov["known"])
        self.assertIn("unknown", summary(cov))


class CoverageLimitsTest(unittest.TestCase):
    def test_without_metrics_the_agent_advises_instead_of_proposing(self):
        state, mcp = run()
        self.assertEqual(state.proposed_actions, [])
        self.assertIn("can't propose or verify a rollback", state.assessment["recommended_next_step"])
        self.assertIn("dep-7731", state.assessment["recommended_next_step"])
        self.assertEqual(state.assessment["hypotheses"][0]["cause"], "Bad deploy v2.14.0")
        # It never tried to use what the client hasn't integrated.
        self.assertFalse({"datadog_query_metrics", "datadog_get_dependency_health"} & {c["tool"] for c in mcp.calls})

    def test_executor_refuses_a_fix_it_could_not_verify(self):
        state, mcp = run()
        executor = SREAgent(MockReasoningLLM(), mcp)._executor(INCIDENT, state)
        out = executor.execute("propose_action", json.dumps(ROLLBACK))
        self.assertEqual(out["error"], "rejected")
        self.assertIn("no integration for metrics", out["detail"])
        self.assertEqual(executor.execute("propose_action", json.dumps(PAGE))["status"], "pending_approval")

    def test_rollback_needs_change_tracking_too(self):
        executor = ToolExecutor(MockMCPClient("bad_deploy"), INCIDENT)
        executor.router = Router([{"vendor": "datadog", "provides": ["metrics"], "config": {}}])
        out = executor.execute("propose_action", json.dumps(ROLLBACK))
        self.assertIn("no integration for change tracking", out["detail"])


if __name__ == "__main__":
    unittest.main()
