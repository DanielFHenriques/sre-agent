import json
import unittest

from sre_agent.agent import Budget, SREAgent
from sre_agent.llm import MockReasoningLLM
from sre_agent.mocks.fixtures import SERVICE, TENANT
from sre_agent.mocks.mcp import MockMCPClient
from sre_agent.models import IncidentRef

INCIDENT = IncidentRef("PD-1001", TENANT, SERVICE)


def run(scenario, failing=None, llm=None, budget=Budget()):
    mcp = MockMCPClient(scenario, failing)
    return SREAgent(llm or MockReasoningLLM(), mcp, budget).run(INCIDENT), mcp


class LoopingLLM:
    """A model that never concludes: keeps asking for the same runbook."""

    def complete(self, messages, tools, tool_choice="auto"):
        if isinstance(tool_choice, dict):
            return MockReasoningLLM().complete(messages, tools, tool_choice)
        call = {"id": f"loop_{len(messages)}", "type": "function",
                "function": {"name": "runbook_get", "arguments": json.dumps({"service_id": SERVICE})}}
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]}


class AgentScenarioTest(unittest.TestCase):
    def test_bad_deploy_proposes_rollback_of_the_right_deploy(self):
        result, _ = run("bad_deploy")
        self.assertEqual(result.stop_reason, "assessment_submitted")
        self.assertEqual([a.action for a in result.proposed_actions], ["rollback_deploy"])
        self.assertEqual(result.proposed_actions[0].params["deploy_id"], "dep-7731")
        self.assertEqual(result.assessment["confidence"], "high")

    def test_dependency_outage_does_not_roll_back(self):
        result, _ = run("dependency_outage")
        actions = [a.action for a in result.proposed_actions]
        self.assertNotIn("rollback_deploy", actions)
        self.assertEqual(actions, ["toggle_feature_flag", "page_team"])
        self.assertIn("payments-gateway", result.assessment["hypotheses"][0]["cause"])

    def test_logs_down_still_answers_with_lower_confidence(self):
        result, _ = run("bad_deploy", failing={"datadog_search_logs"})
        self.assertEqual(result.stop_reason, "assessment_submitted")
        self.assertEqual(result.assessment["confidence"], "medium")
        self.assertTrue(any("logs_search" in q for q in result.assessment["open_questions"]))

    def test_dependency_data_down_means_no_action(self):
        result, _ = run("bad_deploy", failing={"datadog_get_dependency_health"})
        self.assertEqual(result.proposed_actions, [])
        self.assertEqual(result.assessment["confidence"], "low")

    def test_assessment_leads_with_business_impact(self):
        result, _ = run("bad_deploy")
        impact = result.assessment["impact"]
        self.assertEqual(impact["business_metric"], "successful checkouts per minute")
        self.assertEqual(impact["change_pct"], -20.0)
        self.assertEqual(impact["suggested_severity"], "SEV-2")

    def test_impact_unknown_when_kpi_unavailable(self):
        result, _ = run("bad_deploy", failing={"datadog_query_metrics"})
        self.assertEqual(result.assessment["impact"]["suggested_severity"], "unknown")

    def test_every_proposal_has_risk_and_remediations_are_verifiable(self):
        for scenario in ("bad_deploy", "dependency_outage"):
            result, _ = run(scenario)
            for p in result.proposed_actions:
                self.assertTrue(p.risk["description"])
                self.assertTrue(p.expected_effect)
                if p.action != "page_team":
                    self.assertIsNotNone(p.how_to_verify, f"{scenario}: {p.action}")

    def test_nothing_executes_actions(self):
        result, mcp = run("bad_deploy")
        self.assertTrue(result.proposed_actions)
        self.assertTrue(all(a.status == "pending_approval" for a in result.proposed_actions))
        self.assertNotIn("propose_action", [c["tool"] for c in mcp.calls])

    def test_every_mcp_call_scoped_to_incident_tenant(self):
        _, mcp = run("dependency_outage")
        self.assertEqual({c["tenant_id"] for c in mcp.calls}, {TENANT})


class AgentStopConditionTest(unittest.TestCase):
    def test_stall_is_detected_and_final_answer_forced(self):
        result, _ = run("bad_deploy", llm=LoopingLLM())
        self.assertEqual(result.stop_reason, "stalled")
        self.assertIsNotNone(result.assessment)

    def test_step_budget_forces_final_answer(self):
        result, _ = run("bad_deploy", budget=Budget(max_steps=1))
        self.assertEqual(result.stop_reason, "step_budget")
        self.assertEqual(result.steps, 1)
        self.assertIn("summary", result.assessment)

    def test_transcript_is_valid_openai_tool_sequence(self):
        result, _ = run("bad_deploy")
        pending: set[str] = set()
        for m in result.messages:
            if m["role"] == "assistant":
                self.assertFalse(pending, "assistant turn before all tool calls were answered")
                pending = {c["id"] for c in m.get("tool_calls", [])}
            elif m["role"] == "tool":
                pending.remove(m["tool_call_id"])
        self.assertFalse(pending)


if __name__ == "__main__":
    unittest.main()
