# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Python 3.13 / FastAPI service (existing). The demo surface is plain HTML/CSS/JS served by the
same FastAPI app at `/demo`: no build step, no Node toolchain, one command to start.

## Users

- **Primary (the demo):** interviewers and reviewers watching the author screen-share during a
  technical interview. They have a few minutes, are technically literate, and are judging whether
  the system is real, how it reasons, and how carefully its claims are measured.
- **The presenter:** the author, driving the demo live while talking over it. Needs it to start
  fast, never stall, and make each step legible on a shared screen.
- **The system's notional user:** an on-call engineer paged by a production alert, who needs to
  know which change caused it, how many users are hurt, and what to do — the scenario the demo
  plays out.

## Product Purpose

IncidentPilot turns a production alert into a diagnosis: it pulls the commits that landed in the
alert's blast window, ranks them, then a Claude tool-use agent reads the diffs, the service map and
the team's runbooks, names the commit most likely responsible (or none), estimates user impact, and
drafts a Slack brief and a resolution report. The demo exists to show that pipeline working end to
end, step by step, in front of someone.

## Positioning

The claims are measured, not asserted. Every replayed incident has a known answer, the agent is
scored against a non-AI correlation baseline over the same inputs, confidence is checked for
calibration, and replays are pinned to the moment the alert fired so later history cannot leak the
answer. What the demo can show that a slide cannot: the agent overruling the ranker by reading the
diff.

## Operating Context

- Live screen-share in interviews, presenter talking over it; 2-3 minutes per run-through.
- **Replay by default, live on demand.** Replays play back runs recorded earlier: no API key, no
  network, no cost, cannot fail mid-interview. They must be labelled as recordings. A switch runs
  the same incident live against Claude (~$0.05, ~30s) when the presenter wants to prove it is real.
- Started from a terminal on Windows (PowerShell 5.1).

## Capabilities and Constraints

- Inputs: Alertmanager / Datadog / generic webhook payloads.
- Agent: claude-sonnet-5 by default, seven tools (diff, runbook search, re-correlate, file history,
  topology, impact recompute, submit_diagnosis). A verdict can name no commit.
- Outputs: verdict with confidence and a needs-human flag, impact estimate with its formula, Slack
  Block Kit brief (never posted from the demo), markdown resolution report.
- Eval suites: synthetic (20), negative controls (8, no guilty commit), real-world regressions
  (15, mined from reverts in golang/go, moby, prometheus, grafana).
- Slack posting stays off in the demo.

## Evidence on Hand

- Synthetic suite: correlation ranker 7/20; claude-opus-5 18/20; claude-sonnet-5 20/20 on both of
  two runs, $0.045/incident, median 29s, zero wrong-and-unflagged.
- Real-world suite: ranker 3/15. **Agent not yet measured** — do not show or imply an agent score.
- Negative controls: ranker 0/8. **Agent not yet measured.**
- All results are replays, not live production traffic. Do not present it as deployed or as having
  production users.

## Product Principles

1. Show the reasoning, not just the answer: the investigation is the product.
2. Never fabricate: a replay says it is a replay, an unmeasured number stays unmeasured.
3. The baseline is always in frame, so the agent's contribution is visible.
4. Reliability over spectacle in a live setting: nothing the presenter depends on may need the
   network unless they chose live mode.

## Accessibility & Inclusion

Must stay legible on a shared screen at reduced resolution and compression: large enough text,
strong contrast, no meaning carried by colour alone.
