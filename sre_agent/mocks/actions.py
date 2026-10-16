"""Stand-in for the automation backend that runs approved actions (e.g. a deploy tool or flag service)."""

from __future__ import annotations

from typing import Any

from sre_agent.mocks.fixtures import TENANT
from sre_agent.mocks.mcp import MockMCPClient
from sre_agent.models import ProposedAction


class MockActionRunner:
    def __init__(self, mcp: MockMCPClient, fail: bool = False):
        self.mcp = mcp
        self.fail = fail
        self.executed: list[dict[str, Any]] = []

    def run(self, proposal: ProposedAction, *, tenant_id: str, approved_by: str) -> dict[str, Any]:
        if tenant_id != TENANT:
            raise PermissionError(f"no access for tenant '{tenant_id}'")
        if self.fail:
            raise RuntimeError(f"{proposal.action} failed: deploy tool returned 503")
        self.executed.append({"action": proposal.action, "params": proposal.params, "approved_by": approved_by})
        self.mcp.apply_effect(proposal.action)
        return {"status": "done", "action": proposal.action}
