# SRE Agent: design brainstorm

Working notes for the take-home. This is the "sketching with two colleagues" doc:
what the agent fetches, in what order, how it gets to a decision, and what it is
allowed to do about it. Code follows this doc commit by commit.

## Constraints we are designing for (from the brief)

- One responder, one surface, one agent.
- All third-party data comes through **MCP tool calls** (Datadog, PagerDuty, VCS/CD, runbooks).
- Read-only tools, plus a **small predefined catalog of actions that need human approval**.
  No autonomous actions.
- Multi-tenant SaaS: every call is scoped to one customer.
- Tool and message format: **OpenAI Chat Completions function calling** (`tools=[{"type": "function", ...}]`,
  `strict: true` schemas, `tool_calls` / `role: "tool"` messages). The transcript in that format
  *is* the agent's working state, which makes it easy to persist, replay and evaluate.

## How many agents?

**One LLM agent.** Everything around it is deterministic code, not more agents:

| Component | LLM? | Job |
|---|---|---|
| **Investigator agent** | yes | Decides which evidence to fetch next, forms hypotheses, recommends one next step. |
| Context prefetcher | no | On trigger, fetches the things we *always* need, in parallel, before the first model call. |
| Tool executor / policy gate | no | Validates arguments, injects tenant scope, enforces timeouts and output size, logs every call. |
| Approval gate | no | Turns an action proposal into a pending request for a human. Never executes. |
| Budget / stop controller | no | Step, tool-call and wall-clock limits; stall detection; forces a final answer. |

Why not specialist sub-agents (logs agent, deploys agent, metrics agent) yet: each hand-off
loses context and adds latency and cost, and at 3am the bottleneck is a coherent story, not
parallel reasoning. I would split when one of these becomes true: the tool catalog gets past
~20-30 tools and selection quality drops, a single investigation no longer fits the context
window, or a sub-task needs a different model (for example cheap log clustering).

## What happens when an incident triggers

The order follows "most likely to explain it, cheapest to fetch" first. Most production
incidents are change-induced, so *what changed* comes right after *what is broken*.

| # | Question | Source (MCP tool) | Who calls it | Notes |
|---|---|---|---|---|
| 0 | What fired, when, how urgent? | PagerDuty `get_incident` | prefetch | Gives service, alert, onset time. Onset time anchors every later query window. |
| 1 | Who owns it, what does it depend on? | PagerDuty `get_service_context` | prefetch | Owners, on-call, upstream/downstream deps, runbook link. |
| 2 | What changed recently? | VCS/CD `list_recent_changes` | prefetch | Deploys, config changes, feature-flag flips in `[onset - 2h, now]`. |
| 3 | What else is alerting? | Datadog `list_triggered_monitors` | prefetch | Separates "my service is sick" from "the region is sick". |
| 4 | How bad, since when? | Datadog `query_metrics` | agent | RED (rate, errors, duration) plus saturation (CPU, memory, DB pool). Look for a step change, not just a high value. |
| 5 | What is the error? | Datadog `search_logs` | agent | Top error patterns, grouped, with **first-seen** time. A pattern first seen right after a deploy is strong evidence. |
| 6 | Is it us or a dependency? | Datadog `get_dependency_health` (APM service map) | agent | Downstream error/latency. A sick dependency argues *against* rolling back. |
| 7 | Have we seen this before? | PagerDuty `search_similar_incidents` | agent | Past incidents on this service with similar symptoms, and what fixed them. |
| 8 | What does the team say to do? | `get_runbook` | agent | Approved remediations and their preconditions. |

Rows 0-3 are always needed, so they are prefetched in parallel without the model. That saves
three or four model round-trips (latency and tokens) before the responder sees anything useful.
Rows 4-8 are model-chosen because which ones matter depends on what 0-3 showed.

"Call logs" in two senses, both covered:
- **Application/request logs** of the affected service: row 5.
- **The agent's own tool-call log**: every call the executor makes is recorded with arguments,
  latency, status and output size. That is the audit trail the responder (and we, when evaluating)
  read afterwards.

## From evidence to a decision

The agent keeps a small set of competing hypotheses, each with evidence for and against.
It is explicitly allowed to say "not enough evidence, here is what is missing".

| Pattern in the evidence | Leading hypothesis | Proposed next step (needs approval) |
|---|---|---|
| Deploy/config change shortly before onset, new error pattern first seen after it, dependencies healthy | Bad change | `rollback_deploy` to the previous version |
| Own changes quiet, a downstream dependency has elevated errors/latency, our errors are timeouts/5xx calling it | Dependency degradation | Runbook mitigation (for example `toggle_feature_flag` for a fallback path) and `page_team` for the dependency owner. **Do not roll back.** |
| Request rate step-up, saturation (CPU, pool) climbing, no new error pattern | Capacity | `scale_out` |
| Conflicting or missing evidence (a tool failed, signals disagree) | Unknown | No action. Report what is known, what is missing, and the single most useful thing a human could check. |

Guardrails on any proposal:
- The action must be in the catalog, with arguments that pass validation (for example the
  rollback target must be a deploy that actually exists in the fetched change list).
- It must cite evidence from tool results, not from the model's prior knowledge.
- It is only ever **proposed**. The executor records it as `pending_approval` and tells the model
  so. Nothing in this system can execute it.
- Confidence is capped when any core data source failed.

## When does the loop stop?

1. The model calls the terminal tool `submit_assessment` (structured: summary, hypotheses,
   recommended action, confidence, open questions).
2. Budget exhausted (steps, tool calls, wall clock). We then **force** a final
   `submit_assessment` with `tool_choice`, so the responder always gets a partial answer instead
   of a timeout.
3. Stall: the model keeps repeating identical calls. Treated like budget exhaustion.

## Partial failure

Tools fail at 3am too (Datadog rate limits, MCP server down). A failed tool returns a
structured error as the tool message, the model is told the data is missing, and the final
assessment must list it under open questions. The run never crashes because one source is down.

## Multi-tenancy and safety in the tool layer

- `tenant_id` is **not** a tool parameter. The model cannot choose it. The executor injects it
  from the incident's authenticated context. Strict schemas (`additionalProperties: false`)
  reject anything extra.
- Each tool is tagged `read` or `approval_required` in the registry. That tag, not the prompt,
  decides what happens when it is called.
- Tool output is truncated to a fixed size so one noisy log query cannot blow the context.

## Fitting into PagerDuty's own incident response process

Source: PagerDuty's open-source incident response training (response.pagerduty.com). It is
built on the Incident Command System (ICS): an Incident Commander (IC) who decides, a Scribe who keeps
the timeline, Subject Matter Experts (SMEs) who fix things, and liaisons who handle communication.
The agent should slot into that process, not invent its own.

**Which role the agent plays.** The agent is an **SME's investigator plus a Scribe**. It is never
the IC. The course is explicit that the IC is the single source of truth, that no action happens
unless the IC says so, and that the IC coordinates rather than resolves. That lines up with the
brief's "no autonomous actions": the human responder is the IC, and `propose_action` is the agent
bringing an option to the IC. It also keeps the agent out of the "one person taking on multiple
roles" anti-pattern.

**The IC's three questions become the output contract.** An IC asks the experts:
*What's wrong?* (size-up), *What actions can we take?*, and *What are the risks?* A proposal without
its risk is not decidable. So each proposal should carry:

| IC question | Field (today) | Gap |
|---|---|---|
| What's wrong? | `submit_assessment.impact`, `summary`, `hypotheses` | Done: impact from the business KPI with a suggested severity |
| What can we do? | `propose_action` | ok |
| What are the risks? | `propose_action.risk`, `expected_effect`, `how_to_verify` | Done: the executor rejects remediations without a metric check |
| How confident? | `confidence` | ok |

**Severity from business impact, not CPU.** The course recommends tying severity to a business
metric (orders per second, notifications per second). The agent's size-up should lead with that
metric (for checkout: successful checkouts per minute), and could suggest a severity level for the IC
to confirm. A CPU graph alone cannot tell you severity.

**The response cycle maps onto the loop.** The course describes size-up, stabilize, update, verify,
repeat:

| Cycle step | Agent today | To add |
|---|---|---|
| Size up | prefetch + investigation + impact | |
| Stabilize | `propose_action` (approval-gated, with risk) | |
| Update | `timeline.py`: entries written by code as findings and decisions happen | Done. Posting entries to Slack/Teams is not built. |
| Verify | `Remediator.approve`: run the approved action, poll `how_to_verify` every minute for its window | Verdict comes from code, not the model: worked / did_not_work / unknown (metric unreadable is never success). Outcome is appended to the transcript. |

**Communication rules that apply to agent output too.**
- *Clear is better than concise; no acronym soup.* The responder may have been asleep 2 minutes ago.
- *Avoid the bystander effect.* Never "someone should check the DB". Name a role or person, give a
  time box, and say when the agent will check back.
- *Too-frequent status updates are an anti-pattern.* Post when something changes: a new hypothesis,
  a proposal, a verification result. Not on every tool call.
- *"Do nothing" is an acceptable decision, and no decision is the worst one.* The agent always gives
  a recommendation, even if it is "wait 5 minutes and re-check X". This is why budget stops force
  a final assessment.
- *Avoid tunnel vision ("overly focussed on one issue").* Keep competing hypotheses with evidence
  against, which the assessment already does.

**Handovers and postmortems.** The course encourages IC handovers on long incidents and
blameless postmortems afterwards. Both want the same artifact: a timeline of what was seen, what was
decided, and why. If the agent keeps that timeline as it goes, it becomes the handover brief, the
postmortem draft, and the input to memory for the next incident. That makes State and Memory the
natural next areas to build.

## State: surviving a restart, a deploy, a handover

**What is the state.** One document per incident (`AgentResult`): the OpenAI-format transcript,
proposals with their status and verification outcome, the tool call log, the Scribe timeline, the
cycle number, and a version. Nothing important lives only in process memory.

**When it is saved.** After the prefetch, after every model response (model output is the
expensive, non-deterministic part; re-asking may give a different answer), after every round of
tool results, after each approval round, and at the end. `stop_reason: "in_progress"` marks a
mid-investigation checkpoint.

**Restart.** `SREAgent.recover` continues from the last checkpoint. The awkward case is a crash
after the model asked for tools but before their results were written: the transcript then ends
with unanswered `tool_calls`, which OpenAI rejects on the next request. Recovery re-runs exactly
those calls. That is safe because reads are idempotent, proposals are de-duplicated by the
executor, and nothing the agent can call executes anything. If the agent could execute actions,
this would need idempotency keys per action, and that is the main reason I would keep execution
outside the agent.

**Two writers.** Every save is a compare-and-swap on `version`. If a responder approves something
from Slack while the agent is checkpointing (or a second agent process picks up the same incident),
one write fails with `ConflictError` instead of silently erasing the other. The loser reloads and
retries. Here the store is a file per incident; in production it would be a row or document with a
conditional write, plus a lease so only one agent process works an incident at a time.

**Handover.** The brief (`timeline.handover_brief`) is built from stored state, not from anyone's
memory of the call: impact, leading hypothesis and what was ruled out (with why), what was already
tried and whether it worked, what is waiting for approval, open questions, and the timeline.
The handover itself is a timeline entry.

**What a six-hour incident does to this.** The transcript grows without bound. Before it hits
the context limit it needs compaction: replace old raw tool results with a summary and keep the
timeline and the current assessment verbatim. The timeline is already the compact form. Not built.

## When an approved fix does not work

The `deploy_red_herring` scenario is the case that worries me most: a harmless deploy lands four
minutes before a traffic spike exhausts the DB pool. Every signal the first pass looks at (deploy just
before onset, new error pattern first seen after it, healthy dependencies) says "roll back", with
high confidence. Correlation in time is the agent's strongest heuristic and also its most
dangerous one.

What happens:
1. Verification says `did_not_work` (code decides, from the metric).
2. `SREAgent.resume` continues **the same transcript** with a `[re-investigate]` instruction:
   your leading hypothesis is probably wrong, earlier data is stale, look wider, do not repeat the fix.
   Re-investigating is read-only, so it starts without asking. Anything it proposes still needs approval.
3. The resumed executor carries forward what the incident already knows: deploy ids seen (so the
   rollback guardrail still works), prior proposals and their outcomes, and the call log.
4. The executor **rejects re-proposing a fix that already failed** (or was rejected by a human).
   That is enforced in code, not left to the prompt.
5. The ruled-out hypothesis stays in the assessment with the reason it was ruled out, so the
   responder sees why the agent changed its mind.
6. After `Budget.max_cycles` investigations the agent stops proposing and returns a deterministic
   escalation listing what was tried. No model call. An agent that keeps proposing fixes mid-outage
   is worse than one that says "this needs a human".

What I would change next: the first pass should check saturation whenever the error pattern is a
timeout or pool exhaustion, even when a deploy looks guilty. A real eval set would be built from
cases like this: incidents where the obvious answer was wrong.

## Open questions to raise in the session

- The brief refers to "the region-outage burst above", but the scenario text does not describe
  it. Assumption for now: many incidents across many tenants at once, all pointing at a shared
  cloud region.
- How much past-incident history is there per tenant, and is it already labelled with root cause
  and remediation, or would we have to extract that from free-text timelines?
- Which remediations do customers actually pre-approve in practice? That decides how useful the
  action catalog is.
