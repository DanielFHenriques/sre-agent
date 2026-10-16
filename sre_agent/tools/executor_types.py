from __future__ import annotations

from typing import Any, Protocol


class MCPClient(Protocol):
    def call_tool(
        self, server: str, tool: str, arguments: dict[str, Any], *, tenant_id: str, timeout_s: float
    ) -> dict[str, Any]: ...
