"""Guardrails for engineers asking the agent questions during an incident.

Layered, because no single layer holds on its own:

1. `screen` (this module): deterministic checks on the question *before* any model sees it.
   Cheap, predictable, easy to audit. Catches the obvious cases:
     - prompt injection ("ignore previous instructions", "you are now ...")
     - attempts to extract the system prompt, configuration or how the agent is built
     - requests for credentials or secrets
     - security: exploitation requests are refused; vulnerability/breach reports are redirected to the
       security incident process, which has its own team and communication rules
     - code requests ("write me the code for ...") and anything not about this incident
   Regex alone is easy to get around (rephrasing, other languages) and can block legitimate questions,
   so it is only the first layer.
2. Scope, structurally (`SREAgent.ask`): every question runs in its own short-lived thread with a
   question-answering prompt, the incident as *data*, and read-only tools. A question can never write
   to the investigation transcript, propose an action, or approve one, so an injection that gets
   past layer 1 has nothing to act on.
3. `filter_answer` (this module): checks the answer before it is shown, for system-prompt text,
   secret-looking strings and code, whatever the question was.

Every screened question is recorded with its category, for audit.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

MAX_QUESTION_CHARS = 2000

_INJECTION = [
    r"\bignore (all |any |the )?(previous|prior|above|earlier) (instructions|prompts?|rules)",
    r"\bdisregard (all |any |the |your )?(previous |prior )?(instructions|rules|guidelines)",
    r"\byou are now\b",
    r"\b(act|behave) as (an? )?(unrestricted|unfiltered|different|new)\b",
    r"\bpretend (to be|you are)\b",
    r"\b(developer|god|admin|debug|jailbreak) mode\b",
    r"\bnew (instructions|rules|persona)\s*:",
    r"\bfrom now on,? you\b",
]
_PROMPT_EXTRACTION = [
    r"\b(system|initial|hidden|original) (prompt|instructions|message)\b",
    r"\b(show|reveal|print|repeat|tell|give|share|dump|output) (me )?(your|the) (prompt|instructions|rules|config(uration)?|tools|functions|source( code)?)\b",
    r"\bhow (are|were) you (coded|built|programmed|implemented|configured|made)\b",
    r"\bwhat (model|llm) are you\b",
    r"\b(your|the agent'?s) (source code|code base|codebase|implementation|tool (list|definitions|schemas?))\b",
]
_SECRETS = [
    r"\b(api|access|secret|auth|bearer|session) ?(key|token)s?\b",
    r"\bpasswords?\b",
    r"\bcredentials?\b",
    r"\bprivate key\b",
    r"\bconnection string\b",
]
_SECURITY_EXPLOIT = [
    r"\bexploit(s|ing|ation)?\b",
    r"\bpayloads? (to|for|that)\b",
    r"\bbypass (the )?(auth|authentication|authorization|login|waf|rate limit)",
    r"\bprivilege escalation\b",
    r"\breverse shell\b",
    r"\b(sql|command|code) injection\b",
    r"\bhow (do|can|could) (i|we|you|someone) hack\b",
]
_SECURITY_REPORT = [
    r"\bvulnerabilit(y|ies)\b",
    r"\bcve-?\d*\b",
    r"\b(data )?breach(ed)?\b",
    r"\bcompromised\b",
    r"\bunauthori[sz]ed access\b",
    r"\bdata (leak|exfiltration)\b",
    r"\bmalware\b",
]
# Asking the agent to act, or "approving" in chat. Neither is how actions happen: the agent only proposes,
# and approval is a separate, authenticated step by an assigned responder (see Remediator.approve).
_ACTION_REQUEST = [
    r"^(please |now |just )?(roll ?back|restart|scale( out| up)?|revert|toggle|turn (on|off)|disable|enable|execute|run|apply|deploy|page)\b",
    r"\b(can|could|would|will) you (please |just )?(roll ?back|restart|scale|revert|toggle|turn (on|off)|disable|enable|execute|run|apply|deploy|page)\b",
    r"\b(go ahead|do it( now)?|i approve|approved?[.!]?$|you have (my )?(approval|permission))\b",
]
_CODE_REQUEST = [
    r"\b(write|give|send|show|generate|create) (me |us )?(the |some |a )?(code|script|snippet|function|class|patch|diff|regex|query)\b",
    r"\b(code|script|snippet|implementation) for\b",
    r"\b(implement|refactor|rewrite)\b",
    r"\bunit tests?\b",
    r"\breview (my|this|the) (pr|pull request|code)\b",
    r"```",
]

# Words that make a question about incident response. A question also counts as on-topic if it names
# something from this incident (service, dependency, deploy, version, flag, team, incident id).
_INCIDENT_VOCAB = re.compile(
    r"\b(incident|outage|degrad\w*|\w*(?:error|exception)\w*|5xx|latency|slow|timeout\w*|deploy\w*|release|rollback|roll back|"
    r"revert|metric\w*|log|logs|kpi|severity|sev-?\d|priority|p[1-5]|customer\w*|impact\w*|cause\w*|why|status|"
    r"what changed|fix\w*|mitigat\w*|dependenc\w*|runbook|timeline|hypothes\w*|on-?call|alert\w*|monitor\w*|"
    r"handover|verif\w*|approv\w*|propos\w*|traffic|saturation|cpu|memory|pool|scale|flag|recover\w*|"
    r"resolv\w*|escalat\w*|page|paging|eta|next step|blast radius|stack ?trace|crash\w*|health\w*)\b"
)

REPLIES = {
    "injection": "I can't change how I work from inside a conversation. I can help with {incident}: ask about "
    "its status, impact, likely cause, what changed, or the proposed next step.",
    "prompt_extraction": "I can't share my instructions or how I'm built. I can help with {incident}: ask about "
    "its status, impact, likely cause, what changed, or the proposed next step.",
    "secrets": "I don't handle credentials or secrets. If one may be exposed, rotate it and treat it as a "
    "security incident.",
    "security_exploit": "I won't help with that. If you suspect an attack, trigger the security incident "
    "process now.",
    "security_report": "That sounds like it may be a security matter. Security incidents follow their own "
    "response process, with a separate team and stricter communication rules. Please trigger the security "
    "incident process rather than discussing details here. I'll keep working {incident} as an operational incident.",
    "action_request": "I can't run actions, and a chat message isn't an approval. I propose actions; an "
    "assigned responder on {incident} approves each one with the Approve control, which checks who they are. "
    "Ask me what I'd propose and why.",
    "code_request": "Writing or reviewing code is out of scope for me here. I can help with {incident}: "
    "for example, which deploy introduced the error, or what the rollback would revert.",
    "off_topic": "I only answer questions about {incident}. Ask about its status, impact, likely cause, what "
    "changed, or the proposed next step.",
    "too_long": "That's too long for me to take as a question. Ask in a sentence or two about {incident}.",
}


@dataclass(frozen=True)
class Screen:
    allowed: bool
    category: str  # ok | injection | prompt_extraction | secrets | security_exploit | security_report | action_request | code_request | off_topic | too_long
    reply: str | None  # what to say instead of answering, when not allowed
    matched: str | None = None  # the rule that fired, for audit


def normalize(text: str) -> str:
    """Undo cheap obfuscation: compatibility forms (full-width letters), zero-width characters, case,
    and spacing. It won't undo rephrasing; that is what the later layers are for."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")  # zero-width and other format chars
    return re.sub(r"\s+", " ", text).strip().lower()


def incident_terms(state: Any) -> set[str]:
    """Names from this incident that make a question on-topic even without incident vocabulary."""
    terms = {state.incident_id.lower()}
    for p in state.proposed_actions:
        terms.update(str(v).lower() for v in p.params.values() if v)
    blob = " ".join(str(m.get("content") or "") for m in state.messages if m["role"] == "tool")
    terms.update(re.findall(r"\b(?:dep-\d+|v\d+\.\d+\.\d+|pd-\d+)\b", blob.lower()))
    terms.update(re.findall(r'"service(?:_id)?": "([a-z0-9-]+)"', blob.lower()))
    terms.update(re.findall(r'"(?:owning_team|team)": "([a-z0-9-]+)"', blob.lower()))
    return {t for t in terms if len(t) > 2}


def screen(question: str, state: Any) -> Screen:
    incident = state.incident_id
    text = normalize(question)

    def blocked(category: str, rule: str | None = None) -> Screen:
        return Screen(False, category, REPLIES[category].format(incident=incident), rule)

    if len(text) > MAX_QUESTION_CHARS:
        return blocked("too_long")
    for category, rules in (
        ("injection", _INJECTION),
        ("prompt_extraction", _PROMPT_EXTRACTION),
        ("secrets", _SECRETS),
        ("security_exploit", _SECURITY_EXPLOIT),
        ("security_report", _SECURITY_REPORT),
        ("action_request", _ACTION_REQUEST),
        ("code_request", _CODE_REQUEST),
    ):
        for rule in rules:
            if re.search(rule, text):
                return blocked(category, rule)
    if not _INCIDENT_VOCAB.search(text) and not any(t in text for t in incident_terms(state)):
        return blocked("off_topic")
    return Screen(True, "ok", None)
