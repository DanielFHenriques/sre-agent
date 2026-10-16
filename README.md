# SRE investigator agent (sketch)

A single-agent sketch of an incident investigator: an incident comes in, the agent gathers context
through tool calls, reasons, and proposes a next step that a human must approve. Everything
external is mocked. Tools and messages use the OpenAI Chat Completions function-calling format.

Design notes and the brainstorm behind it: [docs/DESIGN.md](docs/DESIGN.md).

## Run

Python 3.11+, standard library only.

```bash
py -m sre_agent --scenario bad_deploy
py -m sre_agent --scenario dependency_outage
py -m sre_agent --scenario bad_deploy --fail datadog_search_logs   # partial failure
py -m sre_agent --scenario bad_deploy --approve 1                   # approve, execute (mocked), verify
py -m sre_agent --scenario deploy_red_herring --approve 1 --approve 1
    # the rollback fails verification, the agent re-investigates, then approve its new proposal
py -m sre_agent --scenario deploy_red_herring --state-dir .state --crash-after-steps 2
py -m sre_agent --scenario deploy_red_herring --state-dir .state --recover --approve 1 --approve 1 --handover-to bob
    # the process dies mid-investigation; the second run continues from the checkpoint, then hands over
py -m unittest discover -s tests -t .
```

`--llm openai` swaps the mock for a real model (needs `pip install openai` and `OPENAI_API_KEY`;
model from `OPENAI_MODEL`). Tools stay mocked.

## Layout

| Path | What |
|---|---|
| `sre_agent/tools/specs.py` | OpenAI strict function schemas for every tool |
| `sre_agent/tools/registry.py` | Tool -> side effect (read / approval_required / terminal) -> MCP server |
| `sre_agent/tools/executor.py` | Policy gate: validation, tenant injection, timeouts, proposals, truncation, call log |
| `sre_agent/agent.py` | Core loop: prefetch, model <-> tools, budgets, stall detection, forced final answer |
| `sre_agent/llm.py` | `MockReasoningLLM` (deterministic) and `OpenAIChatClient` |
| `sre_agent/state.py` | Persisted incident state: tenant-scoped keys, atomic writes, version check against two writers |
| `sre_agent/timeline.py` | Scribe-style timeline written by code from events, and the handover brief built from it |
| `sre_agent/remediation.py` | Human approval, execution and metric verification; unreachable from the model |
| `sre_agent/mocks/` | Three incidents that page identically but need different fixes (one where the obvious fix is wrong), plus what each action changes |

## Status

Work in progress. Still to write up: assumptions, what is left out, and where this breaks first
(memory across incidents, the region-outage burst).
