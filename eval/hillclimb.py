"""Hill-climb: improve the agent one pre-registered change at a time, without fooling
ourselves.

The loop, one iteration per `run`:

    READ     look at the dev-split failures from the last iteration (never the test split)
    PROPOSE  pick one change, write its hypothesis into eval/variants.py, commit it
    APPLY    variants are config overrides, so nothing in main code changes per experiment
    RUN      score the variant on dev AND test, K repeats each
    RECORD   state.json, scores.tsv and a per-iteration decision.md, all committed
    DECIDE   the pre-registered rule below says KEEP or REJECT; nobody argues with it

Rules that make the numbers mean something:

  * Improvements are judged on the dev split. The test split is only a non-regression
    gate, and only its aggregate is ever printed. Choosing changes by looking at test
    results turns the test set into a second dev set.
  * Baseline runs are repeated so run-to-run noise is measured, not assumed. A change
    has to beat that noise to count.
  * Safety ratchet: no change may increase wrong-and-unflagged verdicts, whatever else
    it improves.
  * Results from different benchmark versions are never compared; editing an incident
    invalidates the baseline.

    py -3.13 eval/hillclimb.py init --budget 30
    py -3.13 eval/hillclimb.py run baseline --repeats 2
    py -3.13 eval/hillclimb.py run effort-medium --repeats 2
    py -3.13 eval/hillclimb.py status
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_eval  # noqa: E402  (sets the fixture-repo environment on import)
import variants  # noqa: E402
from incidentpilot.config import load_config  # noqa: E402
from incidentpilot.pipeline import AgentConfigError, IncidentPilot  # noqa: E402
from incidentpilot.pricing import price_for  # noqa: E402

STATE_DIR = HERE / "hillclimb"
STATE = STATE_DIR / "state.json"
SPLIT = STATE_DIR / "split.json"
SCORES = STATE_DIR / "scores.tsv"
RUNS = STATE_DIR / "runs"
INCIDENTS = HERE / "incidents.json"
REPO = HERE / "fixtures" / "repo"

SPLIT_SEED = 20260929
TEST_FRACTION = 0.4
# Opus 5 measured $0.15/incident. Sonnet 5 is 40% of the per-token price, with better
# caching on top, so this is a deliberately cautious estimate used only until a
# baseline exists.
DEFAULT_COST_PER_INCIDENT = 0.08

# A cost or latency change smaller than this is not worth a config change even if real.
MIN_RELATIVE_GAIN = 0.10
# A calibration change may escalate at most this share of incidents that were right.
MAX_NEEDLESS_ESCALATION = 0.10


# ============================================================================ split


def _benchmark_fingerprint() -> str:
    return hashlib.sha256(INCIDENTS.read_bytes()).hexdigest()[:16]


def _cause_is_cross_service(record: dict) -> bool:
    """Whether the true cause lives outside the alerting service -- the hard cases. Used
    only to stratify the split so both halves contain some."""
    files = subprocess.run(
        ["git", "-C", str(REPO), "show", "--name-only", "--format=", record["ground_truth_sha"]],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    service = record["alert"]["service"]
    topology = json.loads((HERE.parent / "data" / "topology.json").read_text(encoding="utf-8"))
    own = topology["services"].get(service, {}).get("paths", [])
    return not any(f.startswith(p) for f in files for p in own)


def make_split(records: list[dict]) -> dict:
    """Seeded, stratified 60/40 dev/test split. Deterministic, so it is reproducible."""
    rng = random.Random(SPLIT_SEED)
    strata: dict[bool, list[str]] = {True: [], False: []}
    for record in records:
        strata[_cause_is_cross_service(record)].append(record["incident_id"])

    dev: list[str] = []
    test: list[str] = []
    for cross, ids in sorted(strata.items()):
        ids = sorted(ids)
        rng.shuffle(ids)
        cut = max(1, round(len(ids) * TEST_FRACTION))
        test += ids[:cut]
        dev += ids[cut:]

    return {
        "seed": SPLIT_SEED,
        "benchmark": _benchmark_fingerprint(),
        "dev": sorted(dev),
        "test": sorted(test),
        "stratified_on": "whether the true cause lives outside the alerting service",
        "caveat": (
            "This split was drawn after one full agent run over all 20 incidents had already "
            "been inspected, so the test split is held out from iteration decisions from here "
            "on -- it was not held out from all prior observation."
        ),
    }


# ======================================================================== decision


@dataclass
class Decision:
    keep: bool
    checks: list[tuple[bool, str]]
    warnings: list[str]

    @property
    def verdict(self) -> str:
        return "KEEP" if self.keep else "REJECT"


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _spread(xs: list[float]) -> float:
    return (max(xs) - min(xs)) if xs else 0.0


def _vals(reps: list[dict], split: str, key: str) -> list[float]:
    return [float(r[split][key]) for r in reps]


def _both(reps: list[dict], key: str) -> list[float]:
    return [float(r["dev"][key]) + float(r["test"][key]) for r in reps]


def _per_incident(reps: list[dict], key: str) -> list[float]:
    return [(r["dev"][key] + r["test"][key]) / (r["dev"]["n"] + r["test"]["n"]) for r in reps]


def decide(baseline: list[dict], candidate: list[dict], target: str | None) -> Decision:
    """The pre-registered keep/reject rule. Pure function of the recorded metrics.

    Every check must pass. `baseline` and `candidate` are lists of per-repeat metric
    dicts shaped {"dev": {...}, "test": {...}}.
    """
    checks: list[tuple[bool, str]] = []
    warnings: list[str] = []

    if len(baseline) < 2:
        warnings.append("baseline has fewer than 2 repeats, so run-to-run noise is unmeasured; "
                        "the accuracy noise band falls back to 1 incident")

    # 1. Safety ratchet -- worst case against worst case, so noise cannot mask a regression.
    base_wu, cand_wu = max(_both(baseline, "unflagged_wrong")), max(_both(candidate, "unflagged_wrong"))
    checks.append((cand_wu <= base_wu,
                   f"safety: worst-case wrong-and-unflagged {cand_wu:.0f} vs baseline {base_wu:.0f} "
                   f"(may not rise)"))

    # 2. Accuracy may not regress beyond noise, on either split.
    for split in ("test", "dev"):
        base_top1 = _vals(baseline, split, "top1")
        band = max(_spread(base_top1), 1.0)
        cand_mean, base_mean = _mean(_vals(candidate, split, "top1")), _mean(base_top1)
        n = baseline[0][split]["n"]
        checks.append((cand_mean >= base_mean - band,
                       f"{split} accuracy: {cand_mean:.1f}/{n} vs baseline {base_mean:.1f}/{n} "
                       f"(may not drop more than the {band:.0f}-incident noise band)"))

    # 3. It has to actually improve the thing it set out to improve.
    if target == "accuracy":
        base = _vals(baseline, "dev", "top1")
        band = max(_spread(base), 1.0)
        cand_mean, base_mean = _mean(_vals(candidate, "dev", "top1")), _mean(base)
        n_dev = baseline[0]["dev"]["n"]
        if base_mean + band >= n_dev:
            warnings.append(
                f"dev baseline is already {base_mean:.1f}/{n_dev}, so no accuracy gain can clear "
                f"the noise band on this split -- a REJECT here means 'unmeasurable', not "
                f"'refuted'. Accuracy variants need a harder benchmark."
            )
        checks.append((cand_mean > base_mean + band,
                       f"target accuracy (dev): {cand_mean:.1f} vs {base_mean:.1f} "
                       f"(must beat baseline by more than {band:.0f} incident)"))
    elif target in ("cost", "latency"):
        key = "cost" if target == "cost" else "seconds_total"
        base, cand = _per_incident(baseline, key), _per_incident(candidate, key)
        base_mean, cand_mean = _mean(base), _mean(cand)
        gain = (base_mean - cand_mean) / base_mean if base_mean else 0.0
        beyond_noise = (base_mean - cand_mean) > _spread(base)
        fmt = (lambda v: f"${v:.3f}") if target == "cost" else (lambda v: f"{v:.1f}s")
        checks.append((gain >= MIN_RELATIVE_GAIN and beyond_noise,
                       f"target {target}: {fmt(cand_mean)} vs {fmt(base_mean)} per incident, "
                       f"{gain:+.0%} better (needs >= {MIN_RELATIVE_GAIN:.0%} and beyond baseline noise)"))
    elif target == "calibration":
        base_wu_mean, cand_wu_mean = _mean(_both(baseline, "unflagged_wrong")), _mean(_both(candidate, "unflagged_wrong"))
        checks.append((cand_wu_mean < base_wu_mean,
                       f"target calibration: wrong-and-unflagged {cand_wu_mean:.1f} vs {base_wu_mean:.1f} "
                       f"(must fall)"))
        n_total = baseline[0]["dev"]["n"] + baseline[0]["test"]["n"]
        extra = _mean(_both(candidate, "flagged_correct")) - _mean(_both(baseline, "flagged_correct"))
        allowed = MAX_NEEDLESS_ESCALATION * n_total
        checks.append((extra <= allowed,
                       f"calibration cost: {extra:+.1f} correct answers escalated "
                       f"(at most {allowed:.0f} allowed)"))
    elif target is not None:
        raise ValueError(f"unknown target {target!r}")

    return Decision(keep=all(ok for ok, _ in checks), checks=checks, warnings=warnings)


# ========================================================================== running


def _metrics(rows: list[dict], model: str) -> dict:
    agg = run_eval.aggregate(rows, model)
    cal = run_eval.calibration(rows)
    return {
        "n": agg["n"],
        "top1": agg["top1"],
        "unflagged_wrong": cal["unflagged_and_wrong"],
        "flagged_correct": cal["flagged_and_correct"],
        "cost": agg["estimated_cost_usd"] or 0.0,
        "cache_hit_rate": agg["cache_hit_rate"],
        "model": model,
        "seconds_total": round(sum(r["seconds"] for r in rows), 2),
        "tool_calls_mean": agg["mean_tool_calls"],
        "sources": agg["verdict_sources"],
        "usage": agg["total_usage"],
    }


def _load_state() -> dict:
    if not STATE.exists():
        raise SystemExit("No hill-climb state. Start with:  py -3.13 eval/hillclimb.py init --budget 30")
    return json.loads(STATE.read_text(encoding="utf-8"))


def _save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _baseline_iteration(state: dict) -> dict | None:
    runs = [it for it in state["iterations"] if it["variant"] == "baseline"]
    return runs[-1] if runs else None


def _load_reps(iteration: dict) -> list[dict]:
    folder = RUNS / iteration["folder"]
    return [json.loads(p.read_text(encoding="utf-8"))["metrics"]
            for p in sorted(folder.glob("rep-*.json"))]


def _guard_benchmark(state: dict) -> None:
    if state["benchmark"] != _benchmark_fingerprint():
        raise SystemExit(
            "eval/incidents.json has changed since this hill-climb started, so results would "
            "not be comparable with the recorded baseline.\n"
            "Start a new campaign:  move eval/hillclimb/ aside, then run `init` again."
        )
    if load_config().model != state["default_model"]:
        raise SystemExit(
            f"The default model changed from {state['default_model']} to {load_config().model} "
            f"since this hill-climb started, so every variant would be compared against a "
            f"baseline measured on a different model. Start a new campaign with `init --force`."
        )
    tag = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", "-q", "benchmark-baseline"],
                         capture_output=True, text=True)
    if tag.returncode == 0:
        raise SystemExit("The live demo bug is still applied to the test repo. Undo it first:\n"
                         "  py -3.13 eval/demo_new_bug.py --reset")


def cmd_init(args: argparse.Namespace) -> int:
    if STATE.exists() and not args.force:
        print(f"A hill-climb already exists at {STATE_DIR}. Use --force to restart it.")
        return 1
    records = json.loads(INCIDENTS.read_text(encoding="utf-8"))
    split = make_split(records)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(exist_ok=True)
    SPLIT.write_text(json.dumps(split, indent=2), encoding="utf-8")
    SCORES.write_text("iter\tvariant\trepeats\tdev_top1\ttest_top1\twrong_unflagged\t"
                      "cost_per_incident\tsec_per_incident\tdecision\tspent_usd\n", encoding="utf-8")
    _save_state({
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "budget_usd": args.budget,
        "spent_usd": 0.0,
        "benchmark": split["benchmark"],
        "default_model": load_config().model,
        "iterations": [],
    })
    print(f"Hill-climb initialised in {STATE_DIR}")
    print(f"  model  : {load_config().model}")
    print(f"  budget : ${args.budget:.2f}")
    print(f"  dev    : {len(split['dev'])} incidents  {', '.join(split['dev'])}")
    print(f"  test   : {len(split['test'])} incidents  (held out -- only aggregates are shown)")
    print("\nNext:  py -3.13 eval/hillclimb.py run baseline --repeats 2")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    state = _load_state()
    _guard_benchmark(state)
    variant = variants.get(args.variant)
    split = json.loads(SPLIT.read_text(encoding="utf-8"))

    baseline_it = _baseline_iteration(state)
    if variant.name != "baseline" and baseline_it is None:
        print("Run the baseline first -- every variant is judged against it:\n"
              "  py -3.13 eval/hillclimb.py run baseline --repeats 2")
        return 1

    config = load_config(**variant.overrides)
    if not config.has_anthropic_key:
        print("ANTHROPIC_API_KEY is not set -- the hill-climb only measures the agent.")
        return 1

    records = json.loads(INCIDENTS.read_text(encoding="utf-8"))
    by_id = {r["incident_id"]: r for r in records}
    n = len(split["dev"]) + len(split["test"])
    per_incident = (baseline_it["cost_per_incident"] if baseline_it else DEFAULT_COST_PER_INCIDENT)
    # A variant on a pricier model costs proportionally more than the baseline measured.
    base_price, var_price = price_for(state["default_model"]), price_for(config.model)
    if base_price and var_price:
        per_incident *= var_price.output / base_price.output
    estimate = per_incident * n * args.repeats
    remaining = state["budget_usd"] - state["spent_usd"]
    print(f"{variant.name}: {args.repeats} repeat(s) x {n} incidents, estimated ${estimate:.2f} "
          f"(${remaining:.2f} of ${state['budget_usd']:.2f} budget left)")
    if estimate > remaining and not args.over_budget:
        print("That would exceed the budget. Raise it in eval/hillclimb/state.json, or pass "
              "--over-budget if you mean to.")
        return 1
    if variant.contamination:
        print(f"\n  NOTE: {variant.contamination}\n")

    iteration_no = len(state["iterations"]) + 1
    folder = f"{iteration_no:02d}-{variant.name}"
    (RUNS / folder).mkdir(parents=True, exist_ok=True)

    pilot = IncidentPilot(config)
    reps: list[dict] = []
    spent = 0.0
    for rep in range(1, args.repeats + 1):
        rows_by_split: dict[str, list[dict]] = {"dev": [], "test": []}
        for split_name in ("dev", "test"):
            for incident_id in split[split_name]:
                try:
                    row = run_eval.score_one(by_id[incident_id], pilot, use_agent=True)
                except AgentConfigError as exc:
                    print(f"\nAgent could not run: {exc}\nNothing recorded for this iteration.")
                    return 2
                rows_by_split[split_name].append(row)
                shown = ("HIT " if row["correct"] else "miss") if split_name == "dev" else "  . "
                print(f"  [rep {rep} {split_name:4s}] {incident_id} {shown} "
                      f"conf={row['confidence']:.2f} tools={row['tool_calls']} {row['seconds']}s")

        metrics = {s: _metrics(rows, config.model) for s, rows in rows_by_split.items()}
        agent_verdicts = sum(m["sources"].get("agent", 0) for m in metrics.values())
        if agent_verdicts != n:
            print(f"\nOnly {agent_verdicts}/{n} verdicts came from the agent -- the rest fell back "
                  f"to the heuristic after errors. This repeat is not a valid measurement and "
                  f"is not recorded.")
            return 2

        spent += metrics["dev"]["cost"] + metrics["test"]["cost"]
        (RUNS / folder / f"rep-{rep}.json").write_text(
            json.dumps({"metrics": metrics, "rows": rows_by_split}, indent=2), encoding="utf-8")
        reps.append(metrics)

    decision = decide(_load_reps(baseline_it), reps, variant.target) if baseline_it else None

    def m(key, split=None):
        vals = [r[split][key] for r in reps] if split else [r["dev"][key] + r["test"][key] for r in reps]
        return _mean(vals)

    cost_pi = m("cost") / n
    sec_pi = m("seconds_total") / n
    state["spent_usd"] = round(state["spent_usd"] + spent, 4)
    state["iterations"].append({
        "iter": iteration_no,
        "variant": variant.name,
        "folder": folder,
        "repeats": args.repeats,
        "target": variant.target,
        "dev_top1": m("top1", "dev"),
        "test_top1": m("top1", "test"),
        "wrong_unflagged": m("unflagged_wrong"),
        "cost_per_incident": round(cost_pi, 4),
        "sec_per_incident": round(sec_pi, 1),
        "decision": decision.verdict if decision else "BASELINE",
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _save_state(state)

    with SCORES.open("a", encoding="utf-8") as fh:
        fh.write(f"{iteration_no}\t{variant.name}\t{args.repeats}\t{m('top1', 'dev'):.1f}\t"
                 f"{m('top1', 'test'):.1f}\t{m('unflagged_wrong'):.1f}\t{cost_pi:.4f}\t"
                 f"{sec_pi:.1f}\t{decision.verdict if decision else 'BASELINE'}\t"
                 f"{state['spent_usd']:.2f}\n")

    _write_decision(folder, variant, reps, decision, split)

    print(f"\n{'=' * 72}\n{folder}")
    print(f"  dev  top-1 : {m('top1', 'dev'):.1f}/{len(split['dev'])}")
    print(f"  test top-1 : {m('top1', 'test'):.1f}/{len(split['test'])}   (held out)")
    print(f"  wrong & unflagged : {m('unflagged_wrong'):.1f}")
    print(f"  per incident      : ${cost_pi:.3f}, {sec_pi:.1f}s")
    print(f"  spent so far      : ${state['spent_usd']:.2f} of ${state['budget_usd']:.2f}")
    if decision:
        print(f"\n  DECISION: {decision.verdict}")
        for ok, text in decision.checks:
            print(f"    [{'pass' if ok else 'FAIL'}] {text}")
        for w in decision.warnings:
            print(f"    warning: {w}")
    print(f"\n  written to {RUNS / folder}")
    return 0


def _write_decision(folder: str, variant, reps: list[dict], decision: Decision | None,
                    split: dict) -> None:
    lines = [f"# {folder}", "", f"**Variant:** `{variant.name}` -> `{json.dumps(variant.overrides)}`",
             f"**Target:** {variant.target or 'n/a (baseline)'}", "",
             "## Pre-registered hypothesis", "", variant.hypothesis, "",
             "## Pre-registered risk", "", variant.risk, ""]
    if variant.contamination:
        lines += ["## Contamination", "", variant.contamination, ""]
    lines += ["## Result", ""]
    for i, r in enumerate(reps, 1):
        lines.append(f"- repeat {i}: dev {r['dev']['top1']}/{r['dev']['n']}, test "
                     f"{r['test']['top1']}/{r['test']['n']}, wrong-and-unflagged "
                     f"{r['dev']['unflagged_wrong'] + r['test']['unflagged_wrong']}, "
                     f"${r['dev']['cost'] + r['test']['cost']:.2f}")
    lines.append("")
    if decision:
        lines += [f"## Decision: {decision.verdict}", ""]
        lines += [f"- [{'x' if ok else ' '}] {text}" for ok, text in decision.checks]
        lines += [f"- warning: {w}" for w in decision.warnings]
        lines.append("")
    # Dev failures are what the next iteration reads. Test failures are never listed.
    misses = {}
    for p in sorted((RUNS / folder).glob("rep-*.json")):
        for row in json.loads(p.read_text(encoding="utf-8"))["rows"]["dev"]:
            if not row["correct"]:
                misses.setdefault(row["incident_id"], row)
    lines += ["## Dev-split misses (input for the next iteration)", ""]
    if not misses:
        lines.append("None.")
    for incident_id, row in sorted(misses.items()):
        lines.append(f"- **{incident_id}** ({row['service']}): expected *{row['truth_subject']}*, "
                     f"got *{row['verdict_subject'] or row['verdict_sha'] or 'no verdict'}* "
                     f"at {row['confidence']:.0%}")
    lines += ["", f"_Test split ({len(split['test'])} incidents): aggregate only, by design._"]
    (RUNS / folder / "decision.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_status(args: argparse.Namespace) -> int:
    state = _load_state()
    split = json.loads(SPLIT.read_text(encoding="utf-8"))
    print(f"Budget: ${state['spent_usd']:.2f} spent of ${state['budget_usd']:.2f}   "
          f"dev={len(split['dev'])} test={len(split['test'])}   benchmark {state['benchmark']}")
    if not state["iterations"]:
        print("\nNo iterations yet. Next:  py -3.13 eval/hillclimb.py run baseline --repeats 2")
    else:
        print(f"\n{'#':>3}  {'variant':16} {'reps':>4}  {'dev':>6}  {'test':>6}  {'w&u':>4}  "
              f"{'$/inc':>6}  {'s/inc':>6}  decision")
        for it in state["iterations"]:
            print(f"{it['iter']:>3}  {it['variant']:16} {it['repeats']:>4}  "
                  f"{it['dev_top1']:>6.1f}  {it['test_top1']:>6.1f}  {it['wrong_unflagged']:>4.1f}  "
                  f"{it['cost_per_incident']:>6.3f}  {it['sec_per_incident']:>6.1f}  {it['decision']}")
    tried = {it["variant"] for it in state["iterations"]}
    pending = [v for v in variants.VARIANTS.values() if v.name not in tried]
    if pending:
        print("\nPre-registered, not yet run:")
        for v in pending:
            print(f"  {v.name:16} target={v.target or '-':12} {v.hypothesis[:70]}...")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the split and state")
    p.add_argument("--budget", type=float, default=10.0, help="USD ceiling for the campaign")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("run", help="score one variant on dev + test")
    p.add_argument("variant")
    p.add_argument("--repeats", type=int, default=2)
    p.add_argument("--over-budget", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="trajectory so far")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
