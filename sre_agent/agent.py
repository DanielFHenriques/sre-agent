"""The core loop: prefetch, then model <-> tools until an assessment, a budget, or a stall.

The transcript is kept in OpenAI Chat Completions format. Together with proposals, the call log,
the timeline and the cycle number it is the whole state of an investigation (`AgentResult`).
With a store attached, that state is checkpointed after every model response and every round of
tool results, so a process that dies mid-investigation can be picked up with `recover`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from sre_agent.llm import LLMClient
from sre_agent.guardrails import ANSWER_BLOCKED, REPLIES, filter_answer, screen
from sre_agent.models import AgentResult, Answer, IncidentRef, SideEffect, TimelineEntry, Verification
from sre_agent.prompts import QA_PROMPT, SYSTEM_PROMPT
from sre_agent.memory import FileMemoryStore, MemoryRecord, distill
from sre_agent.state import FileStateStore
from sre_agent.timeline import Now, investigation_entries, proposal_entry, utc_now
from sre_agent.triage import raise_only, summary as triage_summary, triage
from sre_agent.tools.executor import MCPClient, ToolExecutor
from sre_agent.tools.registry import REGISTRY, openai_tools

FORCE_ASSESSMENT = {"type": "function", "function": {"name": "submit_assessment"}}


@dataclass(frozen=True)
class Budget:
    max_steps: int = 6  # model calls per investigation, excluding the forced final one
    max_tool_calls: int = 20
    max_wall_clock_s: float = 120.0  # the responder should see something within ~2 min
    max_stalled_steps: int = 2  # consecutive steps that only repeat earlier calls
    max_cycles: int = 3  # investigations per incident: the first plus re-investigations after failed fixes


def prefetch_identity(incident: IncidentRef) -> list[tuple[str, dict[str, Any]]]:
    """Wave 1: what the incident is, what the service is, and which sources it has."""
    svc = incident.service_id
    return [
        ("pagerduty_get_incident", {"incident_id": incident.incident_id}),
        ("pagerduty_get_service_context", {"service_id": svc}),
        ("pagerduty_list_service_integrations", {"service_id": svc}),
    ]


def prefetch_signals(
    incident: IncidentRef, integrations: dict[str, Any], details: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    """Wave 2: what triage needs plus what every investigation needs, only from sources the service has.

    If the integrations lookup failed, try everything: for reads, failing open costs a few errors.
    """
    svc = incident.service_id
    provides = {p for i in integrations.get("integrations", []) for p in i.get("provides", [])}
    has = (lambda capability: capability in provides) if "integrations" in integrations else (lambda _: True)
    calls: list[tuple[str, dict[str, Any]]] = []
    if has("deploys"):
        calls.append(("changes_list", {"service_id": svc, "lookback_minutes": 180}))
    if has("monitors"):
        calls.append(("alerts_list", {"service_id": svc}))
    if has("metrics"):
        calls += [
            ("metrics_query", {"service_id": svc, "metric": "business_kpi", "lookback_minutes": 30}),
            ("metrics_query", {"service_id": svc, "metric": "error_rate", "lookback_minutes": 30}),
        ]
    if has("support_cases") and "onset_estimate" in details:
        calls.append(("support_cases_search", {"service_id": svc, "since": details["onset_estimate"]}))
    return calls


class SREAgent:
    def __init__(
        self,
        llm: LLMClient,
        mcp: MCPClient,
        budget: Budget = Budget(),
        clock: Callable[[], float] = time.monotonic,
        on_message: Callable[[dict[str, Any]], None] | None = None,
        store: FileStateStore | None = None,
        memory: FileMemoryStore | None = None,
        now: Now = utc_now,
    ):
        self.llm = llm
        self.mcp = mcp
        self.budget = budget
        self.clock = clock
        self.on_message = on_message or (lambda m: None)
        self.store = store
        self.memory = memory
        self.now = now

    def run(self, incident: IncidentRef) -> AgentResult:
        started = self.clock()
        state = AgentResult(incident.incident_id, stop_reason="in_progress", steps=0, assessment=None)
        executor = self._executor(incident, state)
        state.timeline.append(TimelineEntry(
            self.now(), "triggered",
            f"Incident {incident.incident_id} triggered on {incident.service_id}. Agent started investigating.", "agent",
        ))
        self._append(state.messages, {"role": "system", "content": SYSTEM_PROMPT})
        self._append(state.messages, {
            "role": "user",
            "content": f"Incident {incident.incident_id} triggered on service {incident.service_id}. "
            "Investigate and recommend the next step.",
        })

        # Prefetch, no model: two waves, because which sources to read depends on the service's integrations.
        signatures: set[tuple[str, str]] = set()
        seen = self._prefetch(prefetch_identity(incident), "prefetch_a", executor, state, signatures)
        seen |= self._prefetch(
            prefetch_signals(
                incident, seen.get("pagerduty_list_service_integrations", {}), seen.get("pagerduty_get_incident", {})
            ),
            "prefetch_b", executor, state, signatures,
        )

        # Triage, no model: severity and priority from data, before anyone reasons about causes.
        state.triage = executor.triage = triage(
            seen.get("pagerduty_get_incident", {}), seen.get("business_kpi"), seen.get("support_cases_search")
        )
        state.timeline.append(TimelineEntry(
            self.now(), "triage", f"Triage: {triage_summary(state.triage)} {' '.join(state.triage['signals'])}", "agent",
        ))
        self._append(state.messages, {
            "role": "user",
            "content": f"[triage] {triage_summary(state.triage)} Signals: {' '.join(state.triage['signals'])} "
            "This severity was set from data. If your investigation shows the impact is worse, say so in "
            "impact.suggested_severity; it can be raised, never lowered.",
        })
        self._checkpoint(incident, state)

        return self._investigate(incident, state, executor, signatures, started, previous_assessment=None)

    def _prefetch(self, calls, prefix, executor, state, signatures) -> dict[str, Any]:
        """Run calls without the model; record them in the transcript as if the model had asked, so every
        model sees one format. Returns results by tool name (metrics by metric name).
        Sequential here; in production these run concurrently."""
        if not calls:
            return {}
        tool_calls = [
            {"id": f"{prefix}_{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(calls)
        ]
        self._append(state.messages, {"role": "assistant", "content": None, "tool_calls": tool_calls})
        results: dict[str, Any] = {}
        for call, (name, args) in zip(tool_calls, calls):
            self._run_call(call, executor, state.messages, signatures)
            results[args.get("metric", name)] = json.loads(state.messages[-1]["content"])
        self._checkpoint(executor.incident, state)
        return results

    def resume(self, incident: IncidentRef, previous: AgentResult, verification: Verification) -> AgentResult:
        """Re-investigate after an approved action did not work, on the same transcript.

        Investigation is read-only, so it starts without asking. Anything it proposes still needs
        approval. After `max_cycles` investigations the agent stops proposing and hands back to humans,
        so it cannot loop on remediation in the middle of an outage.
        """
        if verification.verdict != "did_not_work":
            raise ValueError(f"resume is for failed fixes, got verdict '{verification.verdict}'")
        started = self.clock()
        state = AgentResult(
            incident.incident_id,
            stop_reason="in_progress",
            steps=0,
            assessment=None,
            proposed_actions=previous.proposed_actions,
            tool_log=previous.tool_log,
            messages=list(previous.messages),
            cycle=previous.cycle + 1,
            timeline=list(previous.timeline),
            triage=previous.triage,
            version=previous.version,
        )
        executor = self._executor(incident, state)

        if state.cycle > self.budget.max_cycles:
            state.cycle = previous.cycle
            state.stop_reason = "cycle_limit"
            state.assessment = _escalation(previous, verification)
            state.timeline.append(TimelineEntry(
                self.now(), "escalated",
                f"{verification.action} did not work and the cycle limit is reached. Agent stopped proposing "
                "fixes; handed back to humans.", "agent",
            ))
            self._checkpoint(incident, state)
            return state

        state.timeline.append(TimelineEntry(
            self.now(), "reinvestigating",
            f"{verification.action} did not work ({verification.detail}) Re-investigating (cycle {state.cycle}).", "agent",
        ))
        self._append(state.messages, {
            "role": "user",
            "content": f"[re-investigate] The approved {verification.action} did not work: {verification.detail} "
            "Your leading hypothesis is probably wrong. Fetch fresh data (earlier results are stale now), "
            "look wider than last time (traffic, saturation, dependencies), and do not propose the same "
            "action again.",
        })
        self._checkpoint(incident, state)
        # Fresh signatures: re-reading a metric after an action is new information, not a stall.
        return self._investigate(incident, state, executor, set(), started, previous_assessment=previous.assessment)

    def recover(self, incident: IncidentRef, state: AgentResult) -> AgentResult:
        """Continue an investigation whose process died, from its last checkpoint.

        The checkpoint may end with an assistant message whose tool calls were never answered (the
        process died while running them). Those are run now: reads are idempotent, proposals are
        de-duplicated by the executor, so re-running is safe. Without this the transcript would be
        invalid for the next model call.
        """
        if state.stop_reason != "in_progress":
            raise ValueError(f"nothing to recover: investigation already ended ({state.stop_reason})")
        started = self.clock()
        executor = self._executor(incident, state)
        state.timeline.append(TimelineEntry(
            self.now(), "restarted",
            f"Agent process restarted. Continuing cycle {state.cycle} from the checkpoint after step {state.steps}.",
            "agent",
        ))

        assessment = self._answer_dangling_calls(state, executor)
        self._checkpoint(incident, state)
        if assessment is not None:
            return self._finish(incident, state, executor, assessment, "assessment_submitted", None)
        return self._investigate(incident, state, executor, set(), started, previous_assessment=None)

    def ask(self, incident: IncidentRef, state: AgentResult, question: str, asked_by: str) -> Answer:
        """Answer an engineer's question about this incident, behind three guardrail layers.

        1. `screen`: deterministic checks before any model call.
        2. Structure: a fresh thread per question (never the investigation transcript), the incident as
           data, read-only tools offered *and* enforced by the executor. Nothing here can propose,
           approve or execute an action, or change what the investigation sees.
        3. `filter_answer`: the answer is checked before it is returned.
        Every question is recorded on the incident with its verdict.
        """
        verdict = screen(question, state)
        record = {"at": self.now(), "asked_by": asked_by, "question": question[:500],
                  "category": verdict.category, "matched": verdict.matched}
        state.questions.append(record)
        if not verdict.allowed:
            self._checkpoint(incident, state)
            return Answer(verdict.reply or "", verdict.category, False)

        qa_prompt = QA_PROMPT.format(incident_id=incident.incident_id)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": qa_prompt},
            {"role": "user", "content": (
                f"<incident_briefing>\n{json.dumps(briefing(state, incident), indent=1)}\n</incident_briefing>\n"
                f"<question from=\"{asked_by}\">\n{question}\n</question>"
            )},
        ]
        read_only = {name for name, t in REGISTRY.items() if t.side_effect is SideEffect.READ}
        executor = ToolExecutor(self.mcp, incident, transcript=state.messages, allowed_tools=read_only,
                                local_servers=self._local_servers(), now=self.now)
        tools = [t for t in self._tools(executor) if t["function"]["name"] in read_only]
        text = None
        for _ in range(3):  # a question gets a few read-only lookups, not an investigation
            message = self._complete(messages, tools)
            messages.append(message)
            if not message.get("tool_calls"):
                text = (message.get("content") or "").strip()
                break
            for call in message["tool_calls"]:
                result = executor.execute(call["function"]["name"], call["function"]["arguments"])
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)})
        if not text:
            text = f"I couldn't answer that within my limits. The current assessment of {incident.incident_id} is in the brief."

        if text == "OUT_OF_SCOPE":
            record["category"] = "off_topic_model"
            answer = Answer(REPLIES["off_topic"].format(incident=incident.incident_id), "off_topic_model", False)
        else:
            ok, reason = filter_answer(text, [SYSTEM_PROMPT, qa_prompt])
            if ok:
                answer = Answer(text, "ok", True)
            else:
                record["category"], record["matched"] = "output_blocked", reason
                answer = Answer(ANSWER_BLOCKED.format(incident=incident.incident_id), "output_blocked", False)
        self._checkpoint(incident, state)
        return answer

    def remember(self, incident: IncidentRef, state: AgentResult) -> MemoryRecord | None:
        """Distill a finished incident into memory. Deterministic: verified outcomes, not model prose."""
        if self.memory is None:
            return None
        record = distill(state, incident, self.now())
        self.memory.remember(incident.tenant_id, record)
        worked = ", ".join(w["detail"] for w in record.worked) or "nothing verified"
        failed = ", ".join(f["detail"] for f in record.failed) or "none"
        state.timeline.append(TimelineEntry(
            self.now(), "remembered",
            f"Remembered for next time: worked: {worked}; did not work: {failed}; root cause: "
            f"{record.root_cause or 'unconfirmed'}.", "agent",
        ))
        self._checkpoint(incident, state)
        return record

    # -------------------------------------------------------------------------------------------

    def _local_servers(self) -> dict[str, Any]:
        return {"agent_memory": self.memory} if self.memory is not None else {}

    def _tools(self, executor: ToolExecutor) -> list[dict[str, Any]]:
        """Tools offered to the model: only capabilities this service has an integration for (none if its
        integrations couldn't be read), and memory recall only when there is a memory to recall from."""
        covered = executor.router.covered() if executor.router else set()
        return [t for t in openai_tools(covered) if self.memory is not None or t["function"]["name"] != "memory_recall"]

    def _executor(self, incident: IncidentRef, state: AgentResult) -> ToolExecutor:
        """An executor that knows what this incident has already seen and tried, sharing state's lists."""
        executor = ToolExecutor(
            self.mcp,
            incident,
            transcript=state.messages,
            prior_proposals=state.proposed_actions,
            prior_log=state.tool_log,
            local_servers=self._local_servers(),
            now=self.now,
        )
        state.proposed_actions = executor.proposed_actions
        state.tool_log = executor.log
        executor.triage = state.triage
        return executor

    def _investigate(self, incident, state: AgentResult, executor: ToolExecutor, signatures, started,
                     previous_assessment) -> AgentResult:
        tools = self._tools(executor)
        messages = state.messages
        calls_before = len(executor.log)
        stalled, assessment = 0, None
        stop_reason = "step_budget"
        while state.steps < self.budget.max_steps:
            if self.clock() - started > self.budget.max_wall_clock_s:
                stop_reason = "deadline"
                break
            state.steps += 1
            message = self._complete(messages, tools)
            self._append(messages, message)
            self._checkpoint(incident, state)  # a model response costs money and time; don't lose it
            calls = message.get("tool_calls") or []
            if not calls:
                self._append(messages, {"role": "user", "content": "Finish by calling submit_assessment."})
                continue

            new_info = False
            proposals_before = len(executor.proposed_actions)
            for call in calls:
                if len(executor.log) - calls_before >= self.budget.max_tool_calls:
                    # Every tool_call_id must get a reply or the next request is invalid.
                    self._reply(messages, call, {"error": "tool_budget_exhausted"})
                    continue
                new_info |= self._run_call(call, executor, messages, signatures)
                if call["function"]["name"] == "submit_assessment" and executor.log[-1].status == "ok":
                    assessment = executor.log[-1].arguments
            for p in executor.proposed_actions[proposals_before:]:
                state.timeline.append(proposal_entry(self.now(), p))
            self._checkpoint(incident, state)

            if assessment is not None:
                stop_reason = "assessment_submitted"
                break
            stalled = 0 if new_info else stalled + 1
            if stalled >= self.budget.max_stalled_steps:
                stop_reason = "stalled"
                break
            if len(executor.log) - calls_before >= self.budget.max_tool_calls:
                stop_reason = "tool_budget"
                break

        return self._finish(incident, state, executor, assessment, stop_reason, previous_assessment)

    def _finish(self, incident, state: AgentResult, executor, assessment, stop_reason, previous_assessment) -> AgentResult:
        if assessment is None:
            assessment = self._force_assessment(stop_reason, state.messages, self._tools(executor), executor)
        state.assessment = assessment
        state.stop_reason = stop_reason
        state.timeline.extend(investigation_entries(self.now(), previous_assessment, assessment, stop_reason, state.cycle))
        if state.triage is not None:
            impact = assessment.get("impact") or {}
            note = raise_only(state.triage, impact.get("suggested_severity"), impact.get("scope", "investigation findings"))
            if note:
                state.timeline.append(TimelineEntry(self.now(), "triage", note, "agent"))
                if state.triage["response_mode"] != "low":
                    for p in state.proposed_actions:
                        if p.fast_track and p.status == "pending_approval":
                            p.fast_track, p.fast_track_reason = False, None
                            state.timeline.append(TimelineEntry(
                                self.now(), "triage",
                                f"Fast-track withdrawn from {p.action}: severity is now {state.triage['severity']}.",
                                "agent",
                            ))
        self._checkpoint(incident, state)
        return state

    def _answer_dangling_calls(self, state: AgentResult, executor: ToolExecutor) -> dict[str, Any] | None:
        messages = state.messages
        last = max((i for i, m in enumerate(messages) if m["role"] == "assistant"), default=None)
        if last is None or not messages[last].get("tool_calls"):
            return None
        answered = {m["tool_call_id"] for m in messages[last + 1:] if m["role"] == "tool"}
        assessment = None
        proposals_before = len(executor.proposed_actions)
        for call in messages[last]["tool_calls"]:
            if call["id"] in answered:
                continue
            self._run_call(call, executor, messages, set())
            if call["function"]["name"] == "submit_assessment" and executor.log[-1].status == "ok":
                assessment = executor.log[-1].arguments
        for p in executor.proposed_actions[proposals_before:]:
            state.timeline.append(proposal_entry(self.now(), p))
        return assessment

    def _checkpoint(self, incident: IncidentRef, state: AgentResult) -> None:
        if self.store is not None:
            self.store.save(incident, state)

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


def _escalation(previous: AgentResult, verification: Verification) -> dict[str, Any]:
    """Deterministic hand-back to humans once the cycle limit is hit. No model call, no new proposals."""
    tried = [f"{p.action} {p.params}" for p in previous.proposed_actions if p.verification_verdict == "did_not_work"]
    return {
        "summary": f"{len(tried)} approved fixes did not work. The agent has stopped proposing fixes; "
        "this needs a human-led investigation.",
        "impact": (previous.assessment or {}).get("impact") or {
            "business_metric": "unknown", "baseline": None, "current": None, "change_pct": None,
            "scope": "unknown", "suggested_severity": "unknown",
        },
        "hypotheses": [],
        "recommended_next_step": "Escalate to the owning team's senior on-call. Fixes already tried: " + "; ".join(tried),
        "confidence": "low",
        "open_questions": [f"Why did {verification.action} not help? {verification.detail}"],
    }


def briefing(state: AgentResult, incident: IncidentRef) -> dict[str, Any]:
    """What a question is answered from: the current picture, not the raw investigation transcript."""
    changes: list[dict[str, Any]] = []
    for m in state.messages:
        if m["role"] == "tool" and '"changes"' in str(m.get("content")):
            try:
                changes = json.loads(m["content"]).get("changes", changes)
            except json.JSONDecodeError:
                pass
    a = state.assessment or {}
    return {
        "incident_id": incident.incident_id,
        "service_id": incident.service_id,
        "triage": {k: (state.triage or {}).get(k) for k in ("severity", "priority", "response_mode", "customer_facing", "signals")},
        "assessment": {k: a.get(k) for k in ("summary", "impact", "hypotheses", "recommended_next_step", "confidence", "open_questions")},
        "proposals": [
            {"action": p.action, "params": p.params, "status": p.status, "risk": p.risk,
             "verification": p.verification_verdict, "fast_track": p.fast_track}
            for p in state.proposed_actions
        ],
        "recent_changes": changes,
        "timeline": [f"{t.at} [{t.actor}] {t.text}" for t in state.timeline[-15:]],
    }
