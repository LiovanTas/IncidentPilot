"""Pre-registered agent variants for the hill-climb.

Every variant is ONE change from baseline, with its hypothesis, target metric and
expected risk written down before it is run. That ordering is the point: a result can
only confirm or refute a prediction that already exists, instead of being explained
after the fact.

Adding a variant: append to VARIANTS, fill in every field, commit it, *then* run it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TARGETS = ("accuracy", "cost", "latency", "calibration")


@dataclass(frozen=True)
class Variant:
    name: str
    overrides: dict[str, Any]
    target: str | None          # which metric this change is meant to move; None for baseline
    hypothesis: str
    risk: str
    contamination: str = ""     # anything that makes its test-split result less than independent
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.target is not None and self.target not in TARGETS:
            raise ValueError(f"{self.name}: target must be one of {TARGETS}")


VARIANTS: dict[str, Variant] = {v.name: v for v in [
    Variant(
        name="baseline",
        overrides={},
        target=None,
        hypothesis="The configuration that scored 18/20. Every other variant is judged "
                   "against it, and its repeated runs set the noise band.",
        risk="n/a",
    ),
    Variant(
        name="effort-medium",
        overrides={"effort": "medium"},
        target="cost",
        hypothesis="Most incidents need two or three diffs read and one matched to a "
                   "symptom. Medium effort should hold accuracy while cutting output tokens "
                   "(the expensive side, at $25/M) by roughly a third.",
        risk="The cross-service cases -- where the cause is in a shared library and the "
             "agent has to reason about who calls it -- are the ones most likely to need the "
             "extra thinking. Watch INC-06, INC-07 and INC-19 specifically.",
    ),
    Variant(
        name="prefetch-3",
        overrides={"prefetch_diffs": 3},
        target="latency",
        hypothesis="The true cause is in the ranker's top 3 on all 20 incidents, and the "
                   "agent spends about three of its ~7 tool calls fetching exactly those "
                   "diffs, one model round-trip each. Inlining them in the first prompt "
                   "removes those round-trips: expect 25-35% lower latency.",
        risk="Two ways to lose. Input tokens rise and are not cacheable (they are "
             "incident-specific), so cost may go up. And handing it three diffs up front may "
             "anchor it on those three -- INC-20 was only solved by the agent widening its "
             "own search window, which it may stop doing.",
    ),
    Variant(
        name="read-file",
        overrides={"enable_read_file": True},
        target="accuracy",
        hypothesis="A diff shows which lines changed, not what they control. The INC-08 miss "
                   "was a judgement between two plausible OOM causes; seeing the whole config "
                   "file and how each value is consumed should settle cases like it.",
        risk="More tool calls, so higher cost and latency. And with the baseline at 18/20 "
             "there is almost no headroom on this benchmark: an accuracy gain here is likely "
             "to be indistinguishable from noise. See HILLCLIMB.md, 'statistical power'.",
    ),
    Variant(
        name="escalate-075",
        overrides={"escalate_below": 0.75},
        target="calibration",
        hypothesis="On the full run both misses were at <= 0.68 confidence and every hit was "
                   "at >= 0.78. A 0.75 review threshold should flag both misses while "
                   "escalating no correct answers.",
        risk="Needlessly escalating correct answers, which is the on-call time the tool "
             "exists to save. INC-07 was correct at 0.78 -- very close to the line.",
        contamination="The 0.75 threshold was chosen AFTER seeing the confidence of all 20 "
                      "incidents, test split included. Its test result is therefore not "
                      "independent evidence. Do not adopt it on this benchmark alone; confirm "
                      "it on the real-world revert set first.",
    ),
]}


def get(name: str) -> Variant:
    try:
        return VARIANTS[name]
    except KeyError:
        known = ", ".join(VARIANTS)
        raise SystemExit(f"unknown variant '{name}'. Known: {known}") from None
