"""Replay the 20 fixture incidents and score root-cause accuracy.

Two arms:

  heuristic -- the correlation ranker alone, no model calls. Free, deterministic, and
               the control the agent has to beat.
  agent     -- the full Claude tool-use loop on top of the same ranker.

Scored metrics:

  top1      the verdict names the ground-truth commit
  top3      ground truth is in the ranker's top 3 candidates (retrieval ceiling)
  recall    ground truth appears anywhere in the candidate list
  MRR       mean reciprocal rank of ground truth in the candidate list

top3 and recall are properties of the retrieval stage and are identical in both arms;
they bound what the agent can possibly get right. The headline number is top1.

    py -3.13 eval/run_eval.py --mode heuristic
    py -3.13 eval/run_eval.py --mode both --limit 5
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Point the whole system at the fixture corpus before anything reads config.
os.environ.setdefault("INCIDENTPILOT_REPO", str(ROOT / "eval" / "fixtures" / "repo"))
os.environ.setdefault("INCIDENTPILOT_INDEX", str(ROOT / "data" / "runbooks.db"))
os.environ.setdefault("INCIDENTPILOT_OUT", str(ROOT / "eval" / "results" / "reports"))

from incidentpilot.config import load_config  # noqa: E402
from incidentpilot.ingest import normalize  # noqa: E402
from incidentpilot.pipeline import IncidentPilot  # noqa: E402

INCIDENTS_JSON = ROOT / "eval" / "incidents.json"
RESULTS_DIR = ROOT / "eval" / "results"

# Claude Opus 5 list price, USD per million tokens.
PRICE_IN, PRICE_OUT, PRICE_CACHE_READ = 5.00, 25.00, 0.50


def rank_of(sha: str, candidates: list) -> int | None:
    for i, candidate in enumerate(candidates, 1):
        if candidate.sha == sha:
            return i
    return None


def score_one(record: dict, pilot: IncidentPilot, use_agent: bool) -> dict:
    alert = normalize(record["alert"])[0]
    truth = record["ground_truth_sha"]

    started = time.perf_counter()
    result = pilot.handle(alert, use_agent=use_agent)
    elapsed = time.perf_counter() - started

    rank = rank_of(truth, result.candidates)
    verdict_sha = result.diagnosis.offending_sha or ""
    correct = bool(verdict_sha) and truth.startswith(verdict_sha[:10])

    return {
        "incident_id": record["incident_id"],
        "service": alert.service,
        "truth_sha": truth[:10],
        "truth_subject": record["ground_truth_subject"],
        "verdict_sha": verdict_sha[:10] if verdict_sha else None,
        "correct": correct,
        "truth_rank": rank,
        "candidates": len(result.candidates),
        "confidence": result.diagnosis.confidence,
        "needs_human": result.diagnosis.needs_human,
        "source": result.diagnosis.source,
        "tool_calls": len(result.tool_calls),
        "tools_used": [c.name for c in result.tool_calls],
        "runbooks": [rb.runbook_id for rb in result.runbooks],
        "runbooks_cited": result.diagnosis.runbook_ids,
        "seconds": round(elapsed, 2),
        "usage": result.usage,
        "root_cause": result.diagnosis.root_cause,
    }


def aggregate(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {}
    top1 = sum(1 for r in rows if r["correct"])
    top3 = sum(1 for r in rows if r["truth_rank"] and r["truth_rank"] <= 3)
    recall = sum(1 for r in rows if r["truth_rank"])
    mrr = sum(1 / r["truth_rank"] for r in rows if r["truth_rank"]) / n

    usage: dict[str, int] = {}
    for row in rows:
        for key, value in (row["usage"] or {}).items():
            usage[key] = usage.get(key, 0) + value
    cost = (
        usage.get("input_tokens", 0) / 1e6 * PRICE_IN
        + usage.get("cache_creation_input_tokens", 0) / 1e6 * PRICE_IN * 1.25
        + usage.get("cache_read_input_tokens", 0) / 1e6 * PRICE_CACHE_READ
        + usage.get("output_tokens", 0) / 1e6 * PRICE_OUT
    )

    return {
        "n": n,
        "top1": top1,
        "top1_pct": round(top1 / n * 100, 1),
        "top3_retrieval": top3,
        "top3_pct": round(top3 / n * 100, 1),
        "recall": recall,
        "recall_pct": round(recall / n * 100, 1),
        "mrr": round(mrr, 3),
        "needs_human": sum(1 for r in rows if r["needs_human"]),
        "mean_confidence": round(sum(r["confidence"] for r in rows) / n, 2),
        "mean_tool_calls": round(sum(r["tool_calls"] for r in rows) / n, 1),
        "mean_seconds": round(sum(r["seconds"] for r in rows) / n, 1),
        "total_usage": usage,
        "estimated_cost_usd": round(cost, 4),
    }


def calibration(rows: list[dict]) -> dict:
    """Is the stated confidence worth anything? Accuracy above vs below 0.7."""
    high = [r for r in rows if r["confidence"] >= 0.7]
    low = [r for r in rows if r["confidence"] < 0.7]

    def acc(group: list[dict]) -> float | None:
        return round(sum(1 for r in group if r["correct"]) / len(group) * 100, 1) if group else None

    return {
        "high_confidence_n": len(high), "high_confidence_accuracy_pct": acc(high),
        "low_confidence_n": len(low), "low_confidence_accuracy_pct": acc(low),
        "flagged_for_human_n": sum(1 for r in rows if r["needs_human"]),
        "flagged_and_wrong": sum(1 for r in rows if r["needs_human"] and not r["correct"]),
        "unflagged_and_wrong": sum(1 for r in rows if not r["needs_human"] and not r["correct"]),
    }


def render_markdown(report: dict) -> str:
    lines = [f"# IncidentPilot eval - {report['run_at']}", ""]
    lines.append(f"Corpus: {report['n_incidents']} replayed incidents, "
                 f"{report['repo_commits']} commits, model `{report['model']}`")
    lines.append("")
    lines.append("| Arm | top-1 | top-3 (retrieval) | recall | MRR | mean conf | tool calls | s/incident | cost |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for arm, summary in report["arms"].items():
        if not summary:
            continue
        lines.append(
            f"| {arm} | {summary['top1']}/{summary['n']} ({summary['top1_pct']}%) "
            f"| {summary['top3_retrieval']}/{summary['n']} ({summary['top3_pct']}%) "
            f"| {summary['recall']}/{summary['n']} | {summary['mrr']} "
            f"| {summary['mean_confidence']} | {summary['mean_tool_calls']} "
            f"| {summary['mean_seconds']} | ${summary['estimated_cost_usd']} |"
        )
    lines.append("")

    for arm, rows in report["rows"].items():
        if not rows:
            continue
        lines.append(f"## {arm} - per incident")
        lines.append("")
        lines.append("| Incident | Service | Truth | Verdict | Hit | Truth rank | Conf | Tools |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in rows:
            hit = "yes" if r["correct"] else "no"
            rank = r["truth_rank"] if r["truth_rank"] else "-"
            flag = " (flagged)" if r["needs_human"] else ""
            lines.append(
                f"| {r['incident_id']} | {r['service']} | `{r['truth_sha']}` | "
                f"`{r['verdict_sha'] or '-'}` | {hit}{flag} | {rank} | {r['confidence']:.2f} | "
                f"{r['tool_calls']} |"
            )
        lines.append("")
        cal = report["calibration"].get(arm)
        if cal:
            lines.append(f"Calibration: {cal['high_confidence_n']} verdicts at confidence >=0.70 "
                         f"were {cal['high_confidence_accuracy_pct']}% correct; "
                         f"{cal['low_confidence_n']} below 0.70 were "
                         f"{cal['low_confidence_accuracy_pct']}% correct. "
                         f"{cal['unflagged_and_wrong']} wrong verdict(s) went out unflagged.")
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["heuristic", "agent", "both"], default="heuristic")
    parser.add_argument("--limit", type=int, help="only run the first N incidents")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if not INCIDENTS_JSON.exists():
        print("no incidents.json - run `py -3.13 eval/build_fixture_repo.py` first", file=sys.stderr)
        return 1

    records = json.loads(INCIDENTS_JSON.read_text(encoding="utf-8"))
    if args.limit:
        records = records[: args.limit]

    config = load_config()
    arms = ["heuristic"] if args.mode == "heuristic" else (
        ["agent"] if args.mode == "agent" else ["heuristic", "agent"]
    )
    if "agent" in arms and not config.has_anthropic_key:
        print("ANTHROPIC_API_KEY is not set - the agent arm cannot run.", file=sys.stderr)
        if args.mode == "agent":
            return 1
        arms = ["heuristic"]

    pilot = IncidentPilot(config)

    rows: dict[str, list[dict]] = {}
    for arm in arms:
        rows[arm] = []
        for record in records:
            row = score_one(record, pilot, use_agent=(arm == "agent"))
            rows[arm].append(row)
            if not args.quiet:
                mark = "HIT " if row["correct"] else "miss"
                print(f"[{arm}] {row['incident_id']} {mark} "
                      f"truth={row['truth_sha']} verdict={row['verdict_sha'] or '-'} "
                      f"rank={row['truth_rank']} conf={row['confidence']:.2f} "
                      f"tools={row['tool_calls']} {row['seconds']}s")

    commits = subprocess.run(
        ["git", "-C", str(config.repo_path), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()

    report = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_incidents": len(records),
        "repo_commits": commits,
        "model": config.model if "agent" in arms else "n/a (heuristic only)",
        "effort": config.effort,
        "embedder": pilot.index.embedder.name,
        "runbook_chunks": pilot.index.size,
        "arms": {arm: aggregate(rows[arm]) for arm in arms},
        "calibration": {arm: calibration(rows[arm]) for arm in arms},
        "rows": rows,
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (RESULTS_DIR / f"eval-{stamp}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary_path = RESULTS_DIR / f"eval-{stamp}.md"
    summary_path.write_text(render_markdown(report), encoding="utf-8")
    (RESULTS_DIR / "latest.md").write_text(render_markdown(report), encoding="utf-8")

    print()
    for arm in arms:
        s = report["arms"][arm]
        print(f"{arm:10s} top-1 {s['top1']}/{s['n']} ({s['top1_pct']}%)  "
              f"top-3 {s['top3_retrieval']}/{s['n']}  recall {s['recall']}/{s['n']}  "
              f"MRR {s['mrr']}  cost ${s['estimated_cost_usd']}")
    print(f"\nwrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
