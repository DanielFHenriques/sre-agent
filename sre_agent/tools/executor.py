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
from sre_agent.timeline import utc_now
from sre_agent.triage import fast_track_reason


class MCPClient(Protocol):
    def call_tool(
        self, server: str, tool: str, arguments: dict[str, Any], *, tenant_id: str, timeout_s: float
    ) -> dict[str, Any]: ...


class ToolRejected(Exception):
    pass


# How far from a metric's baseline a verification threshold may be. Illustrative; per-metric in practice.
MAX_BASELINE_RATIO = 3.0


class ToolExecutor:
    def __init__(
        self,
        mcp: MCPClient,
        incident: IncidentRef,
        max_output_chars: int = 4000,
        clock: Callable[[], float] = time.perf_counter,
        transcript: list[dict[str, Any]] | None = None,
        prior_proposals: list[ProposedAction] | None = None,
        prior_log: list[ToolCallRecord] | None = None,
        allowed_tools: set[str] | None = None,
        local_servers: dict[str, Any] | None = None,
        now: Callable[[], str] = utc_now,
    ):
        self.mcp = mcp
        self.incident = incident
        self.max_output_chars = max_output_chars
        self.clock = clock
        # None = every registered tool. The question-answering path passes only the read tools.
        self.allowed_tools = allowed_tools
        # First-party "servers" (the agent's memory) handled in-process instead of over MCP.
        self.local_servers = local_servers or {}
        self.now = now
        self._related_services: set[str] = set()  # downstream dependencies, for memory recall
        self._seen_memory_fixes: list[dict[str, Any]] = []  # verified fixes recalled from memory
        # The prior_* arguments let a resumed investigation carry forward what this incident already tried.
        self.proposed_actions: list[ProposedAction] = list(prior_proposals or [])
        self.log: list[ToolCallRecord] = list(prior_log or [])
        # Facts the agent has actually seen in tool output, which proposals are checked against:
        # a rollback may only target a seen deploy; a verification check is anchored on a seen metric.
        self._seen_deploy_ids: set[str] = set()
        self._seen_metrics: dict[str, dict[str, Any]] = {}  # latest summary per metric
        self._seen_similar: list[dict[str, Any]] = []  # past incidents, for fast-track eligibility
        self.triage: dict[str, Any] | None = None  # set by the agent once triage has run
        for message in transcript or []:  # rebuilt from the transcript when resuming or recovering
            if message["role"] == "tool":
                try:
                    content = json.loads(message["content"])
                except json.JSONDecodeError:
                    continue
                if isinstance(content, dict):
                    self._observe(content)

    def execute(self, name: str, raw_arguments: str) -> dict[str, Any]:
        start = self.clock()
        args: dict[str, Any] = {}
        try:
            tool = REGISTRY.get(name)
            if tool is None:
                raise ToolRejected(f"unknown tool '{name}'")
            if self.allowed_tools is not None and name not in self.allowed_tools:
                raise ToolRejected(f"'{name}' is not available here")
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
            if server in self.local_servers:
                result = self.local_servers[server].call_tool(
                    tool, args, tenant_id=self.incident.tenant_id, now=self.now(), related=self._related_services
                )
            else:
                result = self.mcp.call_tool(server, tool, args, tenant_id=self.incident.tenant_id, timeout_s=timeout_s)
        except TimeoutError:
            return {"error": "timeout", "detail": f"{server}.{tool} did not answer within {timeout_s}s"}
        except Exception as e:  # MCP server down, auth expired, rate limited...
            return {"error": "unavailable", "detail": f"{server}.{tool} failed: {type(e).__name__}: {e}"}
        self._observe(result)
        return result

    def _observe(self, result: dict[str, Any]) -> None:
        for change in result.get("changes", []):
            if "deploy_id" in change:
                self._seen_deploy_ids.add(change["deploy_id"])
        if "metric" in result and "baseline" in result and "current" in result:
            self._seen_metrics[result["metric"]] = result
        if isinstance(result.get("incidents"), list):
            self._seen_similar = result["incidents"]
        if isinstance(result.get("downstream"), list):
            self._related_services = set(result["downstream"])
        for m in result.get("memories", []) if isinstance(result.get("memories"), list) else []:
            for action in m.get("verified_actions", []):
                self._seen_memory_fixes.append({"incident_id": m["incident_id"], "resolution_action": action, "verified": True})

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
        if verify is not None:
            self._check_verification(verify)

        params = {p: args[p] for p in ["target_service", *needed]}
        for existing in self.proposed_actions:
            if existing.action == action and existing.params == params:
                if existing.verification_verdict == "did_not_work":
                    raise ToolRejected(
                        f"{action} {params} was already approved and run, and did not work. Propose something else."
                    )
                if existing.status == "pending_approval":
                    return {"status": "pending_approval", "note": "Already proposed. Waiting on a human."}
                if existing.status == "rejected":
                    raise ToolRejected(f"{action} {params} was rejected by {existing.decided_by}. Do not re-propose.")
        a_fix_failed = any(p.verification_verdict == "did_not_work" for p in self.proposed_actions)
        fast_track = fast_track_reason(self.triage, action, self._seen_similar + self._seen_memory_fixes, a_fix_failed)
        self.proposed_actions.append(
            ProposedAction(
                action=action,
                params=params,
                rationale=args["rationale"],
                evidence=args["evidence"],
                risk=args["risk"],
                expected_effect=args["expected_effect"],
                how_to_verify=verify,
                fast_track=fast_track is not None,
                fast_track_reason=fast_track,
            )
        )
        return {
            "status": "pending_approval",
            "note": "NOT executed. Shown to the responder for approval. Do not assume it has happened."
            + (f" Offered as a one-click (fast-track) approval: {fast_track}" if fast_track else ""),
        }

    def _check_verification(self, verify: dict[str, Any]) -> None:
        """The model writes its own success criterion, so make sure it can actually tell success apart.

        Two ways a check proves nothing:
          - it is far from normal: "error_rate below 0.5" when normal is 0.004 calls a fix that
            leaves errors 100x above normal a success;
          - it already passes: a check that is true before the fix runs is also true if the fix
            does nothing.
        """
        if not 1 <= verify["within_minutes"] <= 60:
            raise ToolRejected("how_to_verify.within_minutes must be between 1 and 60")
        metric, comparison, threshold = verify["metric"], verify["comparison"], verify["threshold"]
        seen = self._seen_metrics.get(metric)
        if seen is None:
            raise ToolRejected(
                f"query {metric} with datadog_query_metrics before using it in how_to_verify, "
                "so the check can be anchored on its baseline"
            )
        baseline, current = seen["baseline"], seen["current"]
        if baseline > 0:
            if comparison == "below" and threshold > MAX_BASELINE_RATIO * baseline:
                raise ToolRejected(
                    f"how_to_verify threshold {threshold} is more than {MAX_BASELINE_RATIO:g}x {metric}'s baseline "
                    f"{baseline}: the fix would count as working while {metric} is still far from normal"
                )
            if comparison == "above" and threshold < baseline / MAX_BASELINE_RATIO:
                raise ToolRejected(
                    f"how_to_verify threshold {threshold} is less than 1/{MAX_BASELINE_RATIO:g} of {metric}'s "
                    f"baseline {baseline}: the fix would count as working while {metric} is still far from normal"
                )
        already = current < threshold if comparison == "below" else current > threshold
        if already:
            raise ToolRejected(
                f"how_to_verify already passes now ({metric} is {current}, {comparison} {threshold}), "
                "so it cannot show whether the fix worked"
            )

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
