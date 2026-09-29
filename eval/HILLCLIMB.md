# Improving IncidentPilot: the iterative process

How changes to the agent get proposed, measured, and kept or thrown away — and the
backlog of changes worth trying.

The short version: **one change at a time, written down before it runs, judged by a
rule that was also written down before it runs.** Everything below exists to stop the
two ways this kind of work usually goes wrong: tuning to the test set until the numbers
mean nothing, and reading noise as progress.

---

## The loop

Each turn of the loop is one command, `eval/hillclimb.py run <variant>`.

| Step | What happens | Where it lives |
| --- | --- | --- |
| **Read** | Look at the *dev-split* failures from the last iteration | `hillclimb/runs/NN-*/decision.md` |
| **Propose** | Pick one change. Write its hypothesis, target metric and risk into `variants.py`. Commit. | `eval/variants.py` |
| **Apply** | Variants are config overrides — main code never changes per experiment | `load_config(**overrides)` |
| **Run** | Score the variant on dev *and* test, K repeats each | `hillclimb.py run` |
| **Record** | Metrics, per-repeat results and the decision, all committed | `hillclimb/state.json`, `scores.tsv` |
| **Decide** | The rule below says KEEP or REJECT. Nobody argues with it after the fact. | `hillclimb.decide()` |

```powershell
py -3.13 eval/hillclimb.py status                          # where things stand, what's pending
py -3.13 eval/hillclimb.py run baseline --repeats 2        # first: measures noise
py -3.13 eval/hillclimb.py run effort-medium --repeats 2   # then one variant at a time
```

A KEEP means the override becomes the new default in `config.py` and the next
iteration's baseline is re-run with it. A REJECT means the variant stays in the registry
as a record of what didn't work.

---

## The rules

These are enforced in code where they can be, and written down where they can't.

**1. Improvements are judged on dev. Test is only a gate.**
The 20 incidents are split 12 dev / 8 test by a seeded, stratified draw
(`hillclimb/split.json`). A change must *improve* dev and must *not regress* test. Only
the test split's aggregate is ever printed — its individual failures are never shown,
because the moment you choose a change by looking at test failures, test becomes a
second dev set and stops measuring anything.

**2. Noise is measured, not assumed.**
The baseline runs at least twice. The spread between its repeats is the noise band, and
a change has to beat it. With 12 dev incidents, one flip is 8 percentage points — a
"gain" of one incident is usually the model rolling differently, not the change working.

**3. The safety ratchet.**
No change may increase *wrong and unflagged* verdicts — answers that are wrong and
presented as settled. Worst case is compared against worst case, so noise cannot hide a
regression. A variant that is 40% cheaper and occasionally confidently wrong is rejected.

**4. Each variant does one thing.**
Two changes in one variant make it impossible to say which one helped.

**5. Pre-register everything.**
Hypothesis, target metric and expected risk go into `variants.py` and get committed
*before* the run. A result can then confirm or refute a prediction that already exists,
instead of being explained afterwards.

**6. The benchmark is frozen for the life of a campaign.**
Editing an incident invalidates every comparison against the baseline. The harness
fingerprints `incidents.json` and refuses to run if it has changed.

### What's off limits

- **Relabelling an incident because the model got it wrong.** A fixture change needs a
  reason that holds *without* the model's answer — its labelled cause demonstrably cannot
  produce its own symptom, say. It gets recorded in the commit message and starts a new
  campaign. (INC-20 was fixed on exactly this basis: its labelled cause changed datetime
  encoding, and its symptom was about amounts. INC-08, where the model simply lost a
  genuine judgement call, was deliberately left alone.)
- **Tuning thresholds on test.** See the `escalate-075` contamination note below.
- **Running over budget.** The harness refuses unless told otherwise.

---

## Statistical power: what this benchmark can and can't decide

Honest limits, stated up front so nobody over-reads a result:

| Question | Decidable here? | Why |
| --- | --- | --- |
| Is it cheaper at the same accuracy? | **Yes** | Cost and latency are measured continuously on every incident |
| Is it better calibrated? | **Partly** | Only two known misses to calibrate against |
| Is it more accurate? | **No** | Dev scored 12/12 last run — there is no room above the ceiling to show a gain |

The seeded split happened to put both known misses (INC-08, INC-20) in the test half.
It was not redrawn: choosing a split after seeing where the outcomes fall is
cherry-picking. The consequence is that accuracy variants on this benchmark will REJECT
with an *unmeasurable* warning, which means "can't tell", not "doesn't work".

**Answering accuracy questions needs a harder benchmark** — which is why it's first in
the backlog.

**Update after iteration 1:** the Sonnet 5 baseline scored 20/20 on both repeats — 12/12 dev
and 8/8 test each time, with zero wrong answers. Accuracy is now at the ceiling on *both*
splits, and with no wrong answers there is nothing to calibrate against either. On this
benchmark only cost and latency variants can produce a meaningful result. `opus-5`,
`read-file` and `escalate-075` should not be run until the benchmark is harder: each can
only come back 'unmeasurable' or lose, and every run costs money.

Also note the split was drawn after one full run over all 20 incidents had already been
inspected. The test split is held out from iteration decisions from here on; it was not
held out from all prior observation.

---

## Budget

At ~$0.15 per incident, one repeat of all 20 is ~$3. A campaign of baseline ×2 plus four
variants ×2 is roughly **$30**, which is the default ceiling in `state.json`.

---

## The backlog

Ordered by leverage. Tier 1 comes first because the loop can't answer accuracy questions
until it exists.

### Tier 1 — Better measurement

**1. A real-world held-out set from revert commits.** *Built — `eval/realworld/`.*
When someone commits "Revert X", X is a developer-labelled bad change. The builder mines
those pairs from seven public repositories, turns each revert's stated reason into a
symptom-only alert, pins the repository to one hour before the revert, and keeps the pair
only if the guilty commit landed inside the window the tool searches. Every rule is in
`build.py`, and `FUNNEL.md` counts what each one dropped — nothing is hand-picked.

Reading the first build's alerts against their answers is what shaped the rules. Each
failure found became a general rule and a regression test:

| Found in the alerts | Rule it became |
| --- | --- |
| Moby's `Docker-DCO-1.1-Signed-off-by` lines passed as the "reason" | Sign-offs are removed in any form |
| `* Revert "Update dependency @types/core…"` named the answer — a bullet hid it from the filter, and scrubbing `@types` hid it from the leak check | Bullets stripped before filtering; the answer's subject is scrubbed identically before comparing |
| Grafana squash-merges its reverts, so the "reason" was the PR's commit list: the fix, the answer reworded, or unrelated work | An alert must describe a failure; lines shaped like commit subjects are dropped even if they mention one |
| "Likely due to CL 564197" — the guilty commit's own message says `Reviewed-on: …/564197` | Gerrit change numbers are scrubbed |
| A squashed fix PR matched because one of its bullets was a revert | The reverting commit must itself be a revert |
| Two reverts shared one explanation — one alert, two different answers | Duplicate alerts are dropped |
| A window of one commit, whose only suspect was the answer | At least 10 commits in the window |

Also checked: alerts that name the guilty commit's author are rejected.

The strict rules keep few pairs: most reverts never say *why*, and most that do restate the
change. golang/go supplies the most, because Gerrit's revert template records a "Reason
for revert:". That skews the suite towards compiler and build regressions — read its
results as regression attribution in real codebases, not as production incidents.

Two infrastructure traps worth knowing if you extend it. The clones are metadata-only
(`--filter=blob:none`), so asking git about a commit that is not in local history makes it
silently fetch that commit's entire ancestry from GitHub: one run pulled 47 near-complete
copies of grafana's history, 7 GB, before it was caught. `GIT_NO_LAZY_FETCH=1` on every
metadata query prevents it. And the builder pre-fetches each window's file contents, or the
eval would measure GitHub's download speed instead of the agent.

**2. Negative controls.** *Built — `eval/make_negatives.py`.*
Eight alerts with no guilty commit: a provider outage, an expired certificate, a traffic
spike, a database failover, an exhausted email quota, a full disk, an availability-zone
fault, a malformed third-party feed. The right answer is to name no commit. Each window is
chosen by rule — the one with the most commits in which nothing the service depends on
changed — and the label is asserted before it is written. checkout-api and web-frontend get
none: they depend on so much that no such window exists, and an indefensible label is worse
than a missing one.

The correlation ranker blames an innocent commit on all eight, which is the point.

**3. Harder synthetic cases.**
Multi-commit causes (two changes that are only broken together), causes older than the
72h window, merge commits, a revert of a revert, and commit messages written in more than
one author's voice — all twenty current incidents were written by one person, in one style.

### Replay protocol v2 — the campaign above needs a new baseline

Building the real-world suite exposed that replays could see the future: file-history
lookups ran from HEAD, and a real repository's later history contains the revert that names
the answer. Every incident is now pinned to its onset, and commits are dated by when they
landed on the main line rather than when they were written. The synthetic scores are
unchanged by it, but what the agent can see during a replay did change, so the hill-climb
fingerprint now includes the protocol version and refuses to compare new variants against
the protocol-1 baseline. Start a new campaign before running any variant.

### Tier 2 — Agent changes, pre-registered and ready to run

| Variant | Target | Hypothesis | Risk |
| --- | --- | --- | --- |
| `effort-medium` | cost | Holds accuracy, cuts output tokens ~⅓ | Cross-service cases need the extra thinking |
| `prefetch-3` | latency | Truth is always in the top 3; inlining those diffs saves ~3 round-trips | Anchors the agent; INC-20 needed it to widen the window |
| `read-file` | accuracy | Seeing what a constant controls settles INC-08-type calls | **Unmeasurable** on this benchmark (dev at ceiling) |
| `escalate-075` | calibration | Both misses were ≤0.68, every hit ≥0.78 | **Contaminated**: threshold chosen after seeing all 20, test included |

Full hypotheses are in `eval/variants.py`.

### Tier 3 — Agent changes worth building

- **Observation accounting.** Make `submit_diagnosis` map every observation in the alert
  ("database CPU flat", "began exactly at the deploy") to the evidence that explains it.
  That is the reasoning that caught the INC-20 labelling bug; requiring it should make
  the agent reject candidates that explain the headline symptom but not the details.
- **Multi-commit verdicts.** The terminal tool accepts exactly one SHA. Real incidents are
  sometimes two changes that are only broken in combination.
- **Self-consistency.** Diagnose three times; disagreement triggers escalation. A strong
  calibration signal, at triple the cost.
- **Searching inside large diffs.** Diffs are truncated at 6,000 characters, which can cut
  off the one line that matters.

### Tier 4 — Realism gaps this benchmark can't measure at all

- **Deploy time, not author time.** Recency is currently scored from when a commit was
  *written*. Incidents correlate with when it was *deployed*. A commit authored Monday and
  shipped Thursday is scored as three days old. This is the largest gap between the
  benchmark and a real production environment.
- **A metrics and logs tool.** Real responders check dashboards before reading diffs.
- **Responder feedback.** A 👍/👎 on the Slack brief turns every real incident into a
  labelled example. Long term, this is the only source of ground truth that reflects the
  real distribution of incidents, and it's the natural endpoint of everything above.
