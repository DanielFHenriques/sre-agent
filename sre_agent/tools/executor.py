"""Tool executor: the policy gate between the model and the outside world.

Every model-issued tool call goes through `execute`, which:
  1. rejects unknown tools and arguments that do not match the strict schema,
  2. injects the tenant from the incident context (the model never supplies it),
  3. calls the MCP server for read tools, with a timeout, turning failures into data,
  4. turns action proposals into pending approval requests after checking preconditions,
  5. truncates output and appends a line to the call log.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Protocol

from sre_agent.models import IncidentRef, ProposedAction, SideEffect, ToolCallRecord
from sre_agent.tools.registry import REGISTRY


class MCPClient(Protocol):
    def call_tool(
        self, server: str, tool: str, arguments: dict[str, Any], *, tenant_id: str, timeout_s: float
    ) -> dict[str, Any]: ...


class ToolRejected(Exception):
    pass


class ToolExecutor:
    def __init__(
        self,
        mcp: MCPClient,
        incident: IncidentRef,
        max_output_chars: int = 4000,
        clock: Callable[[], float] = time.perf_counter,
    ):
        self.mcp = mcp
        self.incident = incident
        self.max_output_chars = max_output_chars
        self.clock = clock
        self.proposed_actions: list[ProposedAction] = []
        self.log: list[ToolCallRecord] = []
        # Deploy ids the agent has actually seen in tool output. A rollback may only target one of these.
        self._seen_deploy_ids: set[str] = set()

    def execute(self, name: str, raw_arguments: str) -> dict[str, Any]:
        start = self.clock()
        args: dict[str, Any] = {}
        try:
            tool = REGISTRY.get(name)
            if tool is None:
                raise ToolRejected(f"unknown tool '{name}'")
            try:
                args = json.loads(raw_arguments or "{}")
            except json.JSONDecodeError as e:
                raise ToolRejected(f"arguments are not valid JSON: {e}") from e
            _validate(tool.parameters, args, "arguments")

            if tool.side_effect is SideEffect.READ:
                result = self._call_mcp(tool.mcp_server, name, args, tool.timeout_s)
                status = "error" if "error" in result else "ok"
            elif tool.side_effect is SideEffect.APPROVAL_REQUIRED:
                result = self._propose(args)
                status = "pending_approval"
            else:  # TERMINAL: the agent loop reads the arguments, nothing to call
                result = {"status": "accepted"}
                status = "ok"
        except ToolRejected as e:
            result = {"error": "rejected", "detail": str(e)}
            status = "rejected"

        result = self._truncate(result)
        self.log.append(
            ToolCallRecord(
                tool=name,
                arguments=args,
                status=status,
                latency_ms=round((self.clock() - start) * 1000, 2),
                output_chars=len(json.dumps(result)),
                error=result.get("detail") if status in ("error", "rejected") else None,
            )
        )
        return result

    # --- read tools ------------------------------------------------------------------------

    def _call_mcp(self, server: str | None, tool: str, args: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        assert server is not None
        try:
            result = self.mcp.call_tool(server, tool, args, tenant_id=self.incident.tenant_id, timeout_s=timeout_s)
        except TimeoutError:
            return {"error": "timeout", "detail": f"{server}.{tool} did not answer within {timeout_s}s"}
        except Exception as e:  # MCP server down, auth expired, rate limited...
            return {"error": "unavailable", "detail": f"{server}.{tool} failed: {type(e).__name__}: {e}"}
        for change in result.get("changes", []):
            if "deploy_id" in change:
                self._seen_deploy_ids.add(change["deploy_id"])
        return result

    # --- action proposals ------------------------------------------------------------------

    def _propose(self, args: dict[str, Any]) -> dict[str, Any]:
        action = args["action"]
        if args["target_service"] != self.incident.service_id:
            raise ToolRejected(
                f"target_service must be the incident's service '{self.incident.service_id}'"
            )
        if not args["evidence"]:
            raise ToolRejected("a proposal must cite at least one piece of evidence from tool results")

        needed = {
            "rollback_deploy": ["deploy_id"],
            "toggle_feature_flag": ["flag_name", "flag_value"],
            "scale_out": ["replicas"],
            "page_team": ["team"],
            "restart_service": [],
        }[action]
        missing = [p for p in needed if args.get(p) is None]
        if missing:
            raise ToolRejected(f"{action} needs {missing}")
        if action == "rollback_deploy" and args["deploy_id"] not in self._seen_deploy_ids:
            raise ToolRejected(
                f"deploy_id '{args['deploy_id']}' was not returned by changes_list_recent in this investigation"
            )
        if action == "scale_out" and not 1 <= args["replicas"] <= 50:
            raise ToolRejected("replicas must be between 1 and 50")

        # The Incident Commander cannot decide without knowing the risk, or tell later whether it worked.
        if not args["risk"]["description"].strip() or not args["expected_effect"].strip():
            raise ToolRejected("risk.description and expected_effect must say something")
        verify = args["how_to_verify"]
        if action != "page_team" and verify is None:
            raise ToolRejected(f"{action} needs how_to_verify: a metric check that shows it worked")
        if verify is not None and not 1 <= verify["within_minutes"] <= 60:
            raise ToolRejected("how_to_verify.within_minutes must be between 1 and 60")

        params = {p: args[p] for p in ["target_service", *needed]}
        for existing in self.proposed_actions:
            if existing.action == action and existing.params == params:
                return {"status": "pending_approval", "note": "Already proposed. Waiting on a human."}
        self.proposed_actions.append(
            ProposedAction(
                action=action,
                params=params,
                rationale=args["rationale"],
                evidence=args["evidence"],
                risk=args["risk"],
                expected_effect=args["expected_effect"],
                how_to_verify=verify,
            )
        )
        return {
            "status": "pending_approval",
            "note": "NOT executed. Shown to the responder for approval. Do not assume it has happened.",
        }

    def _truncate(self, result: dict[str, Any]) -> dict[str, Any]:
        text = json.dumps(result)
        if len(text) <= self.max_output_chars:
            return result
        return {"truncated": True, "content": text[: self.max_output_chars]}


def _validate(schema: dict[str, Any], value: Any, path: str) -> None:
    """Small subset of JSON Schema, enough for strict-mode tool schemas.

    OpenAI strict mode already guarantees this for its models; we re-check because the executor
    must not trust whichever model (or mock) is on the other side.
    """
    if "anyOf" in schema:
        errors = []
        for option in schema["anyOf"]:
            try:
                return _validate(option, value, path)
            except ToolRejected as e:
                errors.append(str(e))
        raise ToolRejected(f"{path}: matches no allowed shape ({'; '.join(errors)})")
    types = schema.get("type")
    types = types if isinstance(types, list) else [types]
    checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    if not any(checks[t](value) for t in types):
        raise ToolRejected(f"{path}: expected {'/'.join(types)}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolRejected(f"{path}: must be one of {schema['enum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        extra = set(value) - set(props)
        if extra:
            raise ToolRejected(f"{path}: unexpected fields {sorted(extra)}")
        missing = set(schema.get("required", [])) - set(value)
        if missing:
            raise ToolRejected(f"{path}: missing fields {sorted(missing)}")
        for key, sub in props.items():
            if key in value:
                _validate(sub, value[key], f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _validate(schema["items"], item, f"{path}[{i}]")
