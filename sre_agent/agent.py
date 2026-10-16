"""The core loop: prefetch, then model <-> tools until an assessment, a budget, or a stall.

The transcript is kept in OpenAI Chat Completions format. It is the whole working state of an
investigation, which is what you would persist to survive a restart (not done in this sketch).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from sre_agent.llm import LLMClient
from sre_agent.models import AgentResult, IncidentRef
from sre_agent.prompts import SYSTEM_PROMPT
from sre_agent.tools.executor import MCPClient, ToolExecutor
from sre_agent.tools.registry import openai_tools

FORCE_ASSESSMENT = {"type": "function", "function": {"name": "submit_assessment"}}


@dataclass(frozen=True)
class Budget:
    max_steps: int = 6  # model calls, excluding the forced final one
    max_tool_calls: int = 20
    max_wall_clock_s: float = 120.0  # the responder should see something within ~2 min
    max_stalled_steps: int = 2  # consecutive steps that only repeat earlier calls


def prefetch_calls(incident: IncidentRef) -> list[tuple[str, dict[str, Any]]]:
    """Always needed, so fetched before the first model call. Saves several round-trips."""
    svc = incident.service_id
    return [
        ("pagerduty_get_incident", {"incident_id": incident.incident_id}),
        ("pagerduty_get_service_context", {"service_id": svc}),
        ("changes_list_recent", {"service_id": svc, "lookback_minutes": 180}),
        ("datadog_list_triggered_monitors", {"service_id": svc}),
    ]


class SREAgent:
    def __init__(
        self,
        llm: LLMClient,
        mcp: MCPClient,
        budget: Budget = Budget(),
        clock: Callable[[], float] = time.monotonic,
        on_message: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.llm = llm
        self.mcp = mcp
        self.budget = budget
        self.clock = clock
        self.on_message = on_message or (lambda m: None)

    def run(self, incident: IncidentRef) -> AgentResult:
        started = self.clock()
        executor = ToolExecutor(self.mcp, incident)
        tools = openai_tools()
        messages: list[dict[str, Any]] = []
        self._append(messages, {"role": "system", "content": SYSTEM_PROMPT})
        self._append(messages, {
            "role": "user",
            "content": f"Incident {incident.incident_id} triggered on service {incident.service_id}. "
            "Investigate and recommend the next step.",
        })

        # Prefetch goes into the transcript as if the model had asked, so every model sees one format.
        # Sequential here; in production these run concurrently.
        prefetch = [
            {"id": f"prefetch_{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(prefetch_calls(incident))
        ]
        self._append(messages, {"role": "assistant", "content": None, "tool_calls": prefetch})
        signatures: set[tuple[str, str]] = set()
        for call in prefetch:
            self._run_call(call, executor, messages, signatures)

        steps, stalled, assessment = 0, 0, None
        stop_reason = "step_budget"
        while steps < self.budget.max_steps:
            if self.clock() - started > self.budget.max_wall_clock_s:
                stop_reason = "deadline"
                break
            steps += 1
            message = self._complete(messages, tools)
            self._append(messages, message)
            calls = message.get("tool_calls") or []
            if not calls:
                self._append(messages, {"role": "user", "content": "Finish by calling submit_assessment."})
                continue

            new_info = False
            for call in calls:
                if len(executor.log) >= self.budget.max_tool_calls:
                    # Every tool_call_id must get a reply or the next request is invalid.
                    self._reply(messages, call, {"error": "tool_budget_exhausted"})
                    continue
                new_info |= self._run_call(call, executor, messages, signatures)
                if call["function"]["name"] == "submit_assessment" and executor.log[-1].status == "ok":
                    assessment = executor.log[-1].arguments

            if assessment is not None:
                stop_reason = "assessment_submitted"
                break
            stalled = 0 if new_info else stalled + 1
            if stalled >= self.budget.max_stalled_steps:
                stop_reason = "stalled"
                break
            if len(executor.log) >= self.budget.max_tool_calls:
                stop_reason = "tool_budget"
                break

        if assessment is None:
            assessment = self._force_assessment(stop_reason, messages, tools, executor)

        return AgentResult(
            incident_id=incident.incident_id,
            stop_reason=stop_reason,
            steps=steps,
            assessment=assessment,
            proposed_actions=executor.proposed_actions,
            tool_log=executor.log,
            messages=messages,
        )

    # -------------------------------------------------------------------------------------------

    def _force_assessment(self, reason, messages, tools, executor) -> dict[str, Any]:
        """Degrade, don't die: the responder always gets the best partial answer we have."""
        self._append(messages, {
            "role": "user",
            "content": f"Stop investigating ({reason}). Call submit_assessment now with what you have "
            "and list everything you could not check.",
        })
        message = self._complete(messages, tools, tool_choice=FORCE_ASSESSMENT)
        self._append(messages, message)
        for call in message.get("tool_calls") or []:
            if call["function"]["name"] != "submit_assessment":
                self._reply(messages, call, {"error": "only submit_assessment is allowed now"})
                continue
            self._run_call(call, executor, messages, set())
            if executor.log[-1].status == "ok":
                return executor.log[-1].arguments
        return {
            "summary": f"The agent stopped ({reason}) without a usable assessment. See the tool log.",
            "hypotheses": [],
            "recommended_next_step": "Investigate manually; the evidence gathered so far is in the transcript.",
            "confidence": "low",
            "open_questions": [],
        }

    def _complete(self, messages, tools, tool_choice: Any = "auto") -> dict[str, Any]:
        response = self.llm.complete(messages, tools, tool_choice=tool_choice)
        raw = response["choices"][0]["message"]
        message: dict[str, Any] = {"role": "assistant", "content": raw.get("content")}
        if raw.get("tool_calls"):  # OpenAI rejects an empty tool_calls list on replay
            message["tool_calls"] = [
                {"id": c["id"], "type": "function", "function": {"name": c["function"]["name"], "arguments": c["function"]["arguments"]}}
                for c in raw["tool_calls"]
            ]
        return message

    def _run_call(self, call, executor: ToolExecutor, messages, signatures: set[tuple[str, str]]) -> bool:
        """Execute one call and append its tool message. Returns True if it was not a repeat."""
        name, raw_args = call["function"]["name"], call["function"]["arguments"]
        try:
            signature = (name, json.dumps(json.loads(raw_args), sort_keys=True))
        except json.JSONDecodeError:
            signature = (name, raw_args)
        is_new = signature not in signatures
        signatures.add(signature)
        self._reply(messages, call, executor.execute(name, raw_args))
        return is_new

    def _reply(self, messages, call, result: dict[str, Any]) -> None:
        self._append(messages, {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)})

    def _append(self, messages, message: dict[str, Any]) -> None:
        messages.append(message)
        self.on_message(message)
