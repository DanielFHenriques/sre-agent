import json
import re
import unittest

from sre_agent.mocks.fixtures import SERVICE, TENANT
from sre_agent.mocks.mcp import MockMCPClient
from sre_agent.models import IncidentRef
from sre_agent.tools.executor import ToolExecutor
from sre_agent.tools.registry import openai_tools

INCIDENT = IncidentRef("PD-1001", TENANT, SERVICE)


def connect(executor):
    """Read the service's integrations, as prefetch does, so capability tools can be routed."""
    executor.execute("pagerduty_list_service_integrations", json.dumps({"service_id": SERVICE}))


def rollback_args(**overrides):
    args = {
        "action": "rollback_deploy",
        "target_service": SERVICE,
        "deploy_id": "dep-7731",
        "flag_name": None,
        "flag_value": None,
        "replicas": None,
        "team": None,
        "rationale": "New NPE since v2.14.0",
        "evidence": ["NPE first seen 02:53:40, deploy at 02:51"],
        "risk": {"level": "low", "description": "Reverts the BNPL change too."},
        "expected_effect": "error_rate back to baseline within 5 min.",
        "how_to_verify": {"metric": "error_rate", "comparison": "below", "threshold": 0.01, "within_minutes": 10},
    }
    args.update(overrides)
    return json.dumps(args)


class OpenAISpecTest(unittest.TestCase):
    def test_specs_are_valid_strict_function_tools(self):
        def check_strict(schema, where):
            if schema.get("type") == "object" or "properties" in schema:
                self.assertFalse(schema["additionalProperties"], where)
                self.assertEqual(set(schema["required"]), set(schema["properties"]), where)
                for k, sub in schema["properties"].items():
                    check_strict(sub, f"{where}.{k}")
            for i, option in enumerate(schema.get("anyOf", [])):
                check_strict(option, f"{where}|{i}")
            if "items" in schema:
                check_strict(schema["items"], f"{where}[]")

        for tool in openai_tools():
            self.assertEqual(tool["type"], "function")
            fn = tool["function"]
            self.assertRegex(fn["name"], re.compile(r"^[a-zA-Z0-9_-]{1,64}$"))
            self.assertTrue(fn["strict"])
            check_strict(fn["parameters"], fn["name"])

    def test_model_cannot_pass_tenant(self):
        for tool in openai_tools():
            self.assertNotIn("tenant_id", tool["function"]["parameters"]["properties"])


class ExecutorTest(unittest.TestCase):
    def setUp(self):
        self.mcp = MockMCPClient("bad_deploy")
        self.ex = ToolExecutor(self.mcp, INCIDENT)

    def investigate(self, executor=None):
        """What a proposal is checked against: the recent deploys and the metric it will verify with."""
        ex = executor or self.ex
        connect(ex)
        ex.execute("changes_list", json.dumps({"service_id": SERVICE, "lookback_minutes": 120}))
        ex.execute("metrics_query", json.dumps({"service_id": SERVICE, "metric": "error_rate", "lookback_minutes": 30}))

    def test_read_tool_injects_tenant(self):
        out = self.ex.execute("runbook_get", json.dumps({"service_id": SERVICE}))
        self.assertIn("failure_modes", out)
        self.assertEqual(self.mcp.calls[-1]["tenant_id"], TENANT)

    def test_support_cases_come_from_salesforce_scoped_to_tenant(self):
        connect(self.ex)
        out = self.ex.execute("support_cases_search", json.dumps({"service_id": SERVICE, "since": "2026-10-16T02:54:00Z"}))
        self.assertIn("baseline_per_hour", out)
        self.assertEqual((self.mcp.calls[-1]["server"], self.mcp.calls[-1]["tenant_id"]), ("salesforce", TENANT))

    def test_extra_arguments_rejected(self):
        out = self.ex.execute("runbook_get", json.dumps({"service_id": SERVICE, "tenant_id": "other-corp"}))
        self.assertEqual(out["error"], "rejected")
        self.assertEqual(self.mcp.calls, [])

    def test_unknown_tool_rejected(self):
        out = self.ex.execute("kubectl_delete_namespace", "{}")
        self.assertEqual(out["error"], "rejected")

    def test_tool_failure_becomes_data(self):
        ex = ToolExecutor(MockMCPClient("bad_deploy", {"datadog_search_logs"}), INCIDENT)
        connect(ex)
        out = ex.execute(
            "logs_search",
            json.dumps({"service_id": SERVICE, "level": "error", "query": None, "lookback_minutes": 30}),
        )
        self.assertEqual(out["error"], "timeout")
        self.assertEqual(ex.log[-1].status, "error")

    def test_rollback_requires_deploy_seen_in_this_investigation(self):
        out = self.ex.execute("propose_action", rollback_args())
        self.assertEqual(out["error"], "rejected")

        self.investigate()
        out = self.ex.execute("propose_action", rollback_args())
        self.assertEqual(out["status"], "pending_approval")
        self.assertEqual(len(self.ex.proposed_actions), 1)

    def test_rollback_of_made_up_deploy_rejected(self):
        self.investigate()
        out = self.ex.execute("propose_action", rollback_args(deploy_id="dep-9999"))
        self.assertEqual(out["error"], "rejected")

    def test_remediation_without_verification_rejected(self):
        self.investigate()
        out = self.ex.execute("propose_action", rollback_args(how_to_verify=None))
        self.assertEqual(out["error"], "rejected")
        self.assertIn("how_to_verify", out["detail"])

    def test_empty_risk_rejected(self):
        self.investigate()
        out = self.ex.execute("propose_action", rollback_args(risk={"level": "low", "description": " "}))
        self.assertEqual(out["error"], "rejected")

    def test_malformed_verification_rejected_by_schema(self):
        self.investigate()
        out = self.ex.execute("propose_action", rollback_args(how_to_verify={"metric": "error_rate"}))
        self.assertEqual(out["error"], "rejected")
        self.assertIn("no allowed shape", out["detail"])

    def test_page_team_needs_no_metric_check(self):
        args = json.loads(rollback_args(action="page_team", deploy_id=None, team="team-payments", how_to_verify=None))
        out = self.ex.execute("propose_action", json.dumps(args))
        self.assertEqual(out["status"], "pending_approval")

    def test_lax_verification_threshold_rejected(self):
        """error_rate baseline is 0.004: 'below 0.5' would call a fix that leaves errors 100x normal a success."""
        self.investigate()
        lax = {"metric": "error_rate", "comparison": "below", "threshold": 0.5, "within_minutes": 10}
        out = self.ex.execute("propose_action", rollback_args(how_to_verify=lax))
        self.assertEqual(out["error"], "rejected")
        self.assertIn("more than 3x", out["detail"])
        self.assertEqual(self.ex.proposed_actions, [])

    def test_threshold_at_the_limit_accepted(self):
        self.investigate()
        edge = {"metric": "error_rate", "comparison": "below", "threshold": 0.012, "within_minutes": 10}
        out = self.ex.execute("propose_action", rollback_args(how_to_verify=edge))
        self.assertEqual(out["status"], "pending_approval")

    def test_lax_lower_bound_for_above_checks_rejected(self):
        """business_kpi baseline is 950: 'above 100' passes even if checkouts stay down 90%."""
        self.investigate()
        self.ex.execute("metrics_query", json.dumps({"service_id": SERVICE, "metric": "business_kpi", "lookback_minutes": 30}))
        lax = {"metric": "business_kpi", "comparison": "above", "threshold": 100, "within_minutes": 10}
        out = self.ex.execute("propose_action", rollback_args(how_to_verify=lax))
        self.assertIn("less than 1/3", out["detail"])

    def test_check_that_already_passes_rejected(self):
        """latency_p99 is 460 now (baseline 420): 'below 1000' is true before the fix, so it proves nothing."""
        self.investigate()
        self.ex.execute("metrics_query", json.dumps({"service_id": SERVICE, "metric": "latency_p99", "lookback_minutes": 30}))
        passing = {"metric": "latency_p99", "comparison": "below", "threshold": 1000, "within_minutes": 10}
        out = self.ex.execute("propose_action", rollback_args(how_to_verify=passing))
        self.assertIn("already passes now", out["detail"])

    def test_verification_metric_must_have_been_read(self):
        self.investigate()
        unread = {"metric": "db_pool_usage", "comparison": "below", "threshold": 0.5, "within_minutes": 10}
        out = self.ex.execute("propose_action", rollback_args(how_to_verify=unread))
        self.assertIn("query db_pool_usage", out["detail"])

    def test_baselines_rebuilt_from_transcript(self):
        """A resumed or recovered executor must keep the check, not start from a blank slate."""
        transcript = [{"role": "tool", "tool_call_id": "x", "content": json.dumps(
            {"metric": "error_rate", "baseline": 0.004, "current": 0.21, "change_point": None})},
            {"role": "tool", "tool_call_id": "y", "content": json.dumps(
            {"changes": [{"deploy_id": "dep-7731"}]})}]
        ex = ToolExecutor(self.mcp, INCIDENT, transcript=transcript)
        lax = {"metric": "error_rate", "comparison": "below", "threshold": 0.5, "within_minutes": 10}
        self.assertIn("more than 3x", ex.execute("propose_action", rollback_args(how_to_verify=lax))["detail"])
        self.assertEqual(ex.execute("propose_action", rollback_args())["status"], "pending_approval")
        self.assertEqual(self.mcp.calls, [])  # nothing re-fetched: it all came from the transcript

    def test_proposal_never_reaches_an_mcp_server(self):
        self.investigate()
        self.ex.execute("propose_action", rollback_args())
        self.assertEqual([c["tool"] for c in self.mcp.calls], ["pagerduty_list_service_integrations", "changes_list_recent", "datadog_query_metrics"])

    def test_duplicate_proposal_not_recorded_twice(self):
        self.investigate()
        self.ex.execute("propose_action", rollback_args())
        self.ex.execute("propose_action", rollback_args())
        self.assertEqual(len(self.ex.proposed_actions), 1)

    def test_output_truncated(self):
        ex = ToolExecutor(self.mcp, INCIDENT, max_output_chars=100)
        out = ex.execute("runbook_get", json.dumps({"service_id": SERVICE}))
        self.assertTrue(out["truncated"])
        self.assertEqual(len(out["content"]), 100)


if __name__ == "__main__":
    unittest.main()
