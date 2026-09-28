"""Webhook receiver.

Alertmanager retries anything that does not return quickly, so the handler validates,
normalizes and acknowledges immediately, then runs diagnosis in a background task.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import load_config
from .ingest import normalize
from .models import Alert, make_incident_id
from .pipeline import IncidentPilot

log = logging.getLogger("incidentpilot.server")

_state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _state["config"] = config
    _state["pilot"] = IncidentPilot(config)
    log.info(
        "IncidentPilot ready · repo=%s · runbook chunks=%d · agent=%s · slack_post=%s",
        config.repo_path, _state["pilot"].index.size,
        "on" if config.has_anthropic_key else "off (heuristic only)", config.slack_post,
    )
    yield
    _state["pilot"].index.close()


app = FastAPI(title="IncidentPilot", version="0.1.0", lifespan=lifespan)


def _authorize(authorization: str | None) -> None:
    expected = _state["config"].webhook_token
    if not expected:
        return
    provided = (authorization or "").removeprefix("Bearer ").strip()
    if provided != expected:
        raise HTTPException(status_code=401, detail="invalid or missing webhook token")


def _process(alert: Alert) -> None:
    pilot: IncidentPilot = _state["pilot"]
    try:
        outcome = pilot.respond(alert)
        log.info("%s complete: %s", outcome["incident_id"], outcome.get("report_path"))
    except Exception:
        log.exception("incident response failed for %s", alert.summary_line)


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    pilot: IncidentPilot | None = _state.get("pilot")
    if pilot is None:
        raise HTTPException(status_code=503, detail="not ready")
    return "ok"


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    pilot: IncidentPilot = _state["pilot"]
    config = _state["config"]
    return {
        "status": "ready",
        "repo": str(config.repo_path),
        "runbook_chunks": pilot.index.size,
        "embedder": pilot.index.embedder.name,
        "services": sorted(pilot.topology.get("services", {})),
        "agent_enabled": config.has_anthropic_key,
        "model": config.model,
        "slack_posting": config.slack_post,
    }


@app.post("/alerts")
async def receive_alert(request: Request, background: BackgroundTasks,
                        authorization: str | None = Header(default=None)) -> JSONResponse:
    _authorize(authorization)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    alerts = normalize(payload)
    if not alerts:
        return JSONResponse({"accepted": 0, "note": "no firing alerts in payload"}, status_code=202)

    accepted = []
    for alert in alerts:
        incident_id = make_incident_id(alert)
        accepted.append({"incident_id": incident_id, "service": alert.service,
                         "severity": alert.severity, "title": alert.title})
        background.add_task(_process, alert)

    log.info("accepted %d alert(s) from %s", len(alerts), alerts[0].source)
    return JSONResponse({"accepted": len(accepted), "incidents": accepted}, status_code=202)


@app.get("/incidents/{incident_id}", response_class=PlainTextResponse)
async def get_report(incident_id: str) -> str:
    out_dir = Path(_state["config"].out_dir)
    path = out_dir / f"{incident_id}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="no report for that incident (yet)")
    return path.read_text(encoding="utf-8")
