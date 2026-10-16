"""Persisting incident state so an investigation survives a restart, a deploy, or a handover.

One JSON document per incident: transcript, proposals and their outcomes, call log, timeline, cycle.
A file store stands in for what would be a database row (or document) keyed by (tenant, incident).
The properties that matter carry over to a real store:

- Tenant-scoped keys. A tenant id is part of the key, and ids are validated, so one tenant's
  incident can never be read or written through another's.
- Atomic writes. Write to a temp file, then rename over the old one, so a crash mid-write leaves
  the previous checkpoint intact instead of a half-written file.
- Optimistic concurrency. Every save says which version it started from. If someone else (a second
  agent process, a responder acting from another surface) saved in between, the save fails with
  `ConflictError` instead of silently overwriting their changes. (Here the check and the write are
  two steps; a real store does them as one conditional write.)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sre_agent.models import AgentResult, IncidentRef, ProposedAction, TimelineEntry, ToolCallRecord

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ConflictError(Exception):
    pass


def to_dict(result: AgentResult) -> dict[str, Any]:
    return asdict(result)


def from_dict(data: dict[str, Any]) -> AgentResult:
    return AgentResult(
        **{
            **data,
            "proposed_actions": [ProposedAction(**p) for p in data["proposed_actions"]],
            "tool_log": [ToolCallRecord(**r) for r in data["tool_log"]],
            "timeline": [TimelineEntry(**t) for t in data["timeline"]],
        }
    )


class FileStateStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, incident: IncidentRef) -> Path:
        for value in (incident.tenant_id, incident.incident_id):
            if not _SAFE_ID.match(value):
                raise ValueError(f"unsafe id for a storage key: {value!r}")
        return self.root / incident.tenant_id / f"{incident.incident_id}.json"

    def load(self, incident: IncidentRef) -> AgentResult | None:
        path = self._path(incident)
        if not path.exists():
            return None
        return from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, incident: IncidentRef, result: AgentResult) -> None:
        """Compare-and-swap on `version`. On success, `result.version` is the new version."""
        path = self._path(incident)
        current = self.load(incident)
        on_disk = current.version if current else 0
        if on_disk != result.version:
            raise ConflictError(
                f"{incident.incident_id}: saved version is {on_disk}, this writer started from {result.version}. "
                "Reload, merge, and retry."
            )
        data = to_dict(result)
        data["version"] = result.version + 1
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)  # atomic on the same filesystem
        result.version += 1
