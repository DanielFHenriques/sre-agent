import json
import re
import unittest

from sre_agent.mocks.fixtures import SERVICE, TENANT
from sre_agent.mocks.mcp import MockMCPClient
from sre_agent.models import IncidentRef
from sre_agent.tools.executor import ToolExecutor
from sre_agent.tools.registry import openai_tools

INCIDENT = IncidentRef("PD-1001", TENANT, SERVICE)


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

    def test_read_tool_injects_tenant(self):
        out = self.ex.execute("runbook_get", json.dumps({"service_id": SERVICE}))
        self.assertIn("failure_modes", out)
        self.assertEqual(self.mcp.calls[-1]["tenant_id"], TENANT)

    def test_extra_arguments_rejected(self):
        out = self.ex.execute("runbook_get", json.dumps({"service_id": SERVICE, "tenant_id": "other-corp"}))
        self.assertEqual(out["error"], "rejected")
        self.assertEqual(self.mcp.calls, [])

    def test_unknown_tool_rejected(self):
        out = self.ex.execute("kubectl_delete_namespace", "{}")
        self.assertEqual(out["error"], "rejected")

    def test_tool_failure_becomes_data(self):
        ex = ToolExecutor(MockMCPClient("bad_deploy", {"datadog_search_logs"}), INCIDENT)
        out = ex.execute(
            "datadog_search_logs",
            json.dumps({"service_id": SERVICE, "level": "error", "query": None, "lookback_minutes": 30}),
        )
        self.assertEqual(out["error"], "timeout")
        self.assertEqual(ex.log[-1].status, "error")

    def test_rollback_requires_deploy_seen_in_this_investigation(self):
        out = self.ex.execute("propose_action", rollback_args())
        self.assertEqual(out["error"], "rejected")

        self.ex.execute("changes_list_recent", json.dumps({"service_id": SERVICE, "lookback_minutes": 120}))
        out = self.ex.execute("propose_action", rollback_args())
        self.assertEqual(out["status"], "pending_approval")
        self.assertEqual(len(self.ex.proposed_actions), 1)

    def test_rollback_of_made_up_deploy_rejected(self):
        self.ex.execute("changes_list_recent", json.dumps({"service_id": SERVICE, "lookback_minutes": 120}))
        out = self.ex.execute("propose_action", rollback_args(deploy_id="dep-9999"))
        self.assertEqual(out["error"], "rejected")

    def test_proposal_never_reaches_an_mcp_server(self):
        self.ex.execute("changes_list_recent", json.dumps({"service_id": SERVICE, "lookback_minutes": 120}))
        self.ex.execute("propose_action", rollback_args())
        self.assertEqual([c["tool"] for c in self.mcp.calls], ["changes_list_recent"])

    def test_duplicate_proposal_not_recorded_twice(self):
        self.ex.execute("changes_list_recent", json.dumps({"service_id": SERVICE, "lookback_minutes": 120}))
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
