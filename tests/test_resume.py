import json
import unittest

from sre_agent.agent import Budget, SREAgent
from sre_agent.llm import MockReasoningLLM
from sre_agent.mocks.actions import MockActionRunner
from sre_agent.mocks.fixtures import SERVICE, TENANT
from sre_agent.mocks.mcp import MockMCPClient
from sre_agent.models import IncidentRef, Verification
from sre_agent.remediation import Remediator

INCIDENT = IncidentRef("PD-1001", TENANT, SERVICE)


def first_fix_fails(budget=Budget()):
    """deploy_red_herring: investigate, approve the rollback, watch it fail."""
    mcp = MockMCPClient("deploy_red_herring")
    agent = SREAgent(MockReasoningLLM(), mcp, budget)
    remediator = Remediator(MockActionRunner(mcp), mcp, INCIDENT, sleep=lambda s: None)
    result = agent.run(INCIDENT)
    verification = remediator.approve(result.proposed_actions[0], "alice", transcript=result.messages)
    return agent, remediator, result, verification


class ResumeTest(unittest.TestCase):
    def test_red_herring_first_points_at_the_deploy(self):
        _, _, result, verification = first_fix_fails()
        self.assertEqual([p.action for p in result.proposed_actions], ["rollback_deploy"])
        self.assertEqual(verification.verdict, "did_not_work")

    def test_resume_finds_real_cause_and_proposes_something_else(self):
        agent, remediator, first, verification = first_fix_fails()

        second = agent.resume(INCIDENT, first, verification)

        self.assertEqual(second.cycle, 2)
        self.assertEqual(second.stop_reason, "assessment_submitted")
        new = [p for p in second.proposed_actions if p.status == "pending_approval"]
        self.assertEqual([p.action for p in new], ["scale_out"])
        causes = [h["cause"] for h in second.assessment["hypotheses"]]
        self.assertIn("Traffic spike", causes[0])
        ruled_out = next(h for h in second.assessment["hypotheses"] if "deploy" in h["cause"])
        self.assertIn("did not recover", ruled_out["evidence_against"][0])

        self.assertEqual(remediator.approve(new[0], "alice").verdict, "worked")

    def test_resume_continues_the_same_transcript(self):
        agent, _, first, verification = first_fix_fails()
        second = agent.resume(INCIDENT, first, verification)

        self.assertEqual(second.messages[: len(first.messages)], first.messages)
        notes = [m["content"] for m in second.messages if m["role"] == "user"]
        self.assertTrue(any(n.startswith("[automation] rollback_deploy") for n in notes))
        self.assertTrue(any(n.startswith("[re-investigate]") for n in notes))
        # The earlier result is not mutated by the resume.
        self.assertFalse(any(str(m.get("content")).startswith("[re-investigate]") for m in first.messages))

    def test_resume_looks_wider_than_the_first_pass(self):
        agent, _, first, verification = first_fix_fails()
        second = agent.resume(INCIDENT, first, verification)
        new_reads = second.tool_log[len(first.tool_log):]
        metrics = {r.arguments.get("metric") for r in new_reads if r.tool == "datadog_query_metrics"}
        self.assertTrue({"request_rate", "db_pool_usage"} <= metrics)

    def test_executor_refuses_to_repeat_a_failed_fix(self):
        agent, _, first, verification = first_fix_fails()

        class RepeatFailedFix:
            def complete(self, messages, tools, tool_choice="auto"):
                if isinstance(tool_choice, dict):
                    return MockReasoningLLM().complete(messages, tools, tool_choice)
                args = json.loads(first.messages[_propose_index(first.messages)]["tool_calls"][0]["function"]["arguments"])
                call = {"id": f"again_{len(messages)}", "type": "function",
                        "function": {"name": "propose_action", "arguments": json.dumps(args)}}
                return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]}

        stubborn = SREAgent(RepeatFailedFix(), agent.mcp)
        second = stubborn.resume(INCIDENT, first, verification)

        rejected = [r for r in second.tool_log[len(first.tool_log):] if r.tool == "propose_action"]
        self.assertTrue(rejected)
        self.assertTrue(all(r.status == "rejected" and "did not work" in r.error for r in rejected))
        self.assertEqual(len([p for p in second.proposed_actions if p.action == "rollback_deploy"]), 1)

    def test_rollback_guardrail_survives_resume(self):
        agent, _, first, verification = first_fix_fails()
        second = agent.resume(INCIDENT, first, verification)
        # The deploy seen in cycle 1 is still known to the resumed executor (no "not returned" rejection).
        self.assertFalse(any(r.error and "was not returned" in r.error for r in second.tool_log))

    def test_cycle_limit_hands_back_to_humans_without_calling_the_model(self):
        agent, _, first, verification = first_fix_fails(budget=Budget(max_cycles=1))

        class NoCalls:
            def complete(self, *a, **k):
                raise AssertionError("model must not be called past the cycle limit")

        limited = SREAgent(NoCalls(), agent.mcp, Budget(max_cycles=1))
        result = limited.resume(INCIDENT, first, verification)

        self.assertEqual(result.stop_reason, "cycle_limit")
        self.assertIn("Escalate", result.assessment["recommended_next_step"])
        self.assertIn("rollback_deploy", result.assessment["recommended_next_step"])

    def test_resume_only_for_failed_fixes(self):
        agent, _, first, _ = first_fix_fails()
        with self.assertRaises(ValueError):
            agent.resume(INCIDENT, first, Verification("rollback_deploy", "worked", "ok"))


def _propose_index(messages):
    return next(i for i, m in enumerate(messages)
                if any(c["function"]["name"] == "propose_action" for c in m.get("tool_calls") or []))


if __name__ == "__main__":
    unittest.main()
