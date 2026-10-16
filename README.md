# SRE investigator agent (sketch)

A single-agent sketch of an incident investigator. An incident comes in; the agent gathers context
through tool calls, reasons, and proposes a next step that a human must approve. Once approved,
the action runs and code checks whether it worked. If it did not, the agent re-investigates. State
is checkpointed, so an investigation survives a crash or a handover.

Everything external is mocked: MCP tools, incident data, the action backend and, by default, the
model. Tools and messages use the OpenAI Chat Completions function-calling format, and a real
OpenAI model can be swapped in.

The longer design notes, including the brainstorm this started from, are in
[docs/DESIGN.md](docs/DESIGN.md).

## Run it

Python 3.11+, standard library only.

```bash
py -m sre_agent --scenario bad_deploy
py -m sre_agent --scenario dependency_outage --approve 1,2
py -m sre_agent --scenario bad_deploy --fail datadog_search_logs        # a tool times out
py -m sre_agent --scenario deploy_red_herring --approve 1 --approve 1   # first fix fails, agent re-investigates
py -m unittest discover -s tests -t .
```

Crash and recover, then hand over to the next responder:

```bash
py -m sre_agent --scenario deploy_red_herring --state-dir .state --crash-after-steps 2
py -m sre_agent --scenario deploy_red_herring --state-dir .state --recover --approve 1 --approve 1 --handover-to bob
```

`--llm openai` swaps the mock for a real model (`pip install openai`, `OPENAI_API_KEY`, model from
`OPENAI_MODEL`). The tools stay mocked. I have only run the mock; I have not run a real model
through these scenarios.

### The three scenarios

All three page the same way: *checkout-api 5xx error rate above 5%*. They need different responses.

| Scenario | What is really going on | What the agent does |
|---|---|---|
| `bad_deploy` | A deploy 3 min before onset added a new exception | Proposes a rollback of that exact deploy; verification passes |
| `dependency_outage` | payments-gateway is failing; no recent change | Does **not** roll back. Proposes the runbook's fallback flag and pages the owning team |
| `deploy_red_herring` | A harmless deploy lands just before a traffic spike exhausts the DB pool | Confidently proposes a rollback, which fails verification. Re-investigates wider, finds saturation, proposes scale-out, which passes |

The third one is deliberate: it shows the agent being wrong in the most plausible way, and what
contains the damage.

## How it works, in one pass

1. **Prefetch, no model.** The incident, service ownership and dependencies, recent changes, and
   triggered monitors. These are always needed, so prefetching them saves several model round-trips.
2. **Loop.** The model picks tools (metrics with the business KPI first, grouped error logs,
   dependency health, similar past incidents, the runbook) until it calls `submit_assessment`.
   Budgets on steps, tool calls and wall clock, plus stall detection. If any budget runs out, the
   model is forced to give a final answer, so the responder always gets a partial answer instead of
   a timeout.
3. **Propose.** `propose_action` only records a request. Each proposal must carry a risk, an
   expected effect, and a measurable `how_to_verify`, or the executor rejects it.
4. **Approve, execute, verify.** Outside the model's reach. A human approves; the action runs; code
   polls the metric and returns `worked`, `did_not_work` or `unknown`. A metric that can't be read
   is never counted as success.
5. **Re-investigate** on the same transcript if the fix did not work. The executor refuses to
   re-propose a fix that already failed. After 3 investigation cycles the agent stops and hands
   back to humans.
6. **Throughout:** checkpointed state, a Scribe-style timeline written by code, and a call log.

The agent plays the role of a subject-matter expert plus Scribe, in PagerDuty's own incident
command model. The responder is the Incident Commander and makes every decision.

## Assumptions

From the brief:
- One responder, one surface, one agent.
- Third-party data comes only through MCP tool calls.
- Read-only tools, plus a small catalog of actions that need human approval. No autonomous actions.
- Multi-tenant SaaS.

Mine. Each would be a question for the team:
- **The responder is the Incident Commander.** The agent never decides. That matches "no autonomous
  actions" and keeps the agent out of the "one person holding several roles" anti-pattern.
- **Onset time is available** (PagerDuty's estimate, or derivable from the alerting metric). Every
  query window is anchored on it.
- **Each service declares a business KPI** (here, "successful checkouts per minute") in its
  service metadata. Severity is suggested from that KPI, as PagerDuty's own guidance recommends.
  The SEV thresholds in the mock are illustrative.
- **Runbooks name their remediations** in a form that can be mapped to the action catalog. The mock
  finds them with a regex. A real system would need structured runbooks, or an extraction step.
- **The tenant comes from the authenticated webhook**, and every MCP server enforces it with
  per-tenant credentials. The executor injects the tenant; it does not rely on the model.
- **Region-outage burst.** The brief refers to it "above", but the scenario text I received does
  not describe it. I assumed it means many incidents, across many tenants, triggering within
  minutes, all caused by one cloud region degrading.

## What I went deep on

- **Actions and safety.** Strict schemas. A registry tag, not the prompt, decides what each tool
  can do. Tenant injection. Rollbacks may only target a deploy actually seen in this
  investigation. Proposals must carry risk and a verification check. Execution sits outside the
  model's reach. Verdicts come from code. A failed fix can't be proposed again. A cycle limit hands
  back to humans.
- **State.** One persisted document per incident with:
  - tenant-scoped keys,
  - atomic writes,
  - a version check so two writers conflict instead of silently overwriting each other,
  - checkpoints after every model response,
  - crash recovery that repairs a transcript left with unanswered tool calls,
  - a handover brief built from that stored state.
- **Partial failure** throughout. A failed tool becomes data, confidence is capped, and the gap is
  listed as an open question.

## What I left out, and why

| Left out | Why | How I'd approach it |
|---|---|---|
| **Memory across incidents** | Getting state right comes first. Memory is only as good as what is recorded per incident | The timeline is the raw material. After resolution, distill one record per incident: symptoms, ruled-out causes, the fix that verified, the fixes that did not. Retrieve by service plus symptom, filtered to the tenant. Only fixes that *verified* count as evidence |
| **Multiplayer beyond the version check** | One responder, one surface in scope | Incident state as the single source of truth; every surface renders it; an approval says which state version it was made against; a lease so only one agent process works an incident |
| **Real MCP client, parallel calls** | Mocked per the brief | `asyncio.gather` for prefetch and parallel tool calls, a timeout per call, and a circuit breaker per MCP server |
| **Context compaction** | Scenarios are short | Before the context limit: replace old raw tool results with summaries; keep the timeline and the current assessment verbatim |
| **Evaluations against a real model** | No API key in this loop | The scenarios are the start of an eval set. Run each N times at temperature 0 and measure how often the action is correct, the tool-call count and tokens |
| **Who may approve what** | Single responder | Per-action approval policy (for example, rollback needs the service owner's on-call) checked inside `Remediator.approve` |
| **Posting to Slack or Teams** | One surface, CLI | Post timeline entries as they happen. Not every tool call: too-frequent updates are an anti-pattern in PagerDuty's guidance |
| **Multiple languages** | Out of time | The model reasons in English over tool data. Translate only what humans read (summary, proposals, brief) at the edge, and keep identifiers and metric names untranslated |
| **Growing the integration ecosystem** | Out of time | `registry.py` is the seam. A new integration is an MCP server plus registry entries with a side-effect tag. Its schemas are linted by the same strict-mode test |

## Where it breaks first

Ordered by how soon I expect each to hurt.

### 1. The region-outage burst

Picture hundreds of incidents in ten minutes, across many tenants, all caused by one cloud region.
In this order:

1. **The shared model provider's rate limit.** Model capacity is the one resource every tenant
   shares, and every investigation makes several model calls with a growing context. Per-incident
   budgets exist, but there is **no global budget, queue or per-tenant fairness**, so the first
   tenants to page get served and the rest time out.
2. **Datadog and other MCP rate limits, per tenant.** Each incident runs 4 prefetch reads plus
   about 7 more (11 in the `bad_deploy` run). Fifty incidents for one tenant are hundreds of calls in minutes, mostly
   asking the same questions (the same dependency health, the same region monitors).
3. **Useless duplicate answers.** Even with infinite capacity, a hundred investigations each
   concluding "the region is degraded" in a hundred separate threads is noise for responders who
   are already overloaded.
4. Checkpoint write volume and wall-clock deadlines come after that.

What I would do:
- **Admission control.** A queue in front of the agent with per-tenant fairness and priority by
  urgency and severity.
- **Burst detection.** Many incidents in the same region within the same window run **one shared
  investigation**, and each incident gets a link to it instead of its own.
- **Cache tool results** within a tenant for a short time (dependency health, monitors).
- **Circuit breakers** per MCP server and region, so a degraded Datadog region isn't hammered.
- **Degrade, don't drop.** When model capacity runs out, fall back to prefetch-only mode: the
  deterministic context summary (what changed, who owns it, what else is alerting) with no model
  reasoning. It is still useful, and it needs no model call.

### 2. Confidently wrong on time correlation

`deploy_red_herring`: "deploy just before onset" is the strongest heuristic and the most dangerous
one. Today the damage is contained after the fact: the fix has to verify, a failed fix triggers
re-investigation, and it can't be retried. The cheaper fix is to look at saturation on the first
pass whenever the error is a timeout or pool exhaustion. The real fix is an eval set made of
incidents where the obvious answer was wrong.

### 3. The model writes its own success criterion

`how_to_verify` comes from the model. A model that proposes `error_rate below 0.5` makes any fix
"work". The executor checks the shape and the time window, **not whether the threshold is
sensible**. Fix: require the threshold to be near the metric's baseline (for example within 3x),
or take the check from the runbook rather than the model.

### 4. Non-determinism hidden by the mock

The mock is a hard-coded decision table, so every run is identical. A real model will:
- sometimes skip a tool,
- misread truncated output (truncation currently cuts the JSON mid-way; summarizing on the server
  side would be better),
- phrase things differently each run.

The guardrails don't depend on the model behaving: schemas, the registry tag, the executor checks,
and verdicts from code. Whether its recommendations are any good has to be measured, not assumed.

### 5. Six-hour incidents

The transcript grows without bound. There is no compaction yet, so a long incident eventually
exceeds the context window or becomes slow and expensive per step.

### 6. Smaller ones I know about

- **Stale approvals.** An approval is not tied to the state version it was made against. If things
  changed in between, a human can approve a stale proposal.
- **Store race.** The file store's check-then-write has a small race window. A real store needs a
  conditional write.
- **Recovery and side effects.** Recovery re-runs dangling tool calls. That is safe only because
  nothing the agent calls has side effects. If the agent could ever execute actions, every action
  would need an idempotency key.
- **Tenant isolation.** It depends on MCP servers enforcing tenant credentials. The executor's
  injection is one layer of defence, not the whole of it.

## LLM-specific concerns, briefly

- **Non-determinism.** Temperature 0, strict schemas, and deterministic checks around every
  consequential step. The model proposes; code decides what is allowed and whether it worked.
- **Context.** Prefetch is compact, tool output is truncated, logs come back grouped by pattern
  rather than as raw lines, and the timeline is the long-term summary. Compaction is not built.
- **Evaluation.** The scenarios and their expected outcomes are the start of an eval harness. Next
  steps: more incidents where the obvious answer is wrong, real-model runs, and variance across
  runs.
- **Cost.** Prefetch removes the model turns (and tokens) it would spend asking for context it
  always needs. Per-cycle step and tool
  budgets cap the worst case, the cycle limit caps re-investigations, and verification costs no
  model calls. What's missing is token accounting per tenant, which the burst scenario needs.

## Layout

| Path | What |
|---|---|
| `sre_agent/tools/specs.py` | OpenAI strict function schemas for every tool |
| `sre_agent/tools/registry.py` | Tool -> side effect (read / approval_required / terminal) -> MCP server |
| `sre_agent/tools/executor.py` | Policy gate: validation, tenant injection, timeouts, proposal checks, truncation, call log |
| `sre_agent/agent.py` | Core loop: prefetch, model <-> tools, budgets, stall detection, forced answer, resume, recover, checkpoints |
| `sre_agent/llm.py` | `MockReasoningLLM` (deterministic) and `OpenAIChatClient` |
| `sre_agent/remediation.py` | Human approval, execution and metric verification; unreachable from the model |
| `sre_agent/state.py` | Persisted incident state: tenant-scoped keys, atomic writes, version check |
| `sre_agent/timeline.py` | Scribe timeline written by code, and the handover brief |
| `sre_agent/mocks/` | Three scenarios, the mock MCP servers, and what each action changes |
| `tests/` | 52 tests: schemas, guardrails, scenarios, verification, re-investigation, state and recovery |

## Reading the history

The commits are meant to be reviewed in order:

1. `bb7fafd` brainstorm: what to fetch, in what order, how to decide
2. `76bfdb7` tool specs, policy-gated executor, mocked MCP servers
3. `152cf8f` core loop with a deterministic mock model
4. `576215c` CLI demo
5. `91fbba7` mapping the agent onto PagerDuty's incident response process
6. `23a632e` risk, expected effect and verification on every proposal; impact first in the assessment
7. `30610fa` approve, execute, verify
8. `58a1617` re-investigate when a fix fails
9. `b233af3` timeline, persisted state, crash recovery, handover
10. this README
