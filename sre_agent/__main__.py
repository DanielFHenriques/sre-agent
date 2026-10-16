"""Run one mocked incident end to end and print what the responder would see.

    py -m sre_agent --scenario bad_deploy
    py -m sre_agent --scenario dependency_outage --fail datadog_search_logs
    py -m sre_agent --llm openai          # real model, mocked tools (needs `openai` + OPENAI_API_KEY)
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from sre_agent.agent import SREAgent
from sre_agent.llm import MockReasoningLLM, OpenAIChatClient
from sre_agent.mocks.fixtures import SCENARIOS, SERVICE, TENANT
from sre_agent.mocks.mcp import MockMCPClient
from sre_agent.models import IncidentRef


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
        print(f"\n  [controller] {m['content']}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="sre_agent")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="bad_deploy")
    parser.add_argument("--fail", action="append", default=[], help="tool name to make time out (repeatable)")
    parser.add_argument("--llm", choices=["mock", "openai"], default="mock")
    parser.add_argument("--json", action="store_true", help="dump the full OpenAI-format transcript")
    args = parser.parse_args()

    llm = OpenAIChatClient() if args.llm == "openai" else MockReasoningLLM()
    agent = SREAgent(llm, MockMCPClient(args.scenario, set(args.fail)), on_message=print_message)
    print(f"Incident PD-1001 on {SERVICE} (tenant {TENANT}), scenario={args.scenario}")
    result = agent.run(IncidentRef("PD-1001", TENANT, SERVICE))

    a = result.assessment or {}
    print("\n" + "=" * 78)
    print(f"ASSESSMENT  confidence={a.get('confidence')}  stop={result.stop_reason}  model_steps={result.steps}")
    print(f"  {a.get('summary')}")
    impact = a.get("impact") or {}
    if impact:
        print(f"  IMPACT  suggested {impact['suggested_severity']} (Incident Commander to confirm): {impact['scope']}")
    for h in a.get("hypotheses", []):
        print(f"  - [{h['confidence']}] {h['cause']}")
        for e in h["evidence_for"]:
            print(f"      + {e}")
        for e in h["evidence_against"]:
            print(f"      - {e}")
    for q in a.get("open_questions", []):
        print(f"  ? {q}")

    print("\nAWAITING YOUR APPROVAL (nothing has been executed):")
    for i, p in enumerate(result.proposed_actions, 1):
        params = ", ".join(f"{k}={v}" for k, v in p.params.items())
        print(f"  [{i}] {p.action}({params})\n      why:    {p.rationale}")
        print(f"      risk:   [{p.risk['level']}] {p.risk['description']}")
        print(f"      expect: {p.expected_effect}")
        if p.how_to_verify:
            v = p.how_to_verify
            print(f"      verify: {v['metric']} {v['comparison']} {v['threshold']} within {v['within_minutes']} min")
    if not result.proposed_actions:
        print("  (none)")

    print("\nTOOL CALL LOG:")
    for r in result.tool_log:
        print(f"  {r.status:<16} {r.tool:<36} {r.latency_ms:>7.2f} ms  {r.output_chars:>5} chars"
              + (f"  {r.error}" if r.error else ""))

    if args.json:
        print(json.dumps(result.messages, indent=2))


if __name__ == "__main__":
    main()
