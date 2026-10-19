# SRE Agent: decisions, assumptions and shortcomings

A study and talking document for the interview. Every item below was checked against the code at
commit `808c250`. File references are relative to `sre_agent/`. The full reasoning lives in
[README.md](../README.md) and [DESIGN.md](DESIGN.md).

Each item gives:

- **Code:** what the code does, with the file and function.
- **Why:** the reason for the choice.
- **Trade-off:** what it costs or leaves open.
- **Pattern:** the generic agentic pattern it corresponds to.

Items marked **[added in review]** were found while checking the code. They weren't on the original
list.

**The whole design in one sentence:** one model investigates in a tool loop, and deterministic code
around it decides everything that can be decided: severity, what may be proposed, who may approve,
whether a fix worked, and what gets remembered. The model proposes, code decides.

---

## 1. Design decisions

### D1. One agent and a tool loop, inside a deterministic harness

- **Code:** `agent.py:SREAgent._investigate` (line 337) is a single loop: call the model, run its tool
  calls through the executor, append the results, repeat. There is no orchestrator and no sub-agents.
  Everything decidable is decided in plain code: triage (`triage.py`), coverage (`coverage.py`), the
  policy gate (`tools/executor.py`), approval and verification (`remediation.py`), and memory
  (`memory.py:distill`).
- **Why:** every hand-off between agents loses context and adds latency and cost. The model is treated
  as an untrusted component: its output is checked, never obeyed. So the safety properties hold even if
  the model ignores its prompt or is prompt-injected.
- **Trade-off:** one context window holds the whole investigation, which gets tight on long
  incidents. We'd split into sub-agents only when the tool catalog passes about 20-30 tools, an
  investigation no longer fits the window, or a sub-task needs a different model.
- **Pattern:** a single-agent tool-use loop (ReAct style) inside a harness. The model is treated as
  untrusted input: "the model proposes, code decides".

### D2. Prefetch in two waves, recorded as forged tool calls, with the transcript as the only state

- **Code:** `agent.py:run` (line 96) runs two prefetch waves before the first model call, with no
  model involved.
  - `prefetch_identity` (line 40) fetches the incident, the service context and the service's
    integrations.
  - `prefetch_signals` (line 50) fetches changes, alerts, `business_kpi`, `error_rate` and support
    cases, but only from sources the integrations provide.
  - `_prefetch` (line 144) writes each wave as one synthetic `assistant` message with `tool_calls`
    (ids `prefetch_a_N`, `prefetch_b_N`), followed by one `tool` message per call. It looks exactly as
    if the model had asked.
  - The transcript (`AgentResult.messages`) is the one persisted document. `ToolExecutor.__init__`
    (`tools/executor.py:70-77`) rebuilds all its "seen" state from it: seen deploys, seen metrics,
    similar incidents, recalled fixes, and the router.
- **Why:**
  - It saves 3-4 model round trips on data that every investigation needs anyway.
  - Every path (model, prefetch, recovery, verification, memory, approvals) reads one format.
  - Fetched data stays in `tool` messages, kept apart from instructions.
  - Rebuilding seen-state from the transcript means nothing else needs persisting.
- **Trade-off:**
  - The prefetch is fixed: it fetches some things a given incident doesn't need.
  - Forged calls must follow the API's rules: every `tool_call_id` needs an answer.
  - The two waves run sequentially here.
- **Pattern:** deterministic context pre-loading, with the transcript as the single source of truth
  (event-sourcing style: state is derived by replaying it).

### D3. Triage and coverage run in code before any model call, and fail closed

- **Code:**
  - `agent.py:run` (lines 122-139) calls `coverage(executor.router)` and then `triage(...)`, and gives
    the model both results as a `[triage]`/`[coverage]` user message.
  - `triage.py:triage` (line 59) sets severity from the KPI drop (≥50% SEV-1, ≥20% SEV-2, ≥5% SEV-3)
    and raises it on a customer burst (≥3 related cases and more than 3x the expected rate).
  - `triage.py:raise_only` (line 135) applies the model's `suggested_severity` only if it is worse.
  - Unknowns fail closed:
    - malformed support-case data counts as `failed`, never as zero (`cases_status`);
    - SEV-4/5 counts as `low` only when we positively know customers aren't reporting it;
    - a missing KPI defaults to SEV-3;
    - unreadable integrations mean no proposals at all (`coverage.py:coverage`, line 44).
- **Why:** severity decides how big the response is: who joins, who may approve, whether fast-track
  applies. It has to be fast, explainable, and the same every time for the same data.
  - **Lowering severity is a human decision.** Otherwise a prompt injection could argue an incident
    down.
  - **Coverage is shown up front,** so nobody mistakes a blind spot for "all clear".
- **Trade-off:** the thresholds are fixed constants here, where production would use per-tenant
  configuration. A ratchet can over-escalate, and only a human brings severity back down.
- **Pattern:** deterministic pre-processing; monotonic state (a ratchet); fail closed.

### D4. Three side-effect tiers in the registry, and the model never executes anything

- **Code:**
  - `models.py:SideEffect` has three values: `READ`, `APPROVAL_REQUIRED` and `TERMINAL`.
  - `tools/registry.py:_TOOLS` tags every tool. Only `propose_action` is `APPROVAL_REQUIRED`, and only
    `submit_assessment` is `TERMINAL`. The executor branches on this tag (`execute`, lines 94-105),
    never on the prompt.
  - No tool maps to `remediation.py`. The only way an action runs is a human calling
    `Remediator.approve`.
  - `fast_track` is computed by code (`triage.py:fast_track_reason`). It only changes how the approval
    is presented. A human still approves.
- **Why:** this is the hard boundary between investigating and acting. A tool the model can't call
  can't be misused by the model.
- **Trade-off:** even a trivial, recurring, verified fix needs a human click. There's no autonomous
  mode.
- **Pattern:** side-effect classification of tools (capability tiers), with a human in the loop
  for every write.

### D5. `ToolExecutor.execute` is the policy enforcement point

- **Code:** `tools/executor.py:execute` (line 79) handles every model call. In order, it:
  - **checks the tool:** it must be known and allowed on this path (`allowed_tools`);
  - **re-validates the arguments** against the strict schema (`_validate`, line 295), even though
    OpenAI's strict mode already does;
  - **injects the tenant:** `tenant_id` is never in a schema, and the executor adds it to every MCP,
    connector and memory call;
  - **routes capability tools** to the service's vendor connector (`_call_capability`, line 141);
  - **turns failures into data:** an error comes back as `{"error": ..., "detail": ...}`, never as an
    exception;
  - **truncates** the output to 4,000 characters;
  - **logs** the call as a `ToolCallRecord`.

  For proposals, `_propose` (line 175) also refuses, or for a pending duplicate de-duplicates:
  - a target that isn't the incident's service;
  - a proposal with no evidence;
  - an action the service's coverage can't support (`blocked_by`);
  - a rollback to a deploy not seen in `changes_list`;
  - `replicas` outside 1-50;
  - an empty risk or expected effect;
  - a missing `how_to_verify` (only `page_team` may omit it).

  It then calls `_check_verification` (line 251), which requires:
  - a window of 1-60 minutes;
  - a metric the agent has actually queried;
  - a threshold within 3x of that metric's baseline (`MAX_BASELINE_RATIO`);
  - a check that doesn't already pass.

  Finally, it refuses duplicates of a fix that failed or was rejected, and returns "already proposed"
  for a pending duplicate.
- **Why:** these are things the prompt asks for and code enforces. A rejection is returned to the
  model as data with a reason, so the model can correct itself in the next step instead of the
  loop crashing.
- **Trade-off:** the rules are hand-written per action. A new action means new rules here. The 3x
  ratio is one global constant for every metric.
- **Pattern:** a policy enforcement point (tool gateway), where errors become observations the model
  can act on.

### D6. Budgets, stall detection, and "degrade, don't die"

- **Code:**
  - `agent.py:Budget` (line 32) sets `max_steps=6`, `max_tool_calls=20`, `max_wall_clock_s=120`,
    `max_stalled_steps=2` and `max_cycles=3`.
  - `_run_call` (line 464) builds a signature for each call from the tool name and its arguments as
    JSON with sorted keys. A step that only repeats earlier signatures counts as stalled.
  - Past the tool budget, each further call is answered with `{"error": "tool_budget_exhausted"}`.
    Every `tool_call_id` must get a reply, or the next request is invalid.
  - On any limit, `_finish` calls `_force_assessment` (line 429). It pins
    `tool_choice={"function": {"name": "submit_assessment"}}`, refuses any other tool, and returns a
    hard-coded low-confidence assessment if the model still fails.
- **Why:** the responder must always get the best partial answer within about two minutes, even if
  the model loops, stalls or is slow.
- **Trade-off:**
  - The deadline is checked only between steps, so one slow model call can overrun it.
  - The budgets apply per investigation cycle, so an incident can use up to three times as much in
    total.
  - Stall detection catches exact repeats only, not near-duplicates.
- **Pattern:** bounded autonomy (step, token and time budgets) and a circuit breaker. Forcing the
  tool choice gives graceful degradation.

### D7. Human approval, verification in code, bounded retry, then escalation

- **Code:**
  - `remediation.py:ApprovalPolicy.check` (line 56) allows only people on the PagerDuty responder
    roster. In a major incident, only the Incident Commander may approve. With no roster, nobody can
    approve, and an unknown response mode is treated as `major`.
  - `Remediator.approve` (line 100) moves a proposal from pending to approved, runs it, and then calls
    `_verify`.
  - `_verify` (line 149) polls `metrics_query` through a `ToolExecutor`, so verification reads are
    tenant-scoped and logged too. The first passing reading counts as `worked`. No readable value
    gives `unknown`. Anything else is `did_not_work`.
  - `agent.py:resume` (line 162) accepts only `did_not_work`. It re-investigates on the same
    transcript and tells the model its leading hypothesis is probably wrong.
  - Past `max_cycles`, `_escalation` (line 484) hands the incident back to humans with a
    deterministic assessment. No model call is made and nothing new is proposed.
- **Why:** the model can't talk itself into "the fix worked". Who may approve comes from the
  incident, never from chat text. A failed fix triggers self-correction, but only a bounded amount.
- **Trade-off:** one passing reading can be a lucky dip. The roster is a snapshot (S4). `unknown` is
  a dead end (S5).
- **Pattern:** a human-in-the-loop approval gate, with an external verifier (code as the judge, not
  the LLM as the judge), and bounded retry with escalation.

### D8. Checkpoint after every model response and tool round; recover replays dangling calls

- **Code:**
  - `_investigate` calls `_checkpoint` after each model response (line 351) and after each round of
    tool results (line 369).
  - `state.py:FileStateStore.save` (line 66) writes atomically (a temp file, then `os.replace`) and
    uses compare-and-swap on `version`.
  - `agent.py:recover` (line 214) loads an `in_progress` checkpoint. `_answer_dangling_calls`
    (line 407) runs any tool calls of the last assistant message that never got an answer, and the
    loop then continues.
- **Why:**
  - A model response costs money and time, so it is never lost.
  - Replaying the dangling calls is what keeps the transcript valid for the next model call.
  - Replay is safe only because no model-callable tool has side effects. Reads are idempotent, and
    proposals are de-duplicated.
- **Trade-off:**
  - The compare-and-swap is check-then-write in two steps, which leaves a small race window.
  - The approval path isn't checkpointed in the same way (see S11).
- **Pattern:** checkpoint and replay (durable execution); optimistic concurrency.

### D9. `ask()`: a guardrail sandwich for engineers' questions

- **Code:** `agent.py:ask` (line 238) answers an engineer's question in three layers:
  1. **`guardrails.py:screen`** (line 153): deterministic checks before any model call. It first
     normalises the text (compatibility forms, zero-width characters, case). It then checks for
     injection, prompt extraction, secrets, exploits, security reports (redirected to the security
     process), action requests, code requests, off-topic questions, and anything over 2,000
     characters.
  2. **An isolated thread:** a fresh transcript with `QA_PROMPT`. The incident is passed in as a
     `briefing()` (data, not instructions). The executor is limited to `allowed_tools=read_only`, the
     thread gets at most 3 rounds of lookups, and it never writes to the investigation transcript.
  3. **`filter_answer`** (line 197): blocks an answer that copies 8 or more consecutive words from
     either prompt, contains a string shaped like a secret, or contains code.

  Every question is recorded in `state.questions` with its verdict, for audit.
- **Why:** no single layer holds. A regex can be rephrased around, and a prompt can be ignored. So
  even if an injection gets past layer 1, it has nothing to act on: it can't propose, approve or
  write.
- **Trade-off:** the regex layer has false positives and is English-only. An answer that leaks data
  without quoting it word for word isn't caught.
- **Pattern:** defence in depth with least privilege. The thread runs in a quarantined context with
  read-only capabilities.

### D10. Memory is a deterministic distillation of verified outcomes, recalled by key

- **Code:**
  - `memory.py:distill` (line 108) builds a record from tool results and verdicts: the top two
    error-log templates (normalised, so versions and numbers don't matter), which metrics moved,
    the fixes that `worked` and those that `did_not_work`, the causes ruled out, the root cause (only
    if a fix verified), and the severity.
  - `FileMemoryStore.recall` (line 181) is scoped to one tenant, and to the same service or one it
    depends on. It scores `0.7·Jaccard(error signature) + 0.3·Jaccard(metric shape)`, decayed by age
    with a 90-day half-life. It returns nothing below 0.35, and at most the top 3, as compact slices.
  - There are no embeddings. A human can retract a record (`retract`).
- **Why:** memory must not be a place to plant "facts". Writes come from code-verified outcomes, not
  from what the model said. Key-based recall is explainable, and a weak match returns nothing rather
  than noise.
- **Trade-off:**
  - Token Jaccard misses paraphrased errors that embeddings would catch.
  - One JSON file per tenant doesn't scale.
  - **[added in review]** Two fields do carry model text: `root_cause` (`hypotheses[0]["cause"]`) and
    `ruled_out` (the model's low-confidence alternative hypotheses). `root_cause` is gated by a fix
    that verified. `ruled_out` isn't gated inside `distill`; only the CLI's "remember only if
    something worked" check protects it. So "memory holds no model prose" is too strong. Say "memory
    holds verified outcomes, plus the model's cause labels when a fix was verified".
- **Pattern:** episodic memory with write-time distillation, and retrieval with a relevance
  threshold.

### D11. Tenant isolation: the first layer is built into the agent

- **Code:**
  - `models.py:IncidentRef` takes `tenant_id` from the authenticated webhook.
  - `tenant_id` appears in no tool schema (`tools/specs.py`), and strict validation rejects extra
    fields. The executor injects it into every call.
  - State and memory are stored per tenant, with validated ids (`state.py:_path`,
    `memory.py:_path`).
- **Why:** the model can't name or switch tenants, even through injected text.
- **Trade-off:** this is the only layer built. The mock MCP server simply refuses tenants other than
  `acme-corp`.
- **Pattern:** identity injected by the harness, never chosen by the model (preventing the confused
  deputy problem).

### D12. Capability tools rather than vendor tools

- **Code:**
  - The model sees `metrics_query`, `logs_search` and so on (`registry.py`, where each capability tool
    has a `capability` field).
  - `connectors.py:Router` maps each capability to the vendor connector the service has integrated:
    Datadog, New Relic, GitHub Actions or Salesforce.
  - Each connector translates the call into its vendor's query language and normalises the answer
    back.
- **Why:** prompts, triage, verification and memory all work on one shape. A new vendor is one class.
  A capability with no integration isn't offered to the model at all (`agent.py:_tools`).
- **Trade-off:** each connector decides what "baseline" means for its vendor, and the agent trusts it.
- **Pattern:** an adapter layer (anti-corruption layer) between the agent and third-party tools.

### D13. Decision made in review: verify against historical normal ranges when history exists

The sketch uses a stand-in, because the mocks carry a single `baseline` number per metric. In
production, where historical data is available, a fix counts as working only when its metric returns
to its own normal range and stays there.

| | In the sketch today | Preferred, with history |
|---|---|---|
| What "normal" is | One `baseline` number from the metrics vendor | A normal band (p5, p50, p95) for the same hour and weekday over the last 4 weeks, excluding past incident windows |
| Success threshold | Written by the model. Rejected if more than 3x the baseline (`MAX_BASELINE_RATIO = 3.0`, one value for every metric) | Derived by code from the band. The model picks only which metric shows recovery, or the runbook names it |
| When a fix "worked" | The first single reading that passes (`Remediator._verify`) | Several consecutive readings inside the band, for example 3 one-minute readings in a row |
| No history (new service or metric) | Not applicable | Fall back to today's ratio rule or a runbook threshold, and say so in the proposal |

- **Why history is better:**
  - A single baseline can't tell a noisy metric from a steady one.
  - A threshold derived by code from data can't be gamed by the model.
  - Consecutive readings stop one lucky dip from counting as a successful fix.
- **What history gets wrong:**
  - Past incidents widen the band, so exclude incident windows.
  - Seasonality: compare the same hour and weekday, and allow a manual override for known peaks.
  - Normal moves after big deploys: keep a short-window band next to the long one.
  - Recovery isn't always caused by the fix, so also check the metric the action targets, such as
    pool saturation for `scale_out`.
- **Note:** the mock model hard-codes `threshold: 0.01` for `error_rate` (`llm.py`). It isn't
  derived from the baseline.

---

## 2. Assumptions

### A1. Incidents are metric-triggered, so the success criterion is the same threshold in reverse

- **Code:** every action except `page_team` must carry a `how_to_verify` metric check
  (`executor.py:_propose`, line 214). `page_team` may omit it, and then `_verify` returns
  `not_applicable` (`remediation.py`, line 153).
- **Why:** if a metric breached, its recovery is the natural, measurable proof that a fix worked.
- **Trade-off:** incidents without a metric, such as a data leak, a security event or a correctness
  bug with no error signal, can only end with `page_team` and `not_applicable`. Security questions in
  `ask` are redirected to the security process.
- **Pattern:** verifiable goals (the agent may only act where success is measurable).

### A2. The approver's identity comes from the surface's SSO, never from chat text

- **Code:** `Remediator.approve(proposal, approved_by)` takes the identity as an argument. The
  `ApprovalPolicy` docstring states the assumption. `screen` blocks "I approve" and "go ahead" in
  chat (`_ACTION_REQUEST`).
- **Why:** a chat message is untrusted input. Approval must be an authenticated act.
- **Trade-off:** the sketch has no authentication itself. In the CLI, `--approver` is a flag.
- **Pattern:** out-of-band authorisation for human-in-the-loop actions.

### A3. Reads are idempotent and cheap enough to repeat on recovery

- **Code:** `recover` re-runs dangling calls without idempotency keys, and the docstring says so
  (`agent.py`, line 218).
- **Why:** this keeps recovery simple. A repeated read only costs a little.
- **Trade-off:** it breaks the moment a model-callable tool gets a side effect. Then every action
  would need an idempotency key.
- **Pattern:** at-least-once replay of idempotent steps.

### A4. The integrations list is truthful

- **Code:**
  - `prefetch_signals` (line 50) reads only the sources the service's integrations provide. If the
    integrations lookup failed, it tries everything ("for reads, failing open costs a few errors").
  - `coverage` and `_propose` block every proposal when the integrations are unknown, and `_tools`
    offers the model no capability tools.
- **Why:** the client's configuration is the best source for what exists, and asking the model to
  guess would be worse.
- **Trade-off:** a stale or wrong integration record means missing data or misrouted queries.
  **[added in review]** When the integrations are unreadable, the router is `None`, so every wave-2
  call returns `integrations_unknown` (`executor.py:_call_capability`, line 143). The fail-open is
  nominal: it yields errors, not data.
- **Pattern:** configuration as the source of the agent's capabilities; fail open for reads and
  closed for writes.

### A5. One investigation per incident at a time

- **Code:** there is no lock or lease. Compare-and-swap on `version` (`state.py:save`) detects a
  second writer and raises `ConflictError`, but doesn't prevent one.
- **Why:** the brief describes one responder, one surface and one agent.
- **Trade-off:** two processes can both investigate, and the loser finds out only when it saves.
- **Pattern:** optimistic concurrency, without leader election.

### A6. Other assumptions

- **From the brief:** one responder, one surface and one agent. Third-party data comes only through
  MCP. Read-only tools plus a small action catalog that needs approval. Multi-tenant SaaS.
- **The responder is the Incident Commander.** The agent is a subject matter expert and scribe. It
  never decides (`prompts.py:SYSTEM_PROMPT`).
- **Historical metric data is available in production** through the client's metrics vendor (D13).
- **Onset time is available**, from PagerDuty or the alerting metric. It sets every query window.
- **Each service declares a business KPI**, and severity is based on it.
- **Runbooks name their remediations** in a form that maps to the action catalog.
- **Every MCP server enforces per-tenant credentials** (D11 is the agent's side of this).
- **Thresholds in the mocks are illustrative.** In practice they are per-tenant configuration.

---

## 3. Shortcomings and what production would need

Ordered roughly by how much each one matters.

### S1. No evaluation harness

- **Code:** the 130 tests run against `llm.py:MockReasoningLLM`, a hard-coded decision table that
  follows the playbook. `OpenAIChatClient` is wired in (`--llm openai`), but no real model has been
  run through the scenarios. The mock was used to avoid API costs, since this is an exercise.
- **Why it matters:** the tests prove the guardrails hold whatever the model does. They prove
  nothing about whether a real model finds the root cause.
- **What production needs:** an eval set of scripted incidents with a known cause, including cases
  where the obvious answer is wrong (`deploy_red_herring` is one).
  - **Run:** each case N times, on every prompt or model change.
  - **Score:**
    - root cause correct;
    - proposed action correct and allowed;
    - the proposal's `how_to_verify` accepted by the executor;
    - the assessment admits what it couldn't check;
    - tool calls, tokens and wall-clock time.
  - **Free first run:** `OpenAIChatClient` honours `OPENAI_BASE_URL`, so a free tier (Gemini, Groq,
    GitHub Models) or a local model works with no code change. We tried a local 27B model through
    Ollama, but abandoned it when the machine went down mid-run.
- **Pattern:** offline evals as regression tests for prompts and models.

### S2. Observability

- **Code:** `models.py:ToolCallRecord` stores `status` (`ok`, `error`, `rejected`,
  `pending_approval`) and a free-text `error`. There is no stable rejection code, and nothing is
  aggregated across incidents. The timeline is for humans, not metrics.
- **What production needs:**
  - **Stable codes:** for example `NO_EVIDENCE`, `UNSEEN_DEPLOY`, `VERIFY_UNSEEN_METRIC`,
    `VERIFY_TOO_FAR_FROM_BASELINE`, `VERIFY_ALREADY_PASSING`, `COVERAGE_BLOCKED`,
    `DUPLICATE_FAILED`, `DUPLICATE_REJECTED` and `SCHEMA_INVALID`.
  - **The metrics that matter:**
    - rejection rate by code;
    - the rate at which humans approve or reject proposals;
    - the verdict mix (`worked`, `did_not_work`, `unknown`);
    - the distribution of stop reasons (`assessment_submitted`, `stalled`, `deadline`,
      `tool_budget`, `step_budget`);
    - time to first assessment;
    - tool calls and tokens per investigation.
  - **Model tiering:** set a baseline with a large model, then try a cheaper one. Keep it if the
    rejection rate, approval rate and eval scores hold.
- **Pattern:** agent telemetry, where guardrail rejections are a quality signal.

### S3. Long-running work is synchronous

- **Code:**
  - `Remediator._verify` polls in a loop and calls `self.sleep(poll_interval_s)` between readings.
    The CLI passes a no-op sleep.
  - `approve` blocks the process for the whole verification window, up to 60 minutes.
  - `_prefetch` is sequential, and its docstring says production would run it concurrently.
- **What production needs:** a durable workflow engine. In Temporal, for example:
  - tool reads and the action are activities with retries;
  - the approval is a signal;
  - verification is a timer;
  - a crash resumes from the workflow history.

  The alternative is an event-driven state machine on a job queue, where each state transition is
  persisted.
- **Pattern:** durable execution; human approval as an asynchronous signal.

### S4. Substring scans of the transcript, and a roster that is a snapshot

- **Code:**
  - `ApprovalPolicy.roster` (`remediation.py`, line 47) returns the first tool message containing
    `"responders"`.
  - `briefing` (`agent.py`, line 501) scans for `"changes"` and keeps the last match, not the first.
  - The `ApprovalPolicy` docstring says it is "read live from the incident at approval time". That is
    true for triage (`state.triage`) but not for the roster.
- **Why it matters:** after a handover, the new Incident Commander may be refused while the old one
  can still approve. A substring scan can also match an unrelated payload.
- **What production needs:** a typed snapshot in `AgentResult` (roster, recent changes), refreshed
  through the executor at approval time and on handover. `ApprovalPolicy` and `briefing()` read the
  snapshot, never the transcript.
- **Pattern:** typed working state next to the raw transcript, rather than re-parsing the
  transcript.

### S5. An `unknown` verdict is a dead end

- **Code:** when the metric can't be read, `_verify` returns `unknown` (line 184). `resume` accepts
  only `did_not_work` (`agent.py`, line 169), and the CLI only reacts to `did_not_work`.
  **[added in review]** `not_executed` (the action failed to run) is the same dead end. And if a
  later cycle runs, for example after another fix fails, the model may propose an `unknown` or
  `execution_failed` action again: `_propose` blocks only duplicates that `did_not_work`, are
  `rejected` or are pending. So an action whose effect is unknown can run twice.
- **What production needs:** a policy decided in code:
  - escalate to the Incident Commander as "executed, unverified";
  - keep re-reading the metric for a bounded time;
  - block re-proposals until a human decides;
  - never feed `unknown` into memory as a success or a failure.
- **Pattern:** an explicit terminal or escalation state for every outcome of a verifier.

### S6. No caching, tracing, or versioning of prompts and models

- **Code:**
  - `OpenAIChatClient` sends the whole transcript on every call, and nothing is cached.
  - There is no tracing beyond the call log and the timeline.
  - The model comes from `OPENAI_MODEL`, defaulting to `gpt-4.1`. The prompts are string constants in
    `prompts.py`. Neither is recorded in `AgentResult`.
- **What production needs:**
  - **Caching:** prompt caching on the stable prefix (system prompt, tools, prefetch), and short-lived
    caching of results for repeated reads.
  - **Tracing:** OpenTelemetry or Langfuse-style traces per step and per tool call.
  - **Versioning:** pinned model versions, and prompts treated as versioned dependencies, with both
    versions recorded on every incident so an eval regression can be traced back.
- **Pattern:** LLM observability; prompts as code.

### S7. No locking for concurrent approvals or parallel investigations

- **Code:** `_require_pending` is an in-memory check. Two surfaces approving the same proposal at the
  same time both pass it. The compare-and-swap catches only the later save, after both actions have
  run. An approval isn't tied to the state version it was made against.
- **What production needs:**
  - the check and the write as one conditional write;
  - a lease, so only one agent process works an incident at a time;
  - approvals that carry the state version they were made against.
- **Pattern:** a lease or leader election per entity; idempotent commands.

### S8. Verification uses one reading and a fixed 3x ratio

- **Code:** in `_verify`, the first passing reading counts as `worked`. `MAX_BASELINE_RATIO = 3.0`
  applies to every metric. **[added in review]** When `baseline` is 0 or less, the ratio check is
  skipped (`executor.py`, line 270), so any threshold that doesn't already pass is accepted.
- **What production needs:** D13: consecutive readings first, because they are cheap, and then
  bands based on history.

### S9. Truncation cuts JSON mid-way

- **Code:** `_truncate` (`executor.py`, line 288) returns `{"truncated": true, "content": <first 4,000
  characters>}`. `_observe` can't parse that, so a truncated metrics result can't anchor a
  verification check. It already caused one triage bug.
- **What production needs:** summarise or aggregate at the source, as support cases and logs already
  do.

### S10. Tenant isolation has only one layer

- **Code:** D11 is the only layer built. A bug in the executor would have nothing behind it.
- **What production needs:**
  - every MCP server enforces per-tenant credentials;
  - tests that try to cross tenants through each path: tools, state, memory and verification.

### S11. **[added in review]** Execution isn't recorded durably before it happens

- **Code:** `Remediator.approve` runs the action and then sets the proposal's status in memory. The
  CLI saves state only after the loop of approvals (`__main__.py`). `ActionRunner.run` has no
  idempotency key. If the process dies after `runner.run` and before the save, the restored proposal
  is still `pending_approval`, so the action could be approved and run a second time.
- **What production needs:** persist an "approved, executing" record before the side effect (an
  intent log or outbox), pass an idempotency key to the runner, and reconcile on restart.
- **Pattern:** write-ahead intent with idempotent execution.

### S12. **[added in review]** The fast-track docstring claims more than the code does

- **Code:** the `triage.py:fast_track_reason` docstring says fast-track changes "how it is presented
  and who may approve it". `ApprovalPolicy.check` never looks at `fast_track`, so it changes only the
  presentation. Fast-track is withdrawn if severity is later raised (`agent.py:_finish`).
- **Why it matters:** the docs promise an approval rule that isn't implemented. The fix is to correct
  the docstring, or to implement the rule.

### S13. Other gaps

- **No timeout on the model call [added in review].** `OpenAIChatClient` sets no timeout, so the
  120-second deadline doesn't bound a hung call.
- **No context compaction.** A six-hour incident will exceed the context window. Replace old raw tool
  results with summaries, and keep the timeline and the current assessment verbatim.
- **No admission control for bursts.** In a region outage, every tenant shares the model provider's
  rate limit. Queue with per-tenant fairness, run one shared investigation per burst, and fall back
  to prefetch only.
- **Fast-track trusts labelled history.** If `resolution_action` or `verified` labels on past
  incidents are wrong, the wrong fix gets fast-tracked. A human still approves it.

---

## Open questions for the interviewers

- The brief mentions "the region-outage burst above", but the scenario text doesn't describe it. We
  assumed many incidents across many tenants at once, all caused by one degraded cloud region. Is
  that right?
- How much past-incident history is there per tenant, and is it labelled with root cause and fix?
- Is 4 weeks of metric history per service a realistic thing to expect from clients' integrations?
- Which remediations do customers actually pre-approve? That decides how useful the action catalog
  is.
- Is there appetite for per-tenant auto-approval of low-severity, recurring, verified fixes? Today
  fast-track is still a human approval.

---

## Appendix: likely interview questions and crisp answers

**Why one agent and not several?**
Every hand-off loses context and adds latency and cost. One loop with a good harness is easier to
make safe and to debug. I'd split when the tools pass about 20-30, an investigation no longer fits
the context, or a sub-task needs a different model.

**How do you stop the model doing something dangerous?**
It can't execute anything. Tools are tagged by side effect in the registry, and the only write the
model can call is `propose_action`, which creates a request. Execution lives in `Remediator`, which
no tool reaches, and a human on the roster has to approve.

**What if the model is prompt-injected through a log line?**
Tool output is data in `tool` messages. The worst an injection can do is change what the model
proposes, and every proposal still passes the executor's checks and a human. The tenant is injected
by code, so the model can't reach another tenant's data.

**How do you know a fix worked?**
Code decides, not the model. `_verify` polls the metric named in `how_to_verify`. When the proposal
was made, the executor already checked that the threshold is anchored on a metric the agent saw,
within 3x of its baseline, and not already passing. Next step: consecutive readings and bands based
on history.

**Why prefetch, and why fake the tool calls?**
It saves 3-4 round trips on data every investigation needs. Recording the results as tool calls
keeps one transcript format for every path. Prefetch, model, recovery and memory all read it, and the
executor rebuilds its state from it.

**What happens if the model loops or stalls?**
There are budgets: 6 steps, 20 tool calls, 120 seconds and 2 stalled steps. Stalls are detected
from normalised call signatures. On any limit, we force `tool_choice` to `submit_assessment`:
degrade, don't die.

**What happens if the process crashes mid-investigation?**
We checkpoint after every model response and tool round. `recover` answers the dangling tool calls
and continues. Replay is safe because no model-callable tool has side effects.

**What is the biggest gap?**
Evals. The tests prove the guardrails, not the reasoning. I'd build scripted incidents with known
causes, including traps like `deploy_red_herring`, run each N times per prompt or model change, and
score root cause, action, rejections and cost.

**What would you change first for production?**
- Durable execution for approval and verification, with Temporal-style signals and timers.
- An intent record and an idempotency key before every action.
- Leases and conditional writes on state.
- A typed snapshot for the roster.
- A policy for `unknown` verdicts.

**How would you make it cheaper?**
Prompt caching on the stable prefix. Then measure, and tier models: set a baseline with a large
model, and move to a cheaper one only if the eval scores and the rejection and approval rates hold.

**How does memory avoid poisoning?**
Records are distilled by code from verified outcomes, never written by the model or users. Recall is
tenant- and service-scoped, decays with age, and has a threshold, so a weak match returns nothing.
I'd be honest that the cause labels are model text, kept only when a fix verified.

**Something you found in your own design?**
- The roster is a snapshot, although the docstring says it is read live.
- An `unknown` verdict is neither resumable nor escalated.
- An action can run twice if the process dies between running it and saving.
