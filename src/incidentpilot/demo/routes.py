"""HTTP surface for the demo page: /demo, its static files, and a small JSON + SSE API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..config import load_config
from . import scenarios as scenario_mod
from .runs import RunManager, load_recording
from .workspace import DEMO_REPO, repo_available

STATIC = Path(__file__).resolve().parent / "static"

router = APIRouter()
manager = RunManager()


class RunRequest(BaseModel):
    scenario: str
    mode: str = "replay"
    speed: float = 1.0


def _demo_repo() -> Path | None:
    return DEMO_REPO if (DEMO_REPO / ".git").exists() else None


@router.get("/demo", include_in_schema=False)
def demo_page() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@router.get("/api/demo/scenarios")
def list_scenarios() -> dict[str, Any]:
    config = load_config()
    items = []
    for scenario in scenario_mod.load(_demo_repo()):
        recording = load_recording(scenario.id)
        items.append({
            **scenario.to_dict(),
            "recorded_at": recording.get("recorded_at") if recording else None,
            "recorded_model": recording.get("model") if recording else None,
            "live_ready": repo_available(scenario.repo),
        })
    return {"scenarios": items, "live_enabled": config.has_anthropic_key,
            "model": config.model}


@router.post("/api/demo/runs")
def start_run(request: RunRequest) -> dict[str, Any]:
    if request.mode not in ("replay", "live"):
        raise HTTPException(status_code=400, detail="mode must be replay or live")
    scenario = scenario_mod.get(request.scenario, _demo_repo())
    if scenario is None:
        raise HTTPException(status_code=404, detail="unknown scenario")
    # 0 means instant: skip the recorded pauses and go straight to the result.
    speed = 0.0 if request.speed <= 0 else min(max(request.speed, 0.25), 8.0)
    run = manager.start(scenario, request.mode, speed)
    return {"run_id": run.id}


@router.get("/api/demo/runs/{run_id}/events")
def run_events(run_id: str) -> StreamingResponse:
    run = manager.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    return StreamingResponse(run.stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
