"""Core domain objects. Plain dataclasses so everything round-trips to JSON."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: Any) -> datetime:
    """Accept ISO-8601 (with or without Z), epoch seconds, or a datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _encode(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


@dataclass
class Alert:
    """A normalized production alert, whatever monitoring system it came from."""

    fingerprint: str
    source: str                      # alertmanager | datadog | generic
    service: str
    severity: str                    # sev1 | sev2 | sev3
    title: str
    description: str = ""
    started_at: datetime = field(default_factory=utcnow)
    labels: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    runbook_hints: list[str] = field(default_factory=list)

    @property
    def summary_line(self) -> str:
        return f"[{self.severity.upper()}] {self.service}: {self.title}"

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(asdict(self), default=_encode))


@dataclass
class CommitCandidate:
    """A commit that landed inside the blast window, with its correlation score."""

    sha: str
    author: str
    authored_at: datetime
    subject: str
    body: str = ""
    files: list[str] = field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    services: list[str] = field(default_factory=list)
    score: float = 0.0
    signals: dict[str, float] = field(default_factory=dict)
    rationale: list[str] = field(default_factory=list)

    @property
    def short_sha(self) -> str:
        return self.sha[:10]

    @property
    def churn(self) -> int:
        return self.insertions + self.deletions

    def to_dict(self) -> dict[str, Any]:
        d = json.loads(json.dumps(asdict(self), default=_encode))
        d["short_sha"] = self.short_sha
        d["churn"] = self.churn
        return d


@dataclass
class RunbookChunk:
    chunk_id: str
    runbook_id: str
    title: str
    heading: str
    text: str
    path: str = ""
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ImpactEstimate:
    affected_users_low: int
    affected_users_point: int
    affected_users_high: int
    failed_requests: int
    duration_minutes: float
    error_budget_burn_pct: float
    blast_radius: list[str] = field(default_factory=list)
    method: str = ""
    confidence: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Diagnosis:
    """The agent's verdict."""

    root_cause: str
    offending_sha: str | None
    confidence: float
    reasoning: str
    evidence: list[str] = field(default_factory=list)
    ruled_out: list[str] = field(default_factory=list)
    runbook_ids: list[str] = field(default_factory=list)
    remediation: list[str] = field(default_factory=list)
    rollback_command: str = ""
    needs_human: bool = False
    source: str = "agent"            # agent | heuristic

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    result_preview: str
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IncidentResult:
    """Everything produced for one alert, ready to serialize into a report."""

    incident_id: str
    alert: Alert
    candidates: list[CommitCandidate]
    runbooks: list[RunbookChunk]
    diagnosis: Diagnosis
    impact: ImpactEstimate
    tool_calls: list[ToolCall] = field(default_factory=list)
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        if not self.finished_at:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "alert": self.alert.to_dict(),
            "candidates": [c.to_dict() for c in self.candidates],
            "runbooks": [r.to_dict() for r in self.runbooks],
            "diagnosis": self.diagnosis.to_dict(),
            "impact": self.impact.to_dict(),
            "tool_calls": [t.to_dict() for t in self.tool_calls],
            "started_at": _encode(self.started_at),
            "finished_at": _encode(self.finished_at) if self.finished_at else None,
            "duration_seconds": round(self.duration_seconds, 2),
            "usage": self.usage,
        }


def make_incident_id(alert: Alert) -> str:
    stamp = alert.started_at.astimezone(timezone.utc).strftime("%Y%m%d")
    digest = hashlib.sha1(f"{alert.fingerprint}{alert.started_at}".encode()).hexdigest()[:6]
    return f"INC-{stamp}-{digest}"
