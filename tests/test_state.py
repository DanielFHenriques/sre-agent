import json
import tempfile
import unittest
from pathlib import Path

from sre_agent.agent import SREAgent
from sre_agent.llm import MockReasoningLLM
from sre_agent.mocks.actions import MockActionRunner
from sre_agent.mocks.fixtures import SERVICE, TENANT
from sre_agent.mocks.mcp import MockMCPClient
from sre_agent.models import IncidentRef
from sre_agent.remediation import Remediator
from sre_agent.state import ConflictError, FileStateStore, to_dict
from sre_agent.timeline import handover_brief

INCIDENT = IncidentRef("PD-1001", TENANT, SERVICE)


class ProcessKilled(BaseException):
    """Not an Exception: nothing in the agent catches it, like a real SIGKILL or OOM."""


class DieAfter:
    def __init__(self, calls):
        self.inner, self.remaining = MockReasoningLLM(), calls

    def complete(self, *args, **kwargs):
        if self.remaining == 0:
            raise ProcessKilled()
        self.remaining -= 1
        return self.inner.complete(*args, **kwargs)


class DieOnTool(MockMCPClient):
    """Dies while running a tool, after the model's tool calls were checkpointed but before replies."""

    def __init__(self, scenario, tool):
        super().__init__(scenario)
        self.die_on = tool

    def call_tool(self, server, tool, arguments, **kw):
        if tool == self.die_on:
            raise ProcessKilled()
        return super().call_tool(server, tool, arguments, **kw)


def assert_valid_tool_sequence(test, messages):
    pending = set()
    for m in messages:
        if m["role"] == "assistant":
            test.assertFalse(pending, "assistant turn before all tool calls were answered")
            pending = {c["id"] for c in m.get("tool_calls", [])}
        elif m["role"] == "tool":
            pending.remove(m["tool_call_id"])
    test.assertFalse(pending)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = FileStateStore(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_finished_run_round_trips(self):
        result = SREAgent(MockReasoningLLM(), MockMCPClient("bad_deploy"), store=self.store).run(INCIDENT)
        loaded = self.store.load(INCIDENT)
        self.assertEqual(to_dict(loaded), to_dict(result))
        self.assertEqual(loaded.stop_reason, "assessment_submitted")
        self.assertGreater(loaded.version, 1)  # checkpointed along the way, not only at the end

    def test_two_writers_conflict_instead_of_overwriting(self):
        SREAgent(MockReasoningLLM(), MockMCPClient("bad_deploy"), store=self.store).run(INCIDENT)
        alice, bob = self.store.load(INCIDENT), self.store.load(INCIDENT)
        alice.proposed_actions[0].status = "rejected"
        self.store.save(INCIDENT, alice)
        bob.proposed_actions[0].status = "approved"
        with self.assertRaises(ConflictError):
            self.store.save(INCIDENT, bob)
        self.assertEqual(self.store.load(INCIDENT).proposed_actions[0].status, "rejected")

    def test_keys_are_tenant_scoped_and_validated(self):
        SREAgent(MockReasoningLLM(), MockMCPClient("bad_deploy"), store=self.store).run(INCIDENT)
        self.assertTrue((Path(self.dir.name) / TENANT / "PD-1001.json").exists())
        self.assertIsNone(self.store.load(IncidentRef("PD-1001", "other-corp", SERVICE)))
        with self.assertRaises(ValueError):
            self.store.load(IncidentRef("../acme-corp/PD-1001", "evil", SERVICE))

    def test_no_temp_files_left_behind(self):
        SREAgent(MockReasoningLLM(), MockMCPClient("bad_deploy"), store=self.store).run(INCIDENT)
        self.assertEqual(list(Path(self.dir.name).rglob("*.tmp")), [])


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = FileStateStore(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_crash_between_steps_recovers_to_same_outcome(self):
        uninterrupted = SREAgent(MockReasoningLLM(), MockMCPClient("bad_deploy")).run(INCIDENT)

        with self.assertRaises(ProcessKilled):
            SREAgent(DieAfter(2), MockMCPClient("bad_deploy"), store=self.store).run(INCIDENT)
        checkpoint = self.store.load(INCIDENT)
        self.assertEqual((checkpoint.stop_reason, checkpoint.steps), ("in_progress", 2))

        recovered = SREAgent(MockReasoningLLM(), MockMCPClient("bad_deploy"), store=self.store).recover(INCIDENT, checkpoint)

        self.assertEqual(recovered.stop_reason, "assessment_submitted")
        self.assertEqual([p.params for p in recovered.proposed_actions], [p.params for p in uninterrupted.proposed_actions])
        self.assertEqual(recovered.assessment["hypotheses"][0]["cause"], uninterrupted.assessment["hypotheses"][0]["cause"])
        self.assertIn("restarted", [t.kind for t in recovered.timeline])
        assert_valid_tool_sequence(self, recovered.messages)

    def test_crash_mid_tool_calls_answers_dangling_calls(self):
        with self.assertRaises(ProcessKilled):
            SREAgent(MockReasoningLLM(), DieOnTool("bad_deploy", "runbook_get"), store=self.store).run(INCIDENT)
        checkpoint = self.store.load(INCIDENT)
        last = checkpoint.messages[-1]
        self.assertEqual(last["role"], "assistant")  # tool calls saved, replies never written
        self.assertIn("runbook_get", json.dumps(last["tool_calls"]))

        recovered = SREAgent(MockReasoningLLM(), MockMCPClient("bad_deploy"), store=self.store).recover(INCIDENT, checkpoint)

        assert_valid_tool_sequence(self, recovered.messages)
        self.assertEqual([p.action for p in recovered.proposed_actions], ["rollback_deploy"])

    def test_recover_refuses_a_finished_investigation(self):
        result = SREAgent(MockReasoningLLM(), MockMCPClient("bad_deploy"), store=self.store).run(INCIDENT)
        with self.assertRaises(ValueError):
            SREAgent(MockReasoningLLM(), MockMCPClient("bad_deploy")).recover(INCIDENT, result)


class TimelineAndHandoverTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = FileStateStore(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def full_red_herring(self):
        mcp = MockMCPClient("deploy_red_herring")
        agent = SREAgent(MockReasoningLLM(), mcp, store=self.store)
        remediator = Remediator(MockActionRunner(mcp), mcp, INCIDENT, sleep=lambda s: None)
        first = agent.run(INCIDENT)
        v = remediator.approve(first.proposed_actions[0], "alice", transcript=first.messages, timeline=first.timeline)
        self.store.save(INCIDENT, first)
        second = agent.resume(INCIDENT, first, v)  # continues the stored version: no conflict
        return second

    def test_timeline_records_decisions_not_tool_calls(self):
        result = self.full_red_herring()
        kinds = [t.kind for t in result.timeline]
        self.assertEqual(kinds, [
            "triggered", "triage", "proposed", "impact", "hypothesis",
            "approved", "executed", "verified",
            "reinvestigating", "proposed", "hypothesis",
        ])
        self.assertLess(len(result.timeline), len(result.tool_log))
        changed = result.timeline[-1].text
        self.assertIn("Changed leading hypothesis from 'Bad deploy v2.15.0'", changed)
        self.assertEqual(result.timeline[5].actor, "alice")

    def test_state_after_resume_is_what_is_stored(self):
        result = self.full_red_herring()
        self.assertEqual(to_dict(self.store.load(INCIDENT)), to_dict(result))
        self.assertEqual(self.store.load(INCIDENT).cycle, 2)

    def test_handover_brief_from_stored_state(self):
        self.full_red_herring()
        brief = handover_brief(self.store.load(INCIDENT), "alice", "bob")
        self.assertIn("from alice to bob", brief)
        self.assertIn("Leading hypothesis: Traffic spike", brief)
        self.assertIn("rollback_deploy(deploy_id=dep-7740): executed, verification did_not_work, by alice", brief)
        self.assertIn("[1] scale_out(replicas=12)", brief)


if __name__ == "__main__":
    unittest.main()
