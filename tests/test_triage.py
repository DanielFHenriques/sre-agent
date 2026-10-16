import unittest

from sre_agent.agent import SREAgent
from sre_agent.llm import MockReasoningLLM
from sre_agent.mocks.fixtures import SERVICE, TENANT
from sre_agent.mocks.mcp import MockMCPClient
from sre_agent.models import IncidentRef
from sre_agent.triage import raise_only, triage

INCIDENT = IncidentRef("PD-1001", TENANT, SERVICE)
DETAILS = {"urgency": "low", "onset_estimate": "2026-10-16T02:54:00Z", "now": "2026-10-16T03:05:00Z"}


def kpi(baseline, current):
    return {"metric": "business_kpi", "baseline": baseline, "current": current}


def cases(related, enterprise=0):
    return {"since": "2026-10-16T02:54:00Z", "related_count": related, "enterprise_count": enterprise,
            "baseline_per_hour": 1.5, "sample": []}


class TriageRulesTest(unittest.TestCase):
    def test_small_kpi_dip_and_no_cases_is_low(self):
        t = triage(DETAILS, kpi(950, 930), cases(0))
        self.assertEqual((t["severity"], t["priority"], t["response_mode"]), ("SEV-4", "P4", "low"))
        self.assertIs(t["customer_facing"], False)

    def test_customer_cases_raise_a_quiet_graph(self):
        """The graphs say -2%, customers say otherwise: customer reports win."""
        t = triage(DETAILS, kpi(950, 930), cases(5))
        self.assertEqual(t["severity"], "SEV-3")
        self.assertTrue(t["customer_facing"])
        self.assertIn("raised to SEV-3", t["signals"][1])

    def test_enterprise_cases_raise_further_and_bump_priority(self):
        t = triage(DETAILS, kpi(950, 930), cases(4, enterprise=1))
        self.assertEqual((t["severity"], t["priority"], t["response_mode"]), ("SEV-2", "P1", "major"))
        self.assertIn("customer liaison", t["recommendation"])

    def test_a_couple_of_cases_is_not_a_burst(self):
        self.assertIs(triage(DETAILS, kpi(950, 930), cases(2))["customer_facing"], False)

    def test_unknown_customer_impact_is_never_low(self):
        """SEV-4 by KPI, but we could not check customer reports: don't treat it as low-impact."""
        for c in (None, {"error": "timeout"}, {"truncated": True, "content": "{..."}):
            t = triage(DETAILS, kpi(950, 930), c)
            self.assertEqual(t["severity"], "SEV-4")
            self.assertIsNone(t["customer_facing"])
            self.assertEqual(t["response_mode"], "standard", c)

    def test_unknown_kpi_defaults_to_sev3(self):
        t = triage(DETAILS, {"error": "timeout"}, cases(0))
        self.assertEqual(t["severity"], "SEV-3")
        self.assertIn("unavailable", t["signals"][0])

    def test_high_urgency_bumps_priority_not_severity(self):
        t = triage({**DETAILS, "urgency": "high"}, kpi(950, 930), cases(0))
        self.assertEqual((t["severity"], t["priority"]), ("SEV-4", "P3"))

    def test_investigation_can_raise_but_never_lower(self):
        t = triage(DETAILS, kpi(950, 930), cases(0))
        self.assertIsNone(raise_only(t, "SEV-5", "looks fine"))
        self.assertEqual(t["severity"], "SEV-4")
        note = raise_only(t, "SEV-2", "all checkouts in EU failing")
        self.assertIn("raised from SEV-4 to SEV-2", note)
        self.assertEqual((t["severity"], t["response_mode"]), ("SEV-2", "major"))


class NoSalesforce(MockMCPClient):
    def call_tool(self, server, tool, arguments, **kw):
        out = super().call_tool(server, tool, arguments, **kw)
        if tool == "pagerduty_list_service_integrations":
            out = {"integrations": [i for i in out["integrations"] if i["vendor"] != "salesforce"]}
        return out


class TriageInAgentTest(unittest.TestCase):
    def test_triage_runs_before_the_model_and_is_in_transcript_and_timeline(self):
        class RecordFirstCall(MockReasoningLLM):
            first = None

            def complete(self, messages, tools, tool_choice="auto"):
                if RecordFirstCall.first is None:
                    RecordFirstCall.first = list(messages)
                return super().complete(messages, tools, tool_choice)

        result = SREAgent(RecordFirstCall(), MockMCPClient("bad_deploy")).run(INCIDENT)
        self.assertEqual((result.triage["severity"], result.triage["priority"]), ("SEV-2", "P1"))
        self.assertTrue(RecordFirstCall.first[-1]["content"].startswith("[triage] SEV-2 / P1"))
        self.assertEqual(result.timeline[1].kind, "triage")

    def test_prefetch_only_queries_integrated_sources(self):
        mcp = NoSalesforce("bad_deploy")
        result = SREAgent(MockReasoningLLM(), mcp).run(INCIDENT)
        self.assertNotIn("salesforce_search_cases", [c["tool"] for c in mcp.calls])
        self.assertIsNone(result.triage["customer_facing"])
        self.assertIn("No support-case integration", " ".join(result.triage["signals"]))

    def test_support_cases_failing_does_not_break_triage(self):
        result = SREAgent(MockReasoningLLM(), MockMCPClient("bad_deploy", {"salesforce_search_cases"})).run(INCIDENT)
        self.assertEqual(result.triage["severity"], "SEV-2")  # still from the KPI
        self.assertIsNone(result.triage["customer_facing"])


if __name__ == "__main__":
    unittest.main()
