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

**1. A real-world held-out set from revert commits.** *Highest leverage.*
When someone commits "Revert X", X is a developer-labelled bad change. Measured:
grafana (439 reverts, 67% with a machine-readable `This reverts commit <sha>`), moby
(323, 54%), prometheus (101, 40%) — roughly 500 real pairs from three service-shaped
repos. Build a harness that mines the pairs, synthesises a *symptom-only* alert from each
revert's stated reason (withholding anything that names the change), and scores against
real history. The hard part is the alert: revert messages often name the fix, which
would leak the answer.

**2. Negative controls.**
Every current incident has a guilty commit, so a tool that always blames *something* is
never penalised for it. Most real alerts aren't caused by a deploy — traffic spikes,
upstream outages, a full disk. Add incidents with no causal commit in the window; the
correct answer is to name none and escalate.

**3. Harder synthetic cases.**
Multi-commit causes (two changes that are only broken together), causes older than the
72h window, merge commits, a revert of a revert, and commit messages written in more than
one author's voice — all twenty current incidents were written by one person, in one style.

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
