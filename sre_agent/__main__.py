"""Run one mocked incident end to end and print what the responder would see.

    py -m sre_agent --scenario bad_deploy
    py -m sre_agent --scenario dependency_outage --fail logs_search
    py -m sre_agent --scenario bad_deploy --approve 1                 # approve proposal 1, then verify it
    py -m sre_agent --scenario dependency_outage --approve 1,2        # approve two proposals at once
    py -m sre_agent --scenario deploy_red_herring --approve 1 --approve 1
        # cycle 1: approve the rollback; it fails verification, so the agent re-investigates;
        # cycle 2: approve the first new pending proposal
    py -m sre_agent --scenario deploy_red_herring --state-dir .state --crash-after-steps 2
    py -m sre_agent --scenario deploy_red_herring --state-dir .state --recover --approve 1 --handover-to bob
        # the process "dies" mid-investigation; the second run picks up from the checkpoint
    py -m sre_agent --scenario deploy_red_herring --memory-dir .memory --approve 1 --approve 1
    py -m sre_agent --scenario deploy_red_herring --memory-dir .memory --incident-id PD-1002
        # second time, memory points it straight at saturation: scale_out in the first cycle
    py -m sre_agent --llm openai          # real model, mocked tools (needs `openai` + OPENAI_API_KEY)
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from sre_agent.agent import SREAgent
from sre_agent.llm import MockReasoningLLM, OpenAIChatClient
from sre_agent.mocks.actions import MockActionRunner
from sre_agent.mocks.fixtures import SCENARIOS, SERVICE, TENANT
from sre_agent.mocks.mcp import MockMCPClient
from sre_agent.models import AgentResult, IncidentRef, ProposedAction, TimelineEntry, Verification
from sre_agent.remediation import ApprovalPolicy, NotAuthorized, Remediator
from sre_agent.memory import FileMemoryStore
from sre_agent.state import FileStateStore
from sre_agent.timeline import handover_brief, utc_now
from sre_agent.coverage import summary as coverage_summary
from sre_agent.triage import summary as triage_summary


def _short(text: str, n: int = 110) -> str:
    return text if len(text) <= n else text[: n - 3] + "..."


def print_message(m: dict[str, Any]) -> None:
    if m["role"] == "assistant":
        if m.get("content"):
            print(f"\n  agent> {m['content']}")
        for c in m.get("tool_calls", []):
            if c["function"]["name"] != "submit_assessment":
                print(f"    -> {c['function']['name']}({_short(c['function']['arguments'], 90)})")
    elif m["role"] == "tool":
        print(f"       <- {_short(m['content'])}")
    elif m["role"] == "user" and "Incident" not in m["content"]:
        print(f"\n  [controller] {_short(m['content'], 200)}")


class CrashAfter:
    """Wraps a model client and kills the process after N calls, to demo checkpoint recovery."""

    def __init__(self, llm, calls: int):
        self.llm, self.remaining = llm, calls

    def complete(self, *args, **kwargs):
        if self.remaining == 0:
            raise SystemExit("\n*** simulated crash: agent process died mid-investigation. "
                             "Run the same command with --recover instead of --crash-after-steps. ***")
        self.remaining -= 1
        return self.llm.complete(*args, **kwargs)


def pending(result: AgentResult) -> list[ProposedAction]:
    return [p for p in result.proposed_actions if p.status == "pending_approval"]


def print_result(result: AgentResult) -> None:
    a = result.assessment or {}
    print("\n" + "=" * 78)
    print(f"ASSESSMENT (cycle {result.cycle})  confidence={a.get('confidence')}  stop={result.stop_reason}  "
          f"model_steps={result.steps}")
    t = result.triage
    if t:
        print(f"  TRIAGE  {triage_summary(t)}")
        for s in t["signals"]:
            print(f"          - {s}")
        print(f"          {t['recommendation']}")
        if t.get("coverage"):
            print(f"  COVERAGE {coverage_summary(t['coverage'])}")
            for limit in t["coverage"]["limits"]:
                print(f"          - {limit}")
    print(f"  {a.get('summary')}")
    impact = a.get("impact") or {}
    if impact:
        print(f"  IMPACT  {impact['scope']}")
    for h in a.get("hypotheses", []):
        print(f"  - [{h['confidence']}] {h['cause']}")
        for e in h["evidence_for"]:
            print(f"      + {e}")
        for e in h["evidence_against"]:
            print(f"      - {e}")
    for q in a.get("open_questions", []):
        print(f"  ? {q}")

    print("\nAWAITING YOUR APPROVAL (nothing has been executed):")
    for i, p in enumerate(pending(result), 1):
        params = ", ".join(f"{k}={v}" for k, v in p.params.items())
        print(f"  [{i}] {p.action}({params})" + ("   << FAST-TRACK: one-click approval >>" if p.fast_track else ""))
        if p.fast_track:
            print(f"      fast-track because: {p.fast_track_reason}")
        print(f"      why:    {p.rationale}")
        print(f"      risk:   [{p.risk['level']}] {p.risk['description']}")
        print(f"      expect: {p.expected_effect}")
        if p.how_to_verify:
            v = p.how_to_verify
            print(f"      verify: {v['metric']} {v['comparison']} {v['threshold']} within {v['within_minutes']} min")
    if not pending(result):
        print("  (none)")


def print_verification(v: Verification) -> None:
    for r in v.readings:
        print(f"  check {r['attempt']}: " + (f"error {r['error']}" if "error" in r
              else f"{r['value']} {'PASS' if r['passed'] else 'not yet'}"))
    print(f"  VERDICT: {v.verdict.upper()}. {v.detail}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="sre_agent")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="bad_deploy")
    parser.add_argument("--fail", action="append", default=[], help="tool name to make time out (repeatable)")
    parser.add_argument("--llm", choices=["mock", "openai"], default="mock")
    parser.add_argument(
        "--approve", action="append", default=[],
        help="comma-separated pending proposals to approve, e.g. 1 or 1,2. Repeat the flag for the next "
        "cycle: used only if a fix fails verification and the agent re-investigates.",
    )
    parser.add_argument("--approver", default="alice",
                        help="who approves (mock roster: alice = Incident Commander; bob, carol = responders)")
    parser.add_argument("--state-dir", help="persist incident state here (checkpointed every step)")
    parser.add_argument("--crash-after-steps", type=int,
                        help="simulate the agent process dying after N model calls (needs --state-dir)")
    parser.add_argument("--recover", action="store_true",
                        help="load the incident from --state-dir and continue where it stopped")
    parser.add_argument("--memory-dir", help="cross-incident memory: recalled during investigation, written when a fix verifies")
    parser.add_argument("--incident-id", default="PD-1001", help="use a new id to replay a scenario as a new incident")
    parser.add_argument("--handover-to", help="hand the incident over to this person and print the brief")
    parser.add_argument("--ask", action="append", default=[],
                        help="ask the agent a question about the incident (repeatable); goes through guardrails")
    parser.add_argument("--asked-by", default="engineer@acme.example")
    parser.add_argument("--json", action="store_true", help="dump the full OpenAI-format transcript")
    args = parser.parse_args()
    if (args.crash_after_steps or args.recover) and not args.state_dir:
        parser.error("--crash-after-steps and --recover need --state-dir")

    llm = OpenAIChatClient() if args.llm == "openai" else MockReasoningLLM()
    if args.crash_after_steps:
        llm = CrashAfter(llm, args.crash_after_steps)
    mcp = MockMCPClient(args.scenario, set(args.fail))
    incident = IncidentRef(args.incident_id, TENANT, SERVICE)
    store = FileStateStore(args.state_dir) if args.state_dir else None
    memory = FileMemoryStore(args.memory_dir) if args.memory_dir else None
    agent = SREAgent(llm, mcp, on_message=print_message, store=store, memory=memory)
    # Simulated clock: verification polls every 60 s of "incident time" without actually waiting.
    remediator = Remediator(MockActionRunner(mcp), mcp, incident, sleep=lambda s: None)

    print(f"Incident {incident.incident_id} on {SERVICE} (tenant {TENANT}), scenario={args.scenario}")
    saved = store.load(incident) if store else None
    if args.recover:
        if saved is None:
            parser.error(f"no saved state for {incident.incident_id} in {args.state_dir}")
        if saved.stop_reason == "in_progress":
            print(f"RECOVERING from checkpoint v{saved.version}: cycle {saved.cycle}, {saved.steps} model steps done, "
                  f"{len(saved.messages)} messages")
            result = agent.recover(incident, saved)
        else:
            print(f"LOADED saved state v{saved.version} (investigation finished: {saved.stop_reason})")
            result = saved
    else:
        if saved is not None:
            parser.error(f"{incident.incident_id} already has saved state in {args.state_dir}; use --recover")
        result = agent.run(incident)
    print_result(result)
    remediator.policy = ApprovalPolicy(result)

    for picks in args.approve:
        chosen = [pending(result)[int(n) - 1] for n in picks.split(",")]
        failed = None
        for proposal in chosen:
            print(f"\nAPPROVAL of {proposal.action} by {args.approver}")
            try:
                v = remediator.approve(proposal, args.approver, transcript=result.messages, timeline=result.timeline)
            except NotAuthorized as e:
                print(f"  REFUSED, nothing executed: {e}")
                if store:
                    store.save(incident, result)
                continue
            print("  authorized: executed, then verified")
            print_verification(v)
            if v.verdict == "did_not_work" and failed is None:
                failed = v
        if store:
            store.save(incident, result)
        if failed is None:
            break
        print(f"\nRE-INVESTIGATING: {failed.action} did not work.")
        result = agent.resume(incident, result, failed)
        print_result(result)
        remediator.policy = ApprovalPolicy(result)

    if memory and any(p.verification_verdict == "worked" for p in result.proposed_actions):
        record = agent.remember(incident, result)
        print(f"\nREMEMBERED {record.incident_id}: worked {[w['detail'] for w in record.worked]}, "
              f"did not work {[f['detail'] for f in record.failed]}, root cause: {record.root_cause}")

    if args.handover_to:
        result.timeline.append(TimelineEntry(
            utc_now(), "handover", f"Handover from {args.approver} to {args.handover_to}.", args.approver
        ))
        if store:
            store.save(incident, result)
        print("\n" + "=" * 78)
        print(handover_brief(result, args.approver, args.handover_to))
    else:
        print("\nTIMELINE:")
        for t in result.timeline:
            print(f"  {t.at}  [{t.actor}] {t.text}")

    for question in args.ask:
        answer = agent.ask(incident, result, question, args.asked_by)
        tag = "ANSWER" if answer.answered else f"NOT ANSWERED ({answer.category})"
        print(f"\nQ ({args.asked_by}): {question}\n{tag}: {answer.text}")

    print("\nTOOL CALL LOG (all cycles):")
    for r in result.tool_log:
        print(f"  {r.status:<16} {r.tool:<36} {r.latency_ms:>7.2f} ms  {r.output_chars:>5} chars"
              + (f"  {r.error}" if r.error else ""))

    if args.json:
        print(json.dumps(result.messages, indent=2))


if __name__ == "__main__":
    main()
