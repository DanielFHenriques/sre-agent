"""Cross-incident memory: what is worth remembering, and how to recall the right slice without noise.

What is remembered (one record per finished incident), all taken from code-verified facts:
  - the error signature (top error log templates, normalized so versions and line numbers don't matter)
  - which metrics moved and which way
  - fixes that *verified* as working, and fixes that ran and did *not* work
  - causes that were ruled out, with the reason
  - the root cause, only if a fix verified (otherwise "unconfirmed")
  - severity
What is not remembered: model prose, engineers' questions, raw transcripts. Memory is written by
deterministic distillation from the incident state, so neither the model nor a user can plant a
"fact" in it. Humans can retract a record that turns out to be wrong.

Recall:
  - tenant-scoped (one store per tenant; the tenant comes from the executor, never the model)
  - same service only, or a service it depends on
  - similarity: mostly the error signature, partly the shape of which metrics moved
  - recency decay, and a hard threshold: weak matches return nothing rather than noise
  - top 3, as compact slices (a few hundred characters each), not full records
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sre_agent.models import AgentResult, IncidentRef
from sre_agent.timeline import describe

MIN_SIMILARITY = 0.35
HALF_LIFE_DAYS = 90
TOP_K = 3
MAX_PER_SERVICE = 200  # oldest records beyond this are dropped

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_STOP = {"the", "and", "for", "from", "after", "with", "at", "in", "of", "to", "version"}


@dataclass
class MemoryRecord:
    incident_id: str
    service_id: str
    resolved_at: str
    severity: str | None
    error_signatures: list[str]
    changed_metrics: list[str]  # "error_rate:up", "business_kpi:down"
    worked: list[dict[str, str]]  # {"action": ..., "detail": ...}
    failed: list[dict[str, str]]
    ruled_out: list[dict[str, str]]  # {"cause": ..., "why": ...}
    root_cause: str | None
    retracted: dict[str, str] | None = None
    tags: list[str] = field(default_factory=list)


def normalize_signature(template: str) -> str:
    """'NPE at Mapper.map(Mapper.java:88) version=v2.14.0' -> 'npe at mapper.map(mapper.java:<n>) version=<v>'."""
    text = template.lower()
    text = re.sub(r"\bv?\d+\.\d+(\.\d+)?\b", "<v>", text)
    text = re.sub(r"\b(dep|pd|inc)-\d+\b", "<id>", text)
    return re.sub(r"\d+", "<n>", text)


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z][a-z_]{2,}", normalize_signature(text)) if t not in _STOP}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _tool_results(state: AgentResult) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    out = []
    for m in state.messages:
        for c in m.get("tool_calls") or []:
            try:
                calls[c["id"]] = (c["function"]["name"], json.loads(c["function"]["arguments"]))
            except json.JSONDecodeError:
                continue
        if m["role"] == "tool" and m["tool_call_id"] in calls:
            name, args = calls[m["tool_call_id"]]
            try:
                out.append((name, args, json.loads(m["content"])))
            except json.JSONDecodeError:
                continue
    return out


def changed_metrics(metric_summaries: dict[str, dict[str, Any]]) -> list[str]:
    moved = []
    for name, m in sorted(metric_summaries.items()):
        baseline, current = m.get("baseline"), m.get("current")
        if not baseline or current is None:
            continue
        ratio = current / baseline
        if ratio >= 1.5:
            moved.append(f"{name}:up")
        elif ratio <= 0.67:
            moved.append(f"{name}:down")
    return moved


def distill(state: AgentResult, incident: IncidentRef, resolved_at: str) -> MemoryRecord:
    """Deterministic: built from tool results, verification verdicts and the final assessment's structure."""
    logs: dict[str, Any] = {}
    metrics: dict[str, dict[str, Any]] = {}
    for name, args, result in _tool_results(state):
        if name == "logs_search" and "patterns" in result:
            logs = result
        if name == "metrics_query" and "baseline" in result:
            metrics.setdefault(args["metric"], result)  # the incident as first seen, before any fix

    patterns = sorted(logs.get("patterns", []), key=lambda p: -p.get("count", 0))[:2]
    worked = [{"action": p.action, "detail": describe(p)} for p in state.proposed_actions if p.verification_verdict == "worked"]
    failed = [{"action": p.action, "detail": describe(p)} for p in state.proposed_actions if p.verification_verdict == "did_not_work"]
    hypotheses = (state.assessment or {}).get("hypotheses") or []
    ruled_out = [
        {"cause": h["cause"], "why": h["evidence_against"][0]}
        for h in hypotheses[1:] if h["confidence"] == "low" and h["evidence_against"]
    ]
    return MemoryRecord(
        incident_id=incident.incident_id,
        service_id=incident.service_id,
        resolved_at=resolved_at,
        severity=(state.triage or {}).get("severity"),
        error_signatures=[normalize_signature(p["template"]) for p in patterns],
        changed_metrics=changed_metrics(metrics),
        worked=worked,
        failed=failed,
        ruled_out=ruled_out,
        root_cause=hypotheses[0]["cause"] if worked and hypotheses else None,
    )


class FileMemoryStore:
    """One JSON file per tenant. Stands in for a tenant-partitioned table or index."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, tenant_id: str) -> Path:
        if not _SAFE_ID.match(tenant_id):
            raise ValueError(f"unsafe tenant id: {tenant_id!r}")
        return self.root / tenant_id / "memory.json"

    def _load(self, tenant_id: str) -> list[MemoryRecord]:
        path = self._path(tenant_id)
        if not path.exists():
            return []
        return [MemoryRecord(**r) for r in json.loads(path.read_text(encoding="utf-8"))]

    def _save(self, tenant_id: str, records: list[MemoryRecord]) -> None:
        path = self._path(tenant_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps([asdict(r) for r in records], indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def remember(self, tenant_id: str, record: MemoryRecord) -> None:
        records = [r for r in self._load(tenant_id) if r.incident_id != record.incident_id]  # re-distill replaces
        records.append(record)
        same = sorted((r for r in records if r.service_id == record.service_id), key=lambda r: r.resolved_at)
        drop = {r.incident_id for r in same[:-MAX_PER_SERVICE]}
        self._save(tenant_id, [r for r in records if r.incident_id not in drop])

    def retract(self, tenant_id: str, incident_id: str, by: str, reason: str) -> bool:
        """A human says this memory is wrong. It stays for audit but is never recalled again."""
        records = self._load(tenant_id)
        for r in records:
            if r.incident_id == incident_id:
                r.retracted = {"by": by, "reason": reason}
                self._save(tenant_id, records)
                return True
        return False

    def recall(
        self, tenant_id: str, service_ids: set[str], error_signature: str, changed: list[str], now: str
    ) -> list[dict[str, Any]]:
        query_sig, query_shape = tokens(error_signature), set(changed)
        now_ts = _ts(now)
        scored = []
        for r in self._load(tenant_id):
            if r.retracted or r.service_id not in service_ids:
                continue
            sig = max((_jaccard(query_sig, tokens(s)) for s in r.error_signatures), default=0.0)
            shape = _jaccard(query_shape, set(r.changed_metrics))
            age_days = max((now_ts - _ts(r.resolved_at)).total_seconds() / 86400, 0)
            score = (0.7 * sig + 0.3 * shape) * 0.5 ** (age_days / HALF_LIFE_DAYS)
            if score >= MIN_SIMILARITY:
                scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        return [_slice(r, score) for score, r in scored[:TOP_K]]

    # The executor routes the memory_recall tool here (a first-party "server").
    def call_tool(self, tool: str, arguments: dict[str, Any], *, tenant_id: str, now: str, related: set[str]) -> dict[str, Any]:
        changed = [f"{c['metric']}:{c['direction']}" for c in arguments["changed_metrics"]]
        memories = self.recall(tenant_id, {arguments["service_id"], *related}, arguments["error_signature"], changed, now)
        return {"memories": memories, "note": "Only matches above the similarity threshold are returned."}


def _slice(r: MemoryRecord, score: float) -> dict[str, Any]:
    return {
        "incident_id": r.incident_id,
        "service_id": r.service_id,
        "resolved_at": r.resolved_at,
        "similarity": round(score, 2),
        "severity": r.severity,
        "root_cause": r.root_cause or "unconfirmed",
        "worked": [w["detail"] for w in r.worked],
        "did_not_work": [f["detail"] for f in r.failed],
        "ruled_out": [f"{x['cause']}: {x['why']}" for x in r.ruled_out],
        # For fast-track: actions that verified, by our own code, on this past incident.
        "verified_actions": sorted({w["action"] for w in r.worked}),
    }


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
