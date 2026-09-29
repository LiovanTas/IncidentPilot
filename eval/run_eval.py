"""Replay incidents and score root-cause accuracy.

Three suites, reported separately so a saturated one cannot mask a hard one:

  synthetic  20 scripted incidents in a synthetic monorepo, one guilty commit each
  negatives  8 alerts with NO guilty commit -- the right answer is to name nothing
  realworld  regressions mined from revert commits in grafana, moby and prometheus

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
    py -3.13 eval/run_eval.py --mode agent --suite realworld
    py -3.13 eval/run_eval.py --mode agent --suite all
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
from typing import Callable

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
REALWORLD_REPOS = ROOT / "eval" / "realworld" / "repos"

SUITES = {
    "synthetic": INCIDENTS_JSON,
    "negatives": ROOT / "eval" / "negatives.json",
    "realworld": ROOT / "eval" / "realworld" / "incidents.json",
}
SUITE_BUILDERS = {
    "synthetic": "py -3.13 eval/build_fixture_repo.py",
    "negatives": "py -3.13 eval/make_negatives.py",
    "realworld": "py -3.13 eval/realworld/build.py",
}
SUITE_INTRO = {
    "synthetic": (
        "{n} incidents were replayed through the system. For each one it saw only what an "
        "on-call engineer sees at 3am: the alert text, and a repository with {commits} commits "
        "of recent history. It then had to name the single commit that caused the outage.\n\n"
        "The incidents and the repository are synthetic -- written so that every outage has a "
        "known cause to score against, which production history cannot give you. The breaking "
        "changes are real commits with real diffs, read through `git` exactly as a production "
        "repo would be. Each verdict is simply right or wrong."
    ),
    "negatives": (
        "{n} alerts in which **no commit is to blame**: a provider outage, a traffic spike, an "
        "expired certificate, a full disk. The right answer is to name no commit and escalate. "
        "Each window still contains several innocent commits, chosen as the most tempting window "
        "in which nothing the alerting service depends on changed -- so the test is whether it "
        "resists blaming a deploy that had nothing to do with it."
    ),
    "realworld": (
        "{n} real regressions from grafana, moby and prometheus. Each is a commit that its "
        "developers later reverted; the alert is the revert's own stated reason with anything "
        "that names the change removed, and the repository is pinned to one hour before the "
        "revert, so the answer does not exist yet. These windows hold dozens to hundreds of "
        "real commits, written by many people, and there is no service map -- this is the "
        "hard test. Many of these reverts were for broken builds or features rather than "
        "production outages, so read the alerts as bug reports. See "
        "`eval/realworld/FUNNEL.md` for how every incident was selected."
    ),
}



def rank_of(sha: str, candidates: list) -> int | None:
    for i, candidate in enumerate(candidates, 1):
        if candidate.sha == sha:
            return i
    return None


def score_one(record: dict, pilot: IncidentPilot, use_agent: bool) -> dict:
    alert = normalize(record["alert"])[0]
    truth = record.get("ground_truth_sha")  # None for a negative control
    negative = truth is None

    started = time.perf_counter()
    result = pilot.handle(alert, use_agent=use_agent)
    elapsed = time.perf_counter() - started

    rank = rank_of(truth, result.candidates) if truth else None
    verdict_sha = result.diagnosis.offending_sha or ""
    if negative:
        correct = not verdict_sha  # nothing to blame, so naming nothing is right
    else:
        correct = bool(verdict_sha) and truth.startswith(verdict_sha[:10])

    by_sha = {c.sha: c for c in result.candidates}
    blamed = next((c for c in result.candidates if c.sha.startswith(verdict_sha[:10])), None) if verdict_sha else None
    truth_commit = by_sha.get(truth) if truth else None

    # Was the real cause outside the service that alerted? Those are the hard ones: a
    # shared library or another team's migration breaking a downstream service. Only
    # meaningful where a service map exists -- the real-world repos have none.
    known_service = alert.service in pilot.topology.get("services", {})
    owners = truth_commit.services if truth_commit else []
    cross_service = bool(truth_commit) and known_service and alert.service not in owners

    return {
        "incident_id": record["incident_id"],
        "service": alert.service,
        "truth_sha": truth[:10] if truth else None,
        "truth_subject": record["ground_truth_subject"],
        "negative": negative,
        "cause": record.get("cause"),
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
    # Retrieval only means something where there is a guilty commit to retrieve.
    positives = [r for r in rows if not r.get("negative")]
    negatives = [r for r in rows if r.get("negative")]
    p = len(positives)
    top3 = sum(1 for r in positives if r["truth_rank"] and r["truth_rank"] <= 3)
    recall = sum(1 for r in positives if r["truth_rank"])
    mrr = sum(1 / r["truth_rank"] for r in positives if r["truth_rank"]) / p if p else None

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
        "positives": p,
        "top3_retrieval": top3 if p else None,
        "top3_pct": round(top3 / p * 100, 1) if p else None,
        "recall": recall if p else None,
        "recall_pct": round(recall / p * 100, 1) if p else None,
        "mrr": round(mrr, 3) if mrr is not None else None,
        "negatives": len(negatives),
        "negatives_named_nothing": sum(1 for r in negatives if r["verdict_sha"] is None),
        "negatives_blamed_but_flagged": sum(1 for r in negatives if r["verdict_sha"] and r["needs_human"]),
        "negatives_blamed_confidently": sum(1 for r in negatives if r["verdict_sha"] and not r["needs_human"]),
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


def _negative_card(r: dict) -> list[str]:
    mark = "✅" if r["correct"] else "❌"
    out = [f"### {mark} {r['incident_id']} — {r['service']}", ""]
    out.append(f"**The alert said:** {r['alert_title']}")
    out.append("")
    out.append(f"**What actually caused it:** nothing in the codebase. {r['cause']}")
    out.append("")
    if r["correct"]:
        out.append("**IncidentPilot blamed:** no commit. **Correct** — it recognised the cause "
                   "was outside the code and handed it to a human.")
    else:
        out.append(f"**IncidentPilot blamed:** {r['verdict_subject'] or r['verdict_sha']} — "
                   f"**wrong**: that change had nothing to do with it.")
        out.append("")
        if r["needs_human"]:
            out.append(f"It did flag the verdict for review ({r['confidence']:.0%} confidence), so "
                       f"the damage is someone checking an innocent change before acting.")
        else:
            out.append(f"**And it stood behind it** at {r['confidence']:.0%} confidence — the "
                       f"expensive failure: a healthy change gets reverted while the real "
                       f"problem carries on.")
    out.append("")
    out.append(f"**Time to answer:** {r['seconds']}s")
    out.append("")
    return out


def _incident_card(r: dict) -> list[str]:
    """One incident, written the way you would explain it to a colleague."""
    if r.get("negative"):
        return _negative_card(r)
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

    p = summary.get("positives", n)
    negs = summary.get("negatives", 0)
    if negs and not p:
        out.append(f"**Correctly named no commit in {summary['top1']} of {n} alerts where "
                   f"nothing in the code was to blame.**")
    else:
        out.append(f"**Named the right commit in {summary['top1']} of {n} incidents.**")
    out.append("")

    if p:
        recall, top3 = summary["recall"], summary["top3_retrieval"]
        if recall == p:
            out.append(f"- The real cause made its suspect list every single time ({recall}/{p}), "
                       f"and was among its top 3 guesses in {top3}/{p}. So it is never losing the "
                       f"culprit — any miss is picking the wrong one off a shortlist that already "
                       f"contains the right answer.")
        else:
            out.append(f"- The real cause made its suspect list in {recall} of {p} incidents, and "
                       f"was in its top 3 in {top3}. In the other {p - recall} it was never "
                       f"shortlisted, so no amount of judgement could have found it — those misses "
                       f"belong to the ranker, not the agent.")
    if negs:
        confident = summary["negatives_blamed_confidently"]
        flagged = summary["negatives_blamed_but_flagged"]
        if confident:
            out.append(f"- **It confidently blamed an innocent commit in {confident} of {negs} "
                       f"alerts with no code cause.** That is the failure that gets a healthy "
                       f"change reverted while the real problem continues.")
        if flagged:
            out.append(f"- In {flagged} more it named an innocent commit but flagged it for "
                       f"review — wrong, but a human checks before anything is reverted.")

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


def _cell(value, total=None) -> str:
    if value is None:
        return "—"
    return f"{value}/{total}" if total is not None else str(value)


def render_markdown(report: dict) -> str:
    arms = {a: s for a, s in report["arms"].items() if s}
    suite = report.get("suite", "synthetic")
    lines = [f"# IncidentPilot — {suite} replay results", ""]
    lines.append(f"*{report['run_at']}*")
    lines.append("")
    lines.append(SUITE_INTRO[suite].format(n=report["n_incidents"], commits=report["repo_commits"]))
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
            f"| {_cell(summary['top3_retrieval'], summary.get('positives'))} "
            f"| {_cell(summary['recall'], summary.get('positives'))} "
            f"| {_cell(summary['mrr'])} | {summary['mean_confidence']} "
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


def _pilot_factory(config) -> Callable[[dict], IncidentPilot]:
    """Synthetic and negative records share the fixture repo; each real-world record lives
    in its own clone, with no service map and no relevant runbooks to inject."""
    pilots: dict[str, IncidentPilot] = {}

    def pilot_for(record: dict) -> IncidentPilot:
        key = record.get("repo") or "fixture"
        if key not in pilots:
            if key == "fixture":
                pilots[key] = IncidentPilot(config)
            else:
                path = REALWORLD_REPOS / key
                if not (path / ".git").exists():
                    raise SystemExit(f"missing clone {path} -- run {SUITE_BUILDERS['realworld']}")
                pilots[key] = IncidentPilot(load_config(
                    **{**_overrides(config), "repo_path": path, "top_k": 0}))
        return pilots[key]

    return pilot_for


def _overrides(config) -> dict:
    """The fields a caller may have overridden, carried into per-repo configs."""
    return {"model": config.model, "effort": config.effort,
            "prefetch_diffs": config.prefetch_diffs, "enable_read_file": config.enable_read_file,
            "escalate_below": config.escalate_below}


def run_suite(suite: str, records: list[dict], arms: list[str], config, quiet: bool) -> dict | int:
    pilot_for = _pilot_factory(config)
    rows: dict[str, list[dict]] = {}
    for arm in arms:
        rows[arm] = []
        for record in records:
            try:
                row = score_one(record, pilot_for(record), use_agent=(arm == "agent"))
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
            if not quiet:
                mark = "HIT " if row["correct"] else "miss"
                print(f"[{suite}/{arm}] {row['incident_id']} {mark} "
                      f"truth={row['truth_sha'] or '(none)'} verdict={row['verdict_sha'] or '-'} "
                      f"rank={row['truth_rank']} conf={row['confidence']:.2f} "
                      f"tools={row['tool_calls']} {row['seconds']}s")

    if suite == "realworld":
        commits = "real-world"
    else:
        commits = subprocess.run(
            ["git", "-C", str(config.repo_path), "rev-list", "--count", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
    any_pilot = pilot_for(records[0])
    return {
        "suite": suite,
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_incidents": len(records),
        "repo_commits": commits,
        "model": config.model if "agent" in arms else "n/a (heuristic only)",
        "effort": config.effort,
        "embedder": any_pilot.index.embedder.name,
        "runbook_chunks": any_pilot.index.size,
        "arms": {arm: aggregate(rows[arm], config.model) for arm in arms},
        "calibration": {arm: calibration(rows[arm]) for arm in arms},
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["heuristic", "agent", "both"], default="heuristic")
    parser.add_argument("--suite", choices=[*SUITES, "all"], default="synthetic")
    parser.add_argument("--limit", type=int, help="only run the first N incidents of each suite")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    suites = list(SUITES) if args.suite == "all" else [args.suite]
    for suite in suites:
        if not SUITES[suite].exists():
            print(f"no {SUITES[suite].relative_to(ROOT)} -- build it with: {SUITE_BUILDERS[suite]}",
                  file=sys.stderr)
            return 1

    config = load_config()
    arms = ["heuristic"] if args.mode == "heuristic" else (
        ["agent"] if args.mode == "agent" else ["heuristic", "agent"]
    )
    if "agent" in arms and not config.has_anthropic_key:
        print("ANTHROPIC_API_KEY is not set - the agent arm cannot run.", file=sys.stderr)
        if args.mode == "agent":
            return 1
        arms = ["heuristic"]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rendered: list[str] = []
    for suite in suites:
        records = json.loads(SUITES[suite].read_text(encoding="utf-8"))
        if args.limit:
            records = records[: args.limit]
        report = run_suite(suite, records, arms, config, args.quiet)
        if isinstance(report, int):
            return report

        (RESULTS_DIR / f"eval-{stamp}-{suite}.json").write_text(json.dumps(report, indent=2),
                                                                 encoding="utf-8")
        markdown = render_markdown(report)
        (RESULTS_DIR / f"eval-{stamp}-{suite}.md").write_text(markdown, encoding="utf-8")
        rendered.append(markdown)

        print()
        for arm in arms:
            s = report["arms"][arm]
            retrieval = (f"top-3 {s['top3_retrieval']}/{s['positives']}  recall "
                         f"{s['recall']}/{s['positives']}  MRR {s['mrr']}  " if s["positives"] else "")
            negatives = (f"named nothing {s['negatives_named_nothing']}/{s['negatives']}, "
                         f"blamed confidently {s['negatives_blamed_confidently']}  "
                         if s["negatives"] else "")
            print(f"{suite:10s} {arm:10s} top-1 {s['top1']}/{s['n']} ({s['top1_pct']}%)  "
                  f"{retrieval}{negatives}cost {_money(s['estimated_cost_usd'])}"
                  + (f"  cache hits {s['cache_hit_rate']:.0%}" if s.get("cache_hit_rate") is not None else ""))
            if arm == "agent" and caching_problem(s):
                print(f"{'':21s} WARNING: {caching_problem(s)}")
            degraded = s["n"] - s["verdict_sources"].get("agent", 0) if arm == "agent" else 0
            if degraded:
                print(f"{'':21s} NOT AN AGENT RESULT: {degraded}/{s['n']} verdicts came from the "
                      f"fallback ranker after agent errors. Do not quote this number.")

    (RESULTS_DIR / "latest.md").write_text("\n\n---\n\n".join(rendered), encoding="utf-8")
    print(f"\nwrote {RESULTS_DIR / 'latest.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
