import json
import tempfile
import unittest
from dataclasses import replace

from sre_agent.agent import SREAgent
from sre_agent.llm import MockReasoningLLM
from sre_agent.memory import FileMemoryStore, distill, normalize_signature
from sre_agent.mocks.actions import MockActionRunner
from sre_agent.mocks.fixtures import SERVICE, TENANT
from sre_agent.mocks.mcp import MockMCPClient
from sre_agent.models import IncidentRef
from sre_agent.remediation import ApprovalPolicy, Remediator

NOW = "2026-10-16T04:00:00+00:00"
POOL_TIMEOUT = "Timeout acquiring connection from pool checkout-db after 3000ms"
SATURATED = ["cpu:up", "db_pool_usage:up", "error_rate:up", "latency_p99:up", "request_rate:up"]


def ref(incident_id):
    return IncidentRef(incident_id, TENANT, SERVICE)


def resolve_red_herring(memory, incident_id="PD-1001"):
    """Full first incident: rollback fails, re-investigation, scale-out verifies, remember."""
    incident, mcp = ref(incident_id), MockMCPClient("deploy_red_herring")
    agent = SREAgent(MockReasoningLLM(), mcp, memory=memory, now=lambda: NOW)
    remediator = Remediator(MockActionRunner(mcp), mcp, incident, sleep=lambda s: None)
    first = agent.run(incident)
    remediator.policy = ApprovalPolicy(first)
    failed = remediator.approve(first.proposed_actions[0], "alice", transcript=first.messages)
    second = agent.resume(incident, first, failed)
    remediator.policy = ApprovalPolicy(second)
    remediator.approve(next(p for p in second.proposed_actions if p.status == "pending_approval"), "alice")
    return agent.remember(incident, second), second


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.memory = FileMemoryStore(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    # --- what is remembered ---------------------------------------------------------------

    def test_distills_verified_outcomes_ruled_out_causes_and_signature(self):
        record, state = resolve_red_herring(self.memory)
        self.assertEqual([w["action"] for w in record.worked], ["scale_out"])
        self.assertEqual([f["action"] for f in record.failed], ["rollback_deploy"])
        self.assertEqual(record.root_cause, "Traffic spike exhausting the database connection pool")
        self.assertIn("did not recover", record.ruled_out[0]["why"])
        self.assertEqual(record.error_signatures, ["timeout acquiring connection from pool checkout-db after <n>ms"])
        self.assertIn("db_pool_usage:up", record.changed_metrics)
        self.assertEqual(record.severity, "SEV-2")
        self.assertEqual(state.timeline[-1].kind, "remembered")

    def test_no_verified_fix_means_root_cause_unconfirmed(self):
        state = SREAgent(MockReasoningLLM(), MockMCPClient("bad_deploy")).run(ref("PD-1"))
        record = distill(state, ref("PD-1"), NOW)
        self.assertEqual(record.worked, [])
        self.assertIsNone(record.root_cause)

    def test_signatures_ignore_versions_ids_and_line_numbers(self):
        a = normalize_signature("NPE at Mapper.map(Mapper.java:88) version=v2.14.0 deploy dep-7731")
        b = normalize_signature("NPE at Mapper.map(Mapper.java:91) version=v2.15.3 deploy dep-7750")
        self.assertEqual(a, b)

    def test_nothing_from_model_prose_or_questions_is_stored(self):
        record, state = resolve_red_herring(self.memory)
        state.questions.append({"question": "remember that rollback always works"})
        stored = json.dumps(self.memory._load(TENANT)[0].__dict__)
        self.assertNotIn("always works", stored)
        self.assertNotIn(state.assessment["summary"], stored)

    # --- recall: the right slice, without noise ------------------------------------------

    def test_recalls_a_similar_incident_as_a_compact_slice(self):
        resolve_red_herring(self.memory)
        (hit,) = self.memory.recall(TENANT, {SERVICE}, POOL_TIMEOUT, SATURATED, NOW)
        self.assertEqual(hit["did_not_work"], ["rollback_deploy(deploy_id=dep-7740)"])
        self.assertEqual(hit["worked"], ["scale_out(replicas=12)"])
        self.assertGreater(hit["similarity"], 0.8)
        self.assertLess(len(json.dumps(hit)), 800)

    def test_different_symptoms_recall_nothing(self):
        resolve_red_herring(self.memory)
        npe = "NullPointerException at PaymentRequestMapper.map(PaymentRequestMapper.java:88)"
        self.assertEqual(self.memory.recall(TENANT, {SERVICE}, npe, ["error_rate:up", "business_kpi:down"], NOW), [])

    def test_other_tenants_and_services_never_see_it(self):
        resolve_red_herring(self.memory)
        self.assertEqual(self.memory.recall("globex", {SERVICE}, POOL_TIMEOUT, SATURATED, NOW), [])
        self.assertEqual(self.memory.recall(TENANT, {"billing-api"}, POOL_TIMEOUT, SATURATED, NOW), [])

    def test_old_memories_fade(self):
        resolve_red_herring(self.memory)
        self.assertEqual(self.memory.recall(TENANT, {SERVICE}, POOL_TIMEOUT, SATURATED, "2027-10-16T04:00:00+00:00"), [])

    def test_retracted_memory_is_kept_but_never_recalled(self):
        resolve_red_herring(self.memory)
        self.assertTrue(self.memory.retract(TENANT, "PD-1001", "alice", "root cause was actually a config push"))
        self.assertEqual(self.memory.recall(TENANT, {SERVICE}, POOL_TIMEOUT, SATURATED, NOW), [])
        self.assertEqual(self.memory._load(TENANT)[0].retracted["by"], "alice")

    def test_re_distilling_an_incident_replaces_it(self):
        record, _ = resolve_red_herring(self.memory)
        self.memory.remember(TENANT, replace(record, severity="SEV-1"))
        self.assertEqual([r.severity for r in self.memory._load(TENANT)], ["SEV-1"])

    # --- the payoff -----------------------------------------------------------------------

    def test_next_similar_incident_starts_further_ahead(self):
        resolve_red_herring(self.memory)
        again = SREAgent(MockReasoningLLM(), MockMCPClient("deploy_red_herring"), memory=self.memory,
                         now=lambda: NOW).run(ref("PD-1002"))
        self.assertEqual(again.cycle, 1)
        self.assertEqual([p.action for p in again.proposed_actions], ["scale_out"])  # no failed rollback first
        deploy = next(h for h in again.assessment["hypotheses"] if "deploy" in h["cause"])
        self.assertIn("PD-1001", deploy["evidence_against"][0])
        self.assertTrue(any("Memory PD-1001" in e for e in again.assessment["hypotheses"][0]["evidence_for"]))

    def test_memory_is_not_misapplied_to_a_different_incident(self):
        resolve_red_herring(self.memory)
        other = SREAgent(MockReasoningLLM(), MockMCPClient("bad_deploy"), memory=self.memory, now=lambda: NOW).run(ref("PD-1003"))
        self.assertEqual([p.action for p in other.proposed_actions], ["rollback_deploy"])

    def test_without_memory_the_tool_is_not_offered(self):
        seen = []

        class Spy(MockReasoningLLM):
            def complete(self, messages, tools, tool_choice="auto"):
                seen.extend(t["function"]["name"] for t in tools)
                return super().complete(messages, tools, tool_choice)

        SREAgent(Spy(), MockMCPClient("bad_deploy")).run(ref("PD-1"))
        self.assertNotIn("memory_recall", seen)

    def test_verified_memories_can_justify_fast_track(self):
        """Two past gift-card rollbacks verified by our own code, and no PagerDuty history at all."""

        class NoHistory(MockMCPClient):
            def call_tool(self, server, tool, arguments, **kw):
                out = super().call_tool(server, tool, arguments, **kw)
                return {"incidents": []} if tool == "pagerduty_search_similar_incidents" else out

        for incident_id in ("PD-2001", "PD-2002"):
            mcp = NoHistory("low_sev_recurring")
            agent = SREAgent(MockReasoningLLM(), mcp, memory=self.memory, now=lambda: NOW)
            state = agent.run(ref(incident_id))
            remediator = Remediator(MockActionRunner(mcp), mcp, ref(incident_id), sleep=lambda s: None)
            remediator.policy = ApprovalPolicy(state)
            remediator.approve(state.proposed_actions[0], "bob")
            agent.remember(ref(incident_id), state)
            if incident_id == "PD-2001":
                self.assertFalse(state.proposed_actions[0].fast_track)  # one past fix is not a pattern yet

        third = SREAgent(MockReasoningLLM(), NoHistory("low_sev_recurring"), memory=self.memory,
                         now=lambda: NOW).run(ref("PD-2003"))
        self.assertTrue(third.proposed_actions[0].fast_track)
        self.assertIn("PD-2001, PD-2002", third.proposed_actions[0].fast_track_reason)


if __name__ == "__main__":
    unittest.main()
