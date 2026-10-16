import json
import unittest

from sre_agent.agent import SREAgent
from sre_agent.connectors import Router
from sre_agent.llm import MockReasoningLLM
from sre_agent.mocks.actions import MockActionRunner
from sre_agent.mocks.fixtures import SERVICE, TENANT
from sre_agent.mocks.mcp import MockMCPClient
from sre_agent.models import IncidentRef
from sre_agent.prompts import QA_PROMPT, SYSTEM_PROMPT
from sre_agent.remediation import ApprovalPolicy, Remediator
from sre_agent.tools.executor import ToolExecutor
from sre_agent.tools.registry import openai_tools

INCIDENT = IncidentRef("PD-1001", TENANT, SERVICE)
VENDORS = ("datadog", "new relic", "newrelic", "salesforce", "github", "splunk", "grafana")


class Integrations(MockMCPClient):
    """The same scenario, but the client has integrated a different set of tools."""

    def __init__(self, scenario, keep_vendors=None, provides_override=None, fail_integrations=False):
        super().__init__(scenario)
        self.keep, self.override, self.fail_integrations = keep_vendors, provides_override or {}, fail_integrations

    def call_tool(self, server, tool, arguments, **kw):
        if tool == "pagerduty_list_service_integrations" and self.fail_integrations:
            raise TimeoutError("pagerduty timed out")
        out = super().call_tool(server, tool, arguments, **kw)
        if tool == "pagerduty_list_service_integrations":
            items = [i for i in out["integrations"] if self.keep is None or i["vendor"] in self.keep]
            out = {"integrations": [{**i, "provides": self.override.get(i["vendor"], i["provides"])} for i in items]}
        return out


class OfferedTools(MockReasoningLLM):
    offered: list[set[str]]

    def __init__(self):
        super().__init__()
        self.offered = []

    def complete(self, messages, tools, tool_choice="auto"):
        self.offered.append({t["function"]["name"] for t in tools})
        return super().complete(messages, tools, tool_choice)


def run(mcp, llm=None):
    return SREAgent(llm or MockReasoningLLM(), mcp).run(INCIDENT)


class VendorAgnosticTest(unittest.TestCase):
    def test_same_incident_same_outcome_whichever_vendor_the_client_uses(self):
        dd_mcp, nr_mcp = MockMCPClient("bad_deploy"), MockMCPClient("bad_deploy_newrelic")
        dd, nr = run(dd_mcp), run(nr_mcp)

        self.assertEqual([p.params for p in nr.proposed_actions], [p.params for p in dd.proposed_actions])
        self.assertEqual(nr.triage["severity"], dd.triage["severity"])
        self.assertEqual(nr.assessment["hypotheses"][0]["cause"], dd.assessment["hypotheses"][0]["cause"])

        nr_servers = {c["server"] for c in nr_mcp.calls}
        self.assertIn("newrelic", nr_servers)
        self.assertNotIn("datadog", nr_servers)
        nrql = [c["arguments"]["nrql"] for c in nr_mcp.calls if c["tool"] == "nrql_query"]
        self.assertTrue(any("FROM OrderCompleted" in q for q in nrql))  # the client's own KPI, via their config

    def test_results_say_where_they_came_from(self):
        result = run(MockMCPClient("bad_deploy_newrelic"))
        sources = {json.loads(m["content"]).get("source") for m in result.messages if m["role"] == "tool"}
        self.assertTrue({"newrelic", "github_actions", "salesforce"} <= sources)

    def test_verification_also_goes_through_the_clients_integration(self):
        mcp = MockMCPClient("bad_deploy_newrelic")
        state = run(mcp)
        remediator = Remediator(MockActionRunner(mcp), mcp, INCIDENT, sleep=lambda s: None)
        remediator.policy = ApprovalPolicy(state)
        self.assertEqual(remediator.approve(state.proposed_actions[0], "alice").verdict, "worked")
        self.assertEqual(mcp.calls[-1]["server"], "newrelic")

    def test_nothing_the_model_reads_names_a_vendor(self):
        text = (SYSTEM_PROMPT + QA_PROMPT + json.dumps(openai_tools())).lower()
        for vendor in VENDORS:
            self.assertNotIn(vendor, text)


class CoverageTest(unittest.TestCase):
    def test_model_is_only_offered_what_the_service_has_integrated(self):
        llm = OfferedTools()
        run(Integrations("bad_deploy", provides_override={"datadog": ["metrics", "monitors"]}), llm)
        self.assertNotIn("logs_search", llm.offered[0])
        self.assertNotIn("dependency_health", llm.offered[0])
        self.assertIn("metrics_query", llm.offered[0])

    def test_calling_an_uncovered_capability_anyway_is_refused_as_data(self):
        mcp = Integrations("bad_deploy", keep_vendors={"salesforce"})
        ex = ToolExecutor(mcp, INCIDENT)
        ex.execute("pagerduty_list_service_integrations", json.dumps({"service_id": SERVICE}))
        out = ex.execute("logs_search", json.dumps({"service_id": SERVICE, "level": "error", "query": None, "lookback_minutes": 30}))
        self.assertEqual(out["error"], "not_integrated")
        self.assertIn("logs", out["detail"])

    def test_unknown_integrations_mean_no_capability_tools(self):
        llm = OfferedTools()
        result = run(Integrations("bad_deploy", fail_integrations=True), llm)
        capability_tools = {"metrics_query", "logs_search", "dependency_health", "alerts_list", "changes_list", "support_cases_search"}
        self.assertFalse(capability_tools & llm.offered[0])
        self.assertEqual(result.triage["severity"], "SEV-3")  # impact unknown
        self.assertEqual(result.proposed_actions, [])  # nothing it could verify

    def test_unmapped_metric_is_explained(self):
        router = Router([{"vendor": "datadog", "provides": ["metrics"], "config": {"metric_map": {}}}])
        mcp = MockMCPClient("bad_deploy")
        ex = ToolExecutor(mcp, INCIDENT)
        ex.router = router
        out = ex.execute("metrics_query", json.dumps({"service_id": SERVICE, "metric": "business_kpi", "lookback_minutes": 30}))
        self.assertEqual(out["error"], "not_mapped")
        self.assertIn("business_kpi", out["detail"])

    def test_integrations_without_a_connector_are_listed_not_used(self):
        router = Router([{"vendor": "launchdarkly", "provides": ["feature_flags"]},
                         {"vendor": "datadog", "provides": ["logs"], "config": {}}])
        self.assertEqual(router.unsupported, ["launchdarkly"])
        self.assertEqual(router.covered(), {"logs"})

    def test_first_integration_listed_wins_for_a_capability(self):
        router = Router([{"vendor": "newrelic", "provides": ["metrics"], "config": {}},
                         {"vendor": "datadog", "provides": ["metrics", "logs"], "config": {}}])
        self.assertEqual(router.routes["metrics"].vendor, "newrelic")
        self.assertEqual(router.routes["logs"].vendor, "datadog")


if __name__ == "__main__":
    unittest.main()
