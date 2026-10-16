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
| `sre_agent/mocks/` | Two incidents that page identically but need opposite fixes |

## Status

Work in progress. Still to write up: assumptions, what is left out, and where this breaks first
(state persistence across restarts, memory across incidents, the region-outage burst).
