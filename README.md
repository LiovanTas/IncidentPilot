# IncidentPilot

An autonomous incident-response agent. It receives a production alert, works out which
commit caused it, estimates how many users are affected, posts a structured Slack brief,
and writes a resolution report — replacing the manual triage an on-call engineer would
otherwise assemble by hand from Grafana, `git log` and the runbook wiki.

Built on the Anthropic API (`claude-opus-5`) with tool use, RAG over a runbook corpus,
and git correlation. Ships with a 20-incident replay harness so the accuracy claim is a
measurement rather than an assertion.

---

## How it works

```
  alert webhook                 correlation                  diagnosis                output
  ─────────────                 ───────────                  ─────────                ──────
  Alertmanager ─┐
  Datadog ──────┼─▶ normalize ─▶ git blast window ─┐
  generic ──────┘    (Alert)     + 4-signal rank   │
                                                   ├─▶ Claude agent loop ─▶ Slack Block Kit brief
                     runbook RAG ──────────────────┤   (7 tools, reads       resolution report (md)
                     (BM25 + dense, RRF)           │    diffs, cites          structured JSON
                                                   │    runbooks)
                     impact model ─────────────────┘
                     (topology + traffic)
```

Six stages, each independently testable:

| Stage | Module | What it does |
| --- | --- | --- |
| Ingest | `ingest.py`, `server.py` | Normalizes Alertmanager / Datadog / generic webhooks into one `Alert` shape |
| Correlate | `gitctx.py` | Pulls commits from the blast window and ranks them on recency, path ownership, keyword overlap and risk class |
| Retrieve | `rag/` | Hybrid BM25 + dense retrieval over the runbook corpus, fused with Reciprocal Rank Fusion |
| Diagnose | `agent/` | Claude tool-use loop that reads diffs, queries topology, and commits to a verdict |
| Quantify | `impact.py` | Affected-user estimate with an uncertainty band, plus error-budget burn |
| Publish | `slack.py`, `report.py` | Block Kit brief and a markdown resolution report with the full investigation trace |

### The correlation ranker is a floor, not the answer

Every commit in the window is scored on four signals, each of which emits a
human-readable rationale:

| Signal | Weight | What it measures |
| --- | --- | --- |
| `recency` | 0.30 | Exponential decay from alert onset. A commit that landed *after* the alert fired scores zero outright. |
| `ownership` | 0.34 | Fraction of changed files owned by the alerting service, its upstream dependencies, or a shared library |
| `keyword` | 0.21 | Overlap between alert text and the commit's subject, body and paths |
| `risk` | 0.15 | Risk class of the change (migration, config, dependency bump, timeout, concurrency…) plus churn |

This is the control arm. On the replay corpus it gets the right commit **35% of the
time** — but it puts the right commit in its **top 3 on every single incident**. The
agent's job is the discrimination the ranker cannot do: reading the diff and asking
whether the mechanism in it actually produces *this* symptom in *this* service.

### The agent

A hand-written tool-use loop rather than the SDK tool runner, because the resolution
report needs the full tool transcript and the eval needs a hard per-incident turn
ceiling. It runs `claude-opus-5` with adaptive thinking, configurable effort, streamed
turns, and a prompt-cache breakpoint on the static system prompt.

Seven tools:

| Tool | Purpose |
| --- | --- |
| `get_commit_diff` | Read a candidate's diff — the primary evidence |
| `search_runbooks` | Hybrid retrieval over the runbook corpus |
| `list_commit_candidates` | Re-correlate with a wider window or from another service's perspective |
| `get_file_history` | Recent commits touching a path |
| `get_service_topology` | Dependencies, callers, blast radius, ownership |
| `estimate_user_impact` | Recompute impact with corrected parameters |
| `submit_diagnosis` | Terminal verdict, `strict: true` so the shape is guaranteed |

The agent is told the ranking is a heuristic that is *often wrong about which of the top
few is the real culprit*, and that a confident wrong answer costs the on-call engineer
more than an honest "I narrowed it to these two" — so `needs_human` is a first-class
outcome, not a failure mode.

---

## Quickstart

```bash
pip install -e ".[dev]"
```

Build the replay corpus (a synthetic 62-commit monorepo plus 20 incidents with known
ground truth):

```bash
python eval/build_fixture_repo.py
```

Run one incident end to end, without spending any tokens:

```bash
INCIDENTPILOT_REPO=eval/fixtures/repo python -m incidentpilot.cli replay examples/alertmanager-checkout-5xx.json --no-agent
```

With the agent:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
INCIDENTPILOT_REPO=eval/fixtures/repo python -m incidentpilot.cli replay examples/alertmanager-checkout-5xx.json
```

Reports, structured JSON and the Slack payload land in `out/`.

Check resolved configuration at any time:

```bash
python -m incidentpilot.cli doctor
```

### As a service

```bash
docker compose up --build
```

```bash
curl -X POST localhost:8080/alerts \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $INCIDENTPILOT_WEBHOOK_TOKEN" \
  -d @examples/alertmanager-checkout-5xx.json
```

The webhook validates, normalizes and returns `202` immediately, then diagnoses in a
background task — Alertmanager retries anything that does not answer fast. Fetch the
report with `GET /incidents/{incident_id}`, and check `GET /readyz` for the resolved
config, corpus size and whether the agent is enabled.

---

## The eval

`eval/` builds a real git repository: nine services, shared libraries, deploy manifests
and SQL migrations, with 62 commits authored across three weeks. Twenty of those commits
are ground-truth incident causes.

The corpus is built to be hard in the ways real triage is hard:

- **Decoys are designed to beat the ranker.** Most incidents include a commit that lands
  closer to onset, touches only the alerting service's own files, and carries a
  risk-flavoured message — while the true cause sits further back.
- **Five causes are not in the alerting service at all.** A shared HTTP client timeout,
  a shared cache namespace, a serialization default, a migration owned by another team,
  a dependency bump visible only in a lockfile.
- **Windows overlap.** Neighbouring incidents' commits fall inside each other's 72-hour
  correlation windows, so no window is artificially clean.

```bash
python eval/run_eval.py --mode heuristic      # free, deterministic control arm
python eval/run_eval.py --mode both           # adds the agent arm (needs an API key)
python eval/run_eval.py --mode agent --limit 5
```

Scored metrics:

| Metric | Meaning |
| --- | --- |
| `top1` | The verdict names the ground-truth commit. **The headline number.** |
| `top3` | Ground truth is in the ranker's top 3 — the retrieval ceiling |
| `recall` | Ground truth appears anywhere in the candidate list |
| `MRR` | Mean reciprocal rank of ground truth |

It also reports **calibration**, because being wrong confidently is a worse failure than
being wrong loudly. Three numbers, and they have to be read together:

- `unflagged_and_wrong` — wrong verdicts the system stood behind. The dangerous error.
- `flagged_and_correct` — right verdicts escalated anyway. The cost, and the reason
  `unflagged_and_wrong` cannot be gamed by flagging everything.
- `discrimination_gap_pp` — accuracy among verdicts the system stood behind, minus
  accuracy among those it escalated. **At or below zero, the confidence number is
  worthless** no matter how the other two look.

### Why the heuristic arm always defers

The ranker reports ~0.35–0.45 confidence and flags every verdict for review. That is not
timidity, it is a measurement. Two earlier confidence formulas were tried — one keyed on
absolute score, one on the leader-to-runner-up margin — and both were uncalibrated; the
margin version was measurably *worse* (wrong-and-unflagged went 9 → 12, and the confidence
bands inverted, with sub-0.70 verdicts scoring 42.9% against 30.8% above).

The cause is in the data. Across the corpus the margin is **0.061 when the ranker is right
and 0.053 when it is wrong** — the distributions overlap almost entirely, so no monotone
function of (score, margin) can separate them. Deciding between two plausible commits
requires knowing what their diffs *do*, and the ranker only ever sees commit metadata.

So the baseline's honest job is: generate candidates at 100% top-3 recall, and defer.
Discriminating is what the agent is for — and whether it actually does is exactly what
`discrimination_gap_pp` on the agent arm will show.

Results are written to `eval/results/` as JSON plus a markdown summary, with per-incident
rows and token cost.

### Current numbers

| Arm | top-1 | top-3 (retrieval) | recall | MRR | wrong & unflagged | cost |
| --- | --- | --- | --- | --- | --- | --- |
| heuristic | 7/20 (35%) | 20/20 (100%) | 20/20 | 0.608 | 0 — defers on all 20 | $0.00 |
| agent | *not yet measured* | 20/20 (100%) | 20/20 | 0.608 | *not yet measured* | — |

The heuristic arm is deterministic and reproducible. The agent arm has not been run yet;
run it and paste the result here rather than assuming one.

---

## Configuration

All configuration is environment-driven. See `.env.example`.

| Variable | Default | Notes |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | — | Unset means heuristic-only mode. Everything still runs. |
| `INCIDENTPILOT_MODEL` | `claude-opus-5` | |
| `INCIDENTPILOT_EFFORT` | `high` | `low`–`max` |
| `INCIDENTPILOT_MAX_TURNS` | `14` | Turn ceiling per incident; exceeding it falls back to the heuristic and flags for review |
| `INCIDENTPILOT_REPO` | fixture repo | Repository under investigation. Read-only. |
| `INCIDENTPILOT_LOOKBACK_HOURS` | `72` | Correlation window |
| `VOYAGE_API_KEY` | — | Enables Voyage embeddings; otherwise a deterministic hashed-TF-IDF backend is used |
| `SLACK_BOT_TOKEN` | — | |
| `INCIDENTPILOT_SLACK_POST` | `0` | **Posting requires both this and a token.** |
| `INCIDENTPILOT_WEBHOOK_TOKEN` | — | Bearer token for `/alerts`; unset disables auth |

### Slack posting is off by default

The brief is *always* written to `out/<incident_id>.slack.json` as the exact Block Kit
payload that would be sent, so it can be reviewed or pasted into Block Kit Builder
without touching a workspace. Delivery requires explicitly setting
`INCIDENTPILOT_SLACK_POST=1` **and** providing a token. Neither alone is enough.

### Retrieval runs offline

The default embedding backend is a deterministic feature-hashing projection over
unigrams and bigrams — no network, no model download, stable across runs. It is not a
semantic model; it captures lexical overlap, which combined with BM25 through RRF is
enough for runbook retrieval. Set `VOYAGE_API_KEY` to swap in real dense embeddings. The
eval is reproducible on a laptop and in CI either way.

---

## Tests

```bash
pytest
```

44 tests covering payload normalization across three providers, impact-model
monotonicity and calibration, commit ranking invariants (including that a commit landing
after the alert can never score), retrieval quality against known-correct runbooks, the
agent loop driven by a fake client (verdict path, nudge-on-no-tool-call, turn-budget
exhaustion, refusal handling, beta-fallback degradation), Block Kit validity, and
dry-run enforcement.

One test asserts that ground truth is retrieved for all 20 incidents — the agent's
ceiling. If that ever regresses, the ranker broke, not the model.

---

## Design notes

**Why a manual loop rather than the SDK tool runner.** The report needs the full tool
transcript with per-call timings, and the eval needs a hard turn ceiling so one
pathological incident cannot spend the whole budget. The tool runner's hooks could cover
approval gating, but not these two together, cleanly.

**Why the system prompt has no incident data in it.** Tools render before `system`, which
renders before `messages`. Keeping the system prompt byte-stable and putting everything
incident-specific in the first user turn means a batch of incidents reads the cache
instead of re-paying for the prefix. A test asserts the system prompt contains no
volatile content.

**Why the impact model is arithmetic rather than a model call.** The number gets quoted
in a Slack brief that a human acts on, so the method has to be inspectable. Every
estimate ships with the formula that produced it, including the
`P(user hit) = 1-(1-delta)^n` step and the downstream propagation factor.

**Why the eval has a free control arm.** Without the heuristic baseline, "the agent got N
right" is unfalsifiable — it could be the ranker doing the work. Running both arms over
the same corpus separates retrieval from discrimination.

---

## Layout

```
src/incidentpilot/
  config.py        env-driven configuration
  models.py        domain objects (Alert, CommitCandidate, Diagnosis, ImpactEstimate…)
  ingest.py        provider payload normalization
  gitctx.py        blast-window collection + explainable ranking
  rag/             chunking, embeddings, SQLite hybrid retriever
  agent/           prompts, tool definitions + dispatch, the loop
  impact.py        affected-user and error-budget estimation
  slack.py         Block Kit brief, dry-run by default
  report.py        markdown resolution report
  pipeline.py      orchestration
  server.py        FastAPI webhook receiver
  cli.py           index / search / replay / serve / doctor
eval/
  scenarios.py           the corpus: baseline tree + 20 incidents with decoys
  build_fixture_repo.py  materializes it as a real git repo
  make_runbooks.py       generates the runbook corpus
  run_eval.py            scores both arms
runbooks/          12 runbooks
data/topology.json service graph, traffic volumes, SLO
examples/          sample webhook payloads
```

---

## Repo setup

After a fresh clone, set the commit identity for this repo and enable the hook:

```bash
git config user.name  "Your Name"
git config user.email "you@example.com"
git config core.hooksPath .githooks
```

`.githooks/pre-commit` then refuses any commit whose resolved author or committer does not
match this repo's configured `user.email`. Repo-local config alone is not enough:
`GIT_AUTHOR_EMAIL` and `GIT_COMMITTER_EMAIL` environment variables silently override it,
and IDEs, CI and agent tooling set them.
