"""Demo runs: live investigations and replays of recorded ones, as event streams.

A live run calls Claude and streams each step as it happens. A replay plays back a
recording of an earlier live run with its original timing, so a demo can run with no API
key, no network and no cost. Both produce the same event sequence, and the page labels
which one it is showing.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..ingest import normalize
from ..pricing import cost_usd
from ..report import render_markdown
from ..slack import build_payload
from .scenarios import Scenario
from .workspace import Pilots, repo_available

log = logging.getLogger("incidentpilot.demo")

RECORDINGS = Path(__file__).resolve().parent / "recordings"
RECORDING_SCHEMA = 1


def recording_path(scenario_id: str) -> Path:
    return RECORDINGS / f"{scenario_id}.json"


def load_recording(scenario_id: str) -> dict | None:
    path = recording_path(scenario_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def answer_event(scenario: Scenario, verdict_sha: str | None) -> dict[str, Any]:
    """The known answer, computed from the scenario at play time -- never from what a
    recording claims -- so a corrected label is what the audience sees."""
    if scenario.suite == "negative":
        matched = verdict_sha is None
    elif scenario.answer_sha:
        matched = bool(verdict_sha) and scenario.answer_sha.startswith(verdict_sha[:10])
    else:
        matched = None
    return {"type": "answer", "suite": scenario.suite, "source": scenario.source,
            "sha": scenario.answer_sha, "subject": scenario.answer_subject, "matched": matched}


class Run:
    def __init__(self, scenario: Scenario, mode: str, speed: float = 1.0):
        self.id = uuid.uuid4().hex[:12]
        self.scenario = scenario
        self.mode = mode
        self.speed = speed
        self.events: list[dict[str, Any]] = []
        self.finished = False
        self._cond = threading.Condition()
        self._t0 = time.perf_counter()

    def push(self, event: dict[str, Any], t: float | None = None) -> None:
        with self._cond:
            stamped = {**event, "seq": len(self.events),
                       "t": round(time.perf_counter() - self._t0 if t is None else t, 3)}
            self.events.append(stamped)
            if event.get("type") in ("end", "error"):
                self.finished = True
            self._cond.notify_all()

    def stream(self, heartbeat: float = 15.0) -> Iterator[str]:
        """Server-sent events: every event so far, then each new one as it lands."""
        sent = 0
        while True:
            with self._cond:
                if sent >= len(self.events) and not self.finished:
                    self._cond.wait(timeout=heartbeat)
                batch = self.events[sent:]
                done = self.finished and sent + len(batch) >= len(self.events)
            for event in batch:
                yield f"data: {json.dumps(event)}\n\n"
            sent += len(batch)
            if not batch and not done:
                yield ": keep-alive\n\n"
            if done:
                return


class RunManager:
    def __init__(self, pilots: Pilots | None = None):
        self.pilots = pilots or Pilots()
        self.runs: dict[str, Run] = {}
        self._live_lock = threading.Lock()  # one live run at a time: it spends money

    # ------------------------------------------------------------------ starting

    def start(self, scenario: Scenario, mode: str, speed: float = 1.0) -> Run:
        run = Run(scenario, mode, speed)
        self.runs[run.id] = run
        target = self._replay if mode == "replay" else self._live
        threading.Thread(target=target, args=(run,), daemon=True, name=f"demo-{run.id}").start()
        return run

    def _start_event(self, run: Run, **extra: Any) -> None:
        run.push({"type": "start", "mode": run.mode, "speed": run.speed,
                  "scenario": run.scenario.to_dict(), **extra})

    # --------------------------------------------------------------------- live

    def _live(self, run: Run) -> None:
        if not self._live_lock.acquire(blocking=False):
            run.push({"type": "error", "message": "Another live run is in progress."})
            return
        try:
            self.execute_live(run)
        finally:
            self._live_lock.release()

    def execute_live(self, run: Run) -> None:
        scenario = run.scenario
        self._start_event(run)
        if not repo_available(scenario.repo):
            run.push({"type": "error", "message":
                      f"The {scenario.repo} repository is not on this machine, so this incident "
                      f"can only be replayed."})
            return
        pilot = self.pilots.get(scenario.repo)
        if not pilot.config.has_anthropic_key:
            run.push({"type": "error", "message":
                      "No ANTHROPIC_API_KEY in this terminal, so live runs are off. "
                      "Replays still work."})
            return
        try:
            alert = normalize(scenario.alert)[0]
            result = pilot.handle(alert, use_agent=True, on_event=run.push)
        except Exception as exc:  # surface it on the page rather than hanging the stream
            log.exception("live demo run failed")
            run.push({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            return
        run.push({
            "type": "done",
            "model": pilot.config.model,
            "duration_s": round(result.duration_seconds, 1),
            "tool_calls": len(result.tool_calls),
            "usage": result.usage,
            "cost_usd": cost_usd(pilot.config.model, result.usage),
            "source": result.diagnosis.source,
            "slack": build_payload(result, pilot.config.slack_channel)["blocks"],
            "report_md": render_markdown(result),
        })
        run.push(answer_event(scenario, result.diagnosis.offending_sha))
        run.push({"type": "end"})

    # ------------------------------------------------------------------- replay

    def _replay(self, run: Run) -> None:
        recording = load_recording(run.scenario.id)
        if recording is None:
            self._start_event(run)
            run.push({"type": "error", "message":
                      "No recording for this incident yet. Record one with "
                      "`incidentpilot demo-record`, or run it live."})
            return
        self._start_event(run, recorded_at=recording.get("recorded_at"),
                          model=recording.get("model"))
        speed = run.speed  # 0 plays every event at once
        started = time.perf_counter()
        verdict_sha = None
        for event in recording["events"]:
            if event["type"] in ("start", "answer", "end"):
                continue
            if speed > 0:
                wait = event["t"] / speed - (time.perf_counter() - started)
                if wait > 0:
                    time.sleep(wait)
            if event["type"] == "verdict":
                verdict_sha = event["diagnosis"].get("offending_sha")
            body = {k: v for k, v in event.items() if k not in ("seq", "t")}
            run.push(body, t=event["t"])  # keep the recorded clock on screen
        run.push(answer_event(run.scenario, verdict_sha), t=run.events[-1]["t"])
        run.push({"type": "end"}, t=run.events[-1]["t"])

    # --------------------------------------------------------------- recording

    def record(self, scenario: Scenario) -> tuple[Path, dict]:
        """Run live, synchronously, and save it as this scenario's recording."""
        run = Run(scenario, "live")
        self.execute_live(run)
        if any(e["type"] == "error" for e in run.events):
            message = next(e["message"] for e in run.events if e["type"] == "error")
            raise RuntimeError(message)
        done = next(e for e in run.events if e["type"] == "done")
        RECORDINGS.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": RECORDING_SCHEMA,
            "scenario": scenario.id,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": done["model"],
            "events": run.events,
        }
        path = recording_path(scenario.id)
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        return path, done
