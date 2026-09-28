"""End-to-end incident response: alert in, diagnosis + Slack brief + report out."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import report as report_mod
from . import slack as slack_mod
from .agent import AgentUnavailable, DiagnosisAgent, ToolContext, heuristic_diagnosis
from .config import Config, load_config
from .gitctx import GitRepo, collect_candidates
from .impact import dependents_of, estimate_impact, load_topology
from .ingest import alert_keywords
from .models import Alert, IncidentResult, make_incident_id, utcnow
from .rag import RunbookIndex

log = logging.getLogger("incidentpilot.pipeline")


class IncidentPilot:
    """Holds the expensive, reusable pieces (git handle, runbook index, topology)."""

    def __init__(self, config: Config | None = None, repo: GitRepo | None = None,
                 index: RunbookIndex | None = None):
        self.config = config or load_config()
        self.repo = repo or GitRepo(self.config.repo_path)
        self.topology = load_topology(str(self.config.topology_path))
        self.index = index or RunbookIndex(self.config.index_path)
        if self.index.size == 0:
            count = self.index.build(self.config.runbook_dir)
            log.info("indexed %d runbook chunks from %s", count, self.config.runbook_dir)
        self._agent: DiagnosisAgent | None = None

    # ------------------------------------------------------------------ helpers

    def _get_agent(self) -> DiagnosisAgent | None:
        if self._agent is not None:
            return self._agent
        if not self.config.has_anthropic_key:
            return None
        try:
            self._agent = DiagnosisAgent(
                model=self.config.model, effort=self.config.effort,
                max_tokens=self.config.max_tokens, max_turns=self.config.max_agent_turns,
            )
        except AgentUnavailable as exc:
            log.warning("agent unavailable, falling back to heuristic: %s", exc)
            return None
        return self._agent

    def _topology_summary(self, service: str) -> dict[str, Any]:
        spec = self.topology.get("services", {}).get(service, {})
        return {
            "depends_on": spec.get("depends_on", []),
            "called_by": dependents_of(service, self.topology),
            "owner": spec.get("owner", "unknown"),
            "tier": spec.get("tier", "?"),
        }

    def _runbook_query(self, alert: Alert) -> str:
        parts = [alert.service, alert.title, alert.description]
        parts.extend(f"{k} {v}" for k, v in alert.labels.items() if k != "service")
        return " ".join(p for p in parts if p)[:600]

    # --------------------------------------------------------------------- main

    def handle(self, alert: Alert, use_agent: bool = True) -> IncidentResult:
        started = utcnow()
        incident_id = make_incident_id(alert)
        log.info("%s: %s", incident_id, alert.summary_line)

        keywords = alert_keywords(alert)
        candidates = collect_candidates(
            self.repo, alert, self.topology, keywords,
            self.config.lookback_hours, self.config.max_candidates,
        )
        runbooks = self.index.search(self._runbook_query(alert), top_k=self.config.top_k)
        impact = estimate_impact(alert, self.topology)

        tool_calls: list = []
        usage: dict[str, int] = {}
        agent = self._get_agent() if use_agent else None

        if agent is None:
            diagnosis = heuristic_diagnosis(alert, candidates, runbooks)
        else:
            ctx = ToolContext(
                alert=alert, repo=self.repo, index=self.index, topology=self.topology,
                candidates=candidates, lookback_hours=self.config.lookback_hours,
                keywords=keywords,
            )
            try:
                diagnosis, tool_calls, usage = agent.run(
                    ctx, runbooks, impact.to_dict(), self._topology_summary(alert.service)
                )
            except Exception as exc:
                log.exception("agent failed; falling back to heuristic")
                diagnosis = heuristic_diagnosis(alert, candidates, runbooks)
                diagnosis.needs_human = True
                diagnosis.reasoning = f"Agent error ({type(exc).__name__}: {exc}). " + diagnosis.reasoning

        return IncidentResult(
            incident_id=incident_id, alert=alert, candidates=candidates, runbooks=runbooks,
            diagnosis=diagnosis, impact=impact, tool_calls=tool_calls,
            started_at=started, finished_at=utcnow(), usage=usage,
        )

    def publish(self, result: IncidentResult, repo_url: str = "") -> dict[str, Any]:
        """Write the report + Slack payload, and post the brief when enabled."""
        out_dir = Path(self.config.out_dir)
        report_path = report_mod.write_report(result, out_dir, repo_url)
        json_path = report_mod.write_json(result, out_dir)
        slack_outcome = slack_mod.deliver(
            result, out_dir, self.config.slack_channel, self.config.slack_post, repo_url
        )
        return {
            "incident_id": result.incident_id,
            "report_path": str(report_path),
            "json_path": str(json_path),
            **slack_outcome,
        }

    def respond(self, alert: Alert, use_agent: bool = True, repo_url: str = "") -> dict[str, Any]:
        result = self.handle(alert, use_agent=use_agent)
        outcome = self.publish(result, repo_url)
        outcome["diagnosis"] = result.diagnosis.to_dict()
        outcome["impact"] = result.impact.to_dict()
        return outcome
