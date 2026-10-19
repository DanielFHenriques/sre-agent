# SRE Agent: assumptions and decisions

For the interviewers. This lists the decisions behind the sketch, the assumptions they rest on, and
where the sketch stops on purpose. The full reasoning is in [README.md](../README.md) and
[DESIGN.md](DESIGN.md). This file points to them and adds decisions made in review, such as the one
on historical data.

## Decision: verify fixes against historical normal ranges

Where historical metric data is available, a fix counts as working only when its metric returns to
that metric's own normal range and stays there. The sketch doesn't do this yet. It uses a simpler
stand-in, because the mocks carry a single baseline number per metric.

| | In the sketch today | Preferred, with history |
|---|---|---|
| What "normal" is | One `baseline` number from the metrics vendor | A normal band (p5, p50, p95) for the same hour and weekday over the last 4 weeks, excluding past incident windows |
| Success threshold | Written by the model. Rejected if more than 3x the baseline (`MAX_BASELINE_RATIO = 3.0`, one value for every metric) | Derived by code from the band. The model picks only which metric shows recovery, or the runbook names it |
| When a fix "worked" | The first single reading that passes (`Remediator._verify`) | Several consecutive readings inside the band, for example 3 one-minute readings in a row |
| No history (new service or metric) | Not applicable | Fall back to today's ratio rule or a runbook threshold, and say so in the proposal |

Why we prefer history:

- A single baseline can't tell a noisy metric from a steady one. The same 3x limit is too loose for
  one and too strict for the other.
- A threshold that code derives from data the agent actually saw can't be gamed by the model.
- Needing several readings in a row stops one lucky dip from counting as a successful fix.

What history gets wrong, and how we'd handle it:

- **Past incidents widen the band.** Exclude incident windows. The state store already records them.
- **Seasonality and planned events.** Compare the same hour and weekday, and allow a manual override
  for known peaks.
- **Normal moves after big deploys or lasting traffic growth.** Keep a short-window band next to the
  long one, and flag it when they disagree.
- **Recovery isn't always caused by the fix.** If a traffic spike ends, errors recover whatever was
  done. Also check the metric the action targets, for example pool saturation for `scale_out`.

The principle is the same as everywhere else in the design: code works out what normal is from data,
the model chooses which signal matters, and a human approves. Triage follows the same reasoning. Its
thresholds (KPI drop, support-case bursts) are fixed constants in the sketch. In production they would
be per-tenant settings based on history.

## Other architecture decisions

| Decision | Why | Where |
|---|---|---|
| One language model, with deterministic code around it | Every hand-off between agents loses context and adds latency and cost. We would split only when the tool catalog grows past about 20-30 tools, an investigation no longer fits the context window, or a sub-task needs a different model | `docs/DESIGN.md`, "How many agents?" |
| The model proposes, code decides | Severity, what may be proposed, who may approve, whether a fix worked and what is remembered are all decided in code, so they hold even if the model ignores its prompt | `tools/executor.py`, `triage.py`, `remediation.py` |
| Prefetch without the model, recorded as tool calls | Saves 3-4 model round trips. All evidence has one shape, which the executor, memory and approvals parse. Fetched data stays in `tool` messages, apart from instructions | `agent.py`, `_prefetch` |
| Capability tools, not vendor tools | The model calls `metrics_query`, never "Datadog". One connector per vendor. A new vendor means one class, and nothing else changes | `connectors.py`, `tools/registry.py` |
| Coverage limits what can be proposed | No metrics means no fix proposals, because none could be verified. No change tracking means no rollbacks | `coverage.py` |
| Tenant isolation: the first layer is built in the agent | `tenant_id` comes from the authenticated webhook (`IncidentRef`). It is never a tool parameter, strict schemas reject extra fields, and the executor injects it into every MCP, connector and memory call. State and memory are stored per tenant, with validated ids. The model can't name or switch tenants, even through injected text | `models.py`, `tools/executor.py`, `state.py`, `memory.py` |
| Execution outside the agent | No tool maps to `Remediator`. That's why crash recovery can re-run tool calls without idempotency keys | `remediation.py` |
| The transcript is the state | Checkpointed after every model response. Saves use compare-and-swap on a version number (optimistic concurrency), so two writers conflict instead of overwriting each other | `agent.py`, `state.py` |
| Bounded self-correction | A failed fix triggers re-investigation on the same transcript. The same fix can't be proposed again. After 3 cycles, code hands back to humans | `agent.py`, `resume` |
| A mock model, not a real API | This is an exercise, and running scenarios repeatedly against a paid API costs money. `MockReasoningLLM` is a hard-coded decision table that follows the playbook, so the loop, guardrails and tests run with no API key. A real client (`OpenAIChatClient`, `--llm openai`) is wired in but has never been run through the scenarios | `llm.py`, `__main__.py` |
| Memory written by code | Only verified outcomes, ruled-out causes and normalised error signatures are stored, never model prose. Neither the model nor a user can plant a "fact" | `memory.py`, `distill` |

## Assumptions

From the brief:

- One responder, one surface, one agent.
- Third-party data comes only through MCP tool calls.
- Read-only tools, plus a small catalog of actions that need human approval. No autonomous actions.
- Multi-tenant SaaS.

Ours (each one is a question for the team):

- **The responder is the Incident Commander.** The agent investigates and keeps the timeline. It never decides.
- **Historical metric data is available in production** through the client's metrics vendor. The
  vendors we target (Datadog, New Relic) have baseline and anomaly functions. Without that data we use
  the fallback described above.
- **Onset time is available**, from PagerDuty or the alerting metric. It sets every query window.
- **Each service declares a business KPI** in its metadata, and severity is based on it.
- **Runbooks name their remediations** in a form that maps to the action catalog.
- **Approver identity comes from the surface's SSO login**, checked against the incident's responder
  roster. Never from text typed into the chat.
- **The tenant comes from the authenticated webhook**, and every MCP server enforces it with per-tenant credentials.
- **Thresholds in the mocks are illustrative.** In practice they are per-tenant configuration.

## Known gaps and what we'd do next

Ordered by how much each one matters.

| Gap | Why it matters | Next step |
|---|---|---|
| No evaluation against a real model | The 130 tests cover the deterministic code around a hard-coded mock. They say nothing about how good a real model's investigations are | Run each scenario N times against a real model. Measure correct-action rate, tool calls and tokens. Add more cases where the obvious answer is wrong. This can be tried for free: the `openai` SDK reads `OPENAI_BASE_URL`, so `OpenAIChatClient` can point at any OpenAI-compatible endpoint with no code change. Examples are a local model through Ollama, or the free tiers of Gemini, Groq or GitHub Models. Check that the chosen model supports function calling and strict schemas, and expect weaker results from small local models |
| Verification uses one reading and a fixed 3x ratio | One dip counts as a successful fix. The ratio doesn't fit every metric | The historical-range decision above. Consecutive readings first, because it is cheap |
| The approval roster is a snapshot | This is a gap between the docs and the code. Triage is read live from state, but `ApprovalPolicy.roster()` reads the first `responders` tool result in the transcript. After a handover, the new Incident Commander may be refused while the old one can still approve. `briefing()` (`agent.py:505`) has the same pattern: it takes recent changes from the transcript by scanning tool messages for the substring `"changes"` | Re-fetch the roster at approval time through the executor, or record handovers as roster changes in state. Read `briefing()`'s changes from the typed tool result, not a substring match |
| Output truncation cuts JSON mid-way | The model may misread half a document. It already caused one triage bug | Summarise or aggregate at the source, as support cases and logs already do |
| Tenant isolation has only one layer in the sketch | The executor's injection is the only layer built. The MCP servers are mocked, and the mock only refuses tenants other than `acme-corp`. A bug in the executor would have nothing behind it | Every MCP server enforces per-tenant credentials, so a wrong `tenant_id` gets no data. Add tests that try to cross tenants through each path: tools, state, memory and verification |
| No context compaction | A six-hour incident will exceed the context window | Replace old raw tool results with summaries. Keep the timeline and the current assessment verbatim |
| No admission control for bursts | In a region outage, every tenant shares the model provider's rate limit | Queue with per-tenant fairness, one shared investigation per burst, and a prefetch-only fallback |
| Stale approvals, file-store race | An approval isn't tied to a state version. The file store checks the version and then writes in two separate steps, so two writers can both pass the check in the gap between them | Approvals carry the state version they were made against. A real store does the check and the write as one conditional write, plus a lease so only one agent process works an incident at a time |

## To raise in the interview

- **The approval roster is a snapshot, not live.** We found this gap between the docs and the code
  in review, and should say so before anyone else does. The docs imply approvals follow the live
  incident, as triage does. In the code, the roster is fixed at the first fetch, so after a handover
  the wrong person can approve. The fix is small: re-fetch the roster at approval time, or record
  handovers in state. `briefing()` has the same pattern. See the gap above.

## Open questions for the interviewers

- The brief mentions "the region-outage burst above", but the scenario text doesn't describe it. We
  assumed many incidents across many tenants at once, all caused by one degraded cloud region. Is that right?
- How much past-incident history is there per tenant, and is it labelled with root cause and fix?
- Is 4 weeks of metric history per service a realistic thing to expect from clients' integrations?
- Which remediations do customers actually pre-approve? That decides how useful the action catalog is.
- Is there appetite for per-tenant auto-approval of low-severity, recurring, verified fixes? Today
  fast-track is still a human approval.
