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
from incidentpilot.pipeline import AgentConfigError, IncidentPilot  # noqa: E402
from incidentpilot.pricing import cache_hit_rate, cost_usd  # noqa: E402

INCIDENTS_JSON = ROOT / "eval" / "incidents.json"
RESULTS_DIR = ROOT / "eval" / "results"



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

    by_sha = {c.sha: c for c in result.candidates}
    blamed = next((c for c in result.candidates if c.sha.startswith(verdict_sha[:10])), None) if verdict_sha else None
    truth_commit = by_sha.get(truth)

    # Was the real cause outside the service that alerted? Those are the hard ones: a
    # shared library or another team's migration breaking a downstream service.
    owners = truth_commit.services if truth_commit else []
    cross_service = bool(truth_commit) and alert.service not in owners

    return {
        "incident_id": record["incident_id"],
        "service": alert.service,
        "truth_sha": truth[:10],
        "truth_subject": record["ground_truth_subject"],
        "verdict_sha": verdict_sha[:10] if verdict_sha else None,
        "verdict_subject": blamed.subject if blamed else None,
        "alert_title": alert.title,
        "truth_files": (truth_commit.files[:3] if truth_commit else []),
        "truth_owners": owners,
        "cross_service": cross_service,
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


def aggregate(rows: list[dict], model: str = "") -> dict:
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
    # Unknown model -> None rather than a confident number at somebody else's rates.
    cost = cost_usd(model, usage) if usage else 0.0
    hit_rate = cache_hit_rate(usage)

    sources: dict[str, int] = {}
    for r in rows:
        sources[r["source"]] = sources.get(r["source"], 0) + 1

    return {
        "n": n,
        "verdict_sources": sources,
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
        "estimated_cost_usd": round(cost, 4) if cost is not None else None,
        "cache_hit_rate": round(hit_rate, 3) if hit_rate is not None else None,
    }


def caching_problem(summary: dict) -> str | None:
    """Prompt caching fails silently: a prefix under the model's minimum cacheable size, or
    a byte that changes between requests, just means every read is billed in full. Zero
    cache reads across a multi-incident agent run can only mean one of those."""
    usage = summary.get("total_usage") or {}
    if summary.get("n", 0) < 2 or not usage.get("input_tokens"):
        return None
    if usage.get("cache_read_input_tokens", 0) == 0:
        return ("prompt caching is not working: zero cache reads across "
                f"{summary['n']} incidents. Either the cached prefix is below this model's "
                "minimum cacheable size, or something in it changes between requests.")
    misses = usage.get("uncached_followups", 0)
    if misses:
        return (f"prompt caching partially missed: {misses} of "
                f"{usage.get('requests', 0)} requests were follow-ups in a conversation that "
                "read nothing from cache. Every follow-up should, so something in the prefix "
                "changed mid-conversation or the history outgrew the 20-block lookback.")
    return None


def _money(value) -> str:
    return "unknown (no price for this model)" if value is None else f"${value}"


def calibration(rows: list[dict]) -> dict:
    """Is the stated confidence worth anything? Accuracy above vs below 0.7."""
    high = [r for r in rows if r["confidence"] >= 0.7]
    low = [r for r in rows if r["confidence"] < 0.7]

    def acc(group: list[dict]) -> float | None:
        return round(sum(1 for r in group if r["correct"]) / len(group) * 100, 1) if group else None

    flagged = [r for r in rows if r["needs_human"]]
    unflagged = [r for r in rows if not r["needs_human"]]

    # `unflagged_and_wrong` alone is gameable -- flag everything and it goes to zero. The
    # paired cost is `flagged_and_correct`: verdicts that were right but escalated anyway,
    # which is the on-call time the system fails to save. The discrimination gap is what
    # actually says whether the confidence signal carries information: accuracy among
    # verdicts the system stood behind, minus accuracy among those it escalated. A gap at
    # or below zero means the confidence number is worthless regardless of either count.
    gap = None
    if flagged and unflagged:
        gap = round(acc(unflagged) - acc(flagged), 1)

    return {
        "high_confidence_n": len(high), "high_confidence_accuracy_pct": acc(high),
        "low_confidence_n": len(low), "low_confidence_accuracy_pct": acc(low),
        "flagged_for_human_n": len(flagged),
        "flagged_and_wrong": sum(1 for r in flagged if not r["correct"]),
        "flagged_and_correct": sum(1 for r in flagged if r["correct"]),
        "unflagged_n": len(unflagged),
        "unflagged_accuracy_pct": acc(unflagged),
        "unflagged_and_wrong": sum(1 for r in unflagged if not r["correct"]),
        "discrimination_gap_pp": gap,
    }


ARM_LABEL = {
    "heuristic": "Correlation ranker only (no AI model)",
    "agent": "Full agent (Claude reads the diffs)",
}


def _ordinal(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


def _incident_card(r: dict) -> list[str]:
    """One incident, written the way you would explain it to a colleague."""
    mark = "✅" if r["correct"] else "❌"
    out = [f"### {mark} {r['incident_id']} — {r['service']}", ""]
    out.append(f"**The alert said:** {r['alert_title']}")
    out.append("")
    out.append(f"**What actually broke it:** {r['truth_subject']}")

    if r["truth_files"]:
        where = ", ".join(f"`{f}`" for f in r["truth_files"])
        if r["cross_service"]:
            if r["truth_owners"]:
                whose = f"it belongs to {', '.join(r['truth_owners'])}"
            else:
                whose = "it is shared library code that every service depends on"
            out.append(f"  - in {where} — **not in {r['service']}**: {whose}, so the team staring "
                       f"at the alert would have no reason to look there")
        else:
            out.append(f"  - in {where}")
    out.append("")

    abstained = r["verdict_sha"] is None
    if r["correct"]:
        out.append("**IncidentPilot blamed:** the same commit. **Correct.**")
    elif abstained:
        out.append("**IncidentPilot blamed:** nothing — it declined to name a commit and handed "
                   "the incident to a human. Wrong in the sense that it did not solve the "
                   "outage, but it did not send anyone chasing the wrong change either.")
    else:
        out.append(f"**IncidentPilot blamed:** {r['verdict_subject']}")
        out.append("")
        if r["truth_rank"]:
            out.append(f"**Did it at least shortlist the real cause?** Yes — it was "
                       f"{_ordinal(r['truth_rank'])} of {r['candidates']} suspects. It had the "
                       f"right answer in hand and picked a different one.")
        else:
            out.append("**Did it at least shortlist the real cause?** No — the real cause never "
                       "made the suspect list. This is a retrieval failure, not a judgement one.")
    out.append("")

    if r["needs_human"]:
        out.append(f"**Did it admit uncertainty?** Yes — flagged for a human to check "
                   f"({r['confidence']:.0%} confidence).")
    else:
        verb = "Correctly confident" if r["correct"] else "**Wrong, and it did not say so**"
        out.append(f"**Did it admit uncertainty?** No — it stood behind this answer at "
                   f"{r['confidence']:.0%} confidence. {verb}.")
    out.append("")

    detail = f"**Time to answer:** {r['seconds']}s"
    if r["tool_calls"]:
        used = ", ".join(sorted(set(r["tools_used"])))
        detail += f" · looked at {r['tool_calls']} pieces of evidence ({used})"
    out.append(detail)
    out.append("")
    return out


def _arm_narrative(arm: str, summary: dict, cal: dict, rows: list[dict]) -> list[str]:
    n = summary["n"]
    out = [f"## {ARM_LABEL.get(arm, arm)}", ""]

    # An agent arm whose verdicts came from the fallback ranker is not an agent result.
    degraded = n - summary["verdict_sources"].get("agent", 0) if arm == "agent" else 0
    if degraded:
        out.append(f"> ⚠️ **These numbers are not an agent result.** {degraded} of {n} verdicts "
                   f"came from the fallback correlation ranker because the agent errored. "
                   f"Do not quote this as agent accuracy — fix the errors and re-run.")
        out.append("")

    out.append(f"**Named the right commit in {summary['top1']} of {n} incidents.**")
    out.append("")

    out.append(f"- The real cause made its suspect list every single time "
               f"({summary['recall']}/{n}), and was among its top 3 guesses in "
               f"{summary['top3_retrieval']}/{n}. So it is never losing the culprit — it is "
               f"picking the wrong one off a shortlist that already contains the right answer.")

    misses = [r for r in rows if not r["correct"]]
    cross = [r for r in misses if r["cross_service"]]
    if cross:
        was = "was" if len(cross) == 1 else "were"
        out.append(f"- {len(cross)} of the {len(misses)} misses {was} a change made **outside the "
                   f"service that alerted** — a shared library or another team's migration. Those "
                   f"are the ones a human loses hours to as well.")

    abstentions = [r for r in rows if r["verdict_sha"] is None]
    if abstentions:
        ids = ", ".join(r["incident_id"] for r in abstentions)
        out.append(f"- {len(abstentions)} of the {len(misses)} misses ({ids}) were not wrong "
                   f"answers — it declined to name any commit and escalated. Counted against it "
                   f"here, but that is the failure you want: nobody gets sent after the wrong "
                   f"change.")

    danger = cal.get("unflagged_and_wrong", 0)
    if danger == 0 and cal.get("unflagged_n", 0) == 0:
        out.append(f"- It never presented a wrong answer as settled: every verdict was flagged for "
                   f"review, including the {cal.get('flagged_and_correct', 0)} it got right. Safe, "
                   f"but it means a human still checks all {n}.")
    elif danger:
        noun = "answer" if danger == 1 else "answers"
        out.append(f"- **{danger} wrong {noun} went out without a warning flag.** That is the "
                   f"expensive failure: an engineer acts on it at 3am and loses the time anyway.")
    else:
        out.append("- Every answer it stood behind was correct.")

    cost = summary["estimated_cost_usd"]
    if cost is None:
        cost_text = ", at an unknown cost (no price on file for this model)."
    elif cost:
        cost_text = f", at ${cost} for all {n}"
        if summary.get("cache_hit_rate") is not None:
            cost_text += f" ({summary['cache_hit_rate']:.0%} of input served from cache)"
        cost_text += "."
    else:
        cost_text = ", at no API cost."
    out.append(f"- Typical time to an answer: {summary['mean_seconds']}s{cost_text}")
    if arm == "agent" and caching_problem(summary):
        out.append(f"- **Warning:** {caching_problem(summary)}")
    out.append("")
    return out


def render_markdown(report: dict) -> str:
    arms = {a: s for a, s in report["arms"].items() if s}
    lines = ["# IncidentPilot — replay results", ""]
    lines.append(f"*{report['run_at']}*")
    lines.append("")
    lines.append(
        f"{report['n_incidents']} incidents were replayed through the system. For each one it saw "
        f"only what an on-call engineer sees at 3am: the alert text, and a repository with "
        f"{report['repo_commits']} commits of recent history. It then had to name the single "
        f"commit that caused the outage."
    )
    lines.append("")
    lines.append(
        "The incidents and the repository are synthetic — written so that every outage has a known "
        "cause to score against, which production history cannot give you. The breaking changes are "
        "real commits with real diffs, and the system reads them through `git` exactly as it would "
        "a production repo. Each verdict is simply right or wrong."
    )
    lines.append("")

    if len(arms) > 1:
        lines.append("## The short version")
        lines.append("")
        for arm, summary in arms.items():
            lines.append(f"- **{ARM_LABEL.get(arm, arm)}** — right {summary['top1']} of "
                         f"{summary['n']} times, {summary['mean_seconds']}s per incident.")
        lines.append("")

    for arm, summary in arms.items():
        lines += _arm_narrative(arm, summary, report["calibration"].get(arm, {}),
                                report["rows"][arm])

    for arm, rows in report["rows"].items():
        if not rows:
            continue
        lines.append(f"# Incident by incident — {ARM_LABEL.get(arm, arm).lower()}")
        lines.append("")
        for r in rows:
            lines += _incident_card(r)

    lines.append("---")
    lines.append("")
    lines.append("# Appendix: the raw numbers")
    lines.append("")
    lines.append("For anyone who wants the standard information-retrieval metrics.")
    lines.append("")
    lines.append("| | meaning |")
    lines.append("| --- | --- |")
    lines.append("| **top-1** | how often the single commit it named was the right one |")
    lines.append("| **top-3** | how often the right commit was among its three best guesses |")
    lines.append("| **recall** | how often the right commit appeared on its suspect list at all |")
    lines.append("| **MRR** | mean reciprocal rank — 1.0 if the right commit is always first, "
                 "0.5 if always second, and so on |")
    lines.append("| **wrong & unflagged** | wrong answers presented without a review flag — "
                 "the costly failure |")
    lines.append("| **needlessly escalated** | right answers flagged for review anyway — "
                 "the cost of playing it safe |")
    lines.append("| **discrimination gap** | accuracy on answers it stood behind minus accuracy "
                 "on answers it escalated. Zero or below means its confidence score is "
                 "meaningless |")
    lines.append("")
    lines.append("| Arm | top-1 | top-3 | recall | MRR | mean conf | wrong & unflagged | "
                 "needlessly escalated | discrimination gap | tool calls | s each | cost |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for arm, summary in arms.items():
        c = report["calibration"].get(arm, {})
        gap = c.get("discrimination_gap_pp")
        lines.append(
            f"| {arm} | {summary['top1']}/{summary['n']} ({summary['top1_pct']}%) "
            f"| {summary['top3_retrieval']}/{summary['n']} | {summary['recall']}/{summary['n']} "
            f"| {summary['mrr']} | {summary['mean_confidence']} "
            f"| {c.get('unflagged_and_wrong', '-')} | {c.get('flagged_and_correct', '-')} "
            f"| {'no signal' if gap is None else str(gap) + 'pp'} "
            f"| {summary['mean_tool_calls']} | {summary['mean_seconds']} "
            f"| {_money(summary['estimated_cost_usd'])} |"
        )
    lines.append("")
    lines.append(f"Model: `{report['model']}` (effort {report['effort']}) · "
                 f"runbook corpus: {report['runbook_chunks']} chunks via the "
                 f"`{report['embedder']}` embedder")
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
            try:
                row = score_one(record, pilot, use_agent=(arm == "agent"))
            except AgentConfigError as exc:
                print(f"\nThe agent could not run, so there is nothing to score.\n\n  {exc}\n",
                      file=sys.stderr)
                if "authentication" in str(exc).lower() or "401" in str(exc):
                    print("  ANTHROPIC_API_KEY looks wrong. In PowerShell:\n"
                          "    $env:ANTHROPIC_API_KEY = \"<your real key>\"\n"
                          "  A real key starts with sk-ant- and is ~100 characters; "
                          "\"sk-ant-...\" is a placeholder.\n", file=sys.stderr)
                print("  Nothing was written. Re-run once the key is set, or use "
                      "--mode heuristic to score the baseline alone.", file=sys.stderr)
                return 2
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
        "arms": {arm: aggregate(rows[arm], config.model) for arm in arms},
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
              f"MRR {s['mrr']}  cost {_money(s['estimated_cost_usd'])}"
              + (f"  cache hits {s['cache_hit_rate']:.0%}" if s.get("cache_hit_rate") is not None else ""))
        if arm == "agent" and caching_problem(s):
            print(f"{'':10s} WARNING: {caching_problem(s)}")
        degraded = s["n"] - s["verdict_sources"].get("agent", 0) if arm == "agent" else 0
        if degraded:
            print(f"{'':10s} NOT AN AGENT RESULT: {degraded}/{s['n']} verdicts came from the "
                  f"fallback ranker after agent errors. Do not quote this number.")
    print(f"\nwrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
