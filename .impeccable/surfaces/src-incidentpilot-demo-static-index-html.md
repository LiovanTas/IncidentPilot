---
version: 1
slug: "src-incidentpilot-demo-static-index-html"
primary_target: "src/incidentpilot/demo/static/index.html"
related_targets: []
---

## Scope

The IncidentPilot demo page at `/demo` (`src/incidentpilot/demo/static/`). Mode: Operate. Built code-led.
Audience: interviewers watching a live screen-share; the presenter drives it. Task: pick an incident,
run it (replay by default, live on demand), and watch the investigation reach a verdict that is checked
against a known answer. Constraints: no build step, works offline (self-hosted fonts, no CDN),
legible over compressed video, replays labelled as recordings, unmeasured numbers never shown.

## Direction contract

THESIS: The investigation plays out on a concourse departure board. The ranker's suspect commits are rows
ranked by score; as Claude reads each diff the row's status flaps over to IDENTIFIED (amber lamp) or
RULED OUT (red, struck through, never removed). It refuses the category default of a dark dashboard of
cards and charts, and the agent-as-chat-transcript.

OWN-WORLD: Steel-frame #2A2D31 panels inside a brushed-steel bezel (#5B5E63 to #A7AAAF); flap-black #0B0B0D
boards built from fixed character cells split by a hairline at mid-height; letter white #F2F2F2. Delay amber
#FFB000 is reserved for the agent's identification, live lamps and the primary action. Cancel red #B00020
(lifted for text contrast) marks ruled-out rows and SEV1. Sofia Sans Condensed caps, widely tracked, on every
flap, label and heading; Sofia Sans for prose; JetBrains Mono only for diffs.

STORY: The presenter picks an incident and presses Run. The audience sees the alert arrive, the shortlist
with the ranker's pick, Claude circling a row and opening its diff, its reasoning lines, statuses flapping
over, the verdict with confidence and impact, then the known answer revealed beside it with time and cost,
and the Slack brief and report a tab away. They come away believing it reads evidence, can overrule the
ranker, and is honest about what was measured.

FIRST VIEWPORT: 1440x900. Left rail 264px: brand, Replay/Live switch, speed, the seven incidents with suite
labels, recording date at the foot. Main: an alert board across the top (SEV flap, title, service, then
elapsed clock, users affected, error rate; the amber Run investigation button at its right end, the
primary action). Below, the suspects board (No., Commit, Change, Landed, Score, Status as the flap column),
about 62% wide, and the investigation board on the right with timestamped steps. The verdict board lands
beneath the suspects board and scrolls into view.

FORM: Departure Board (split-flap concourse), the lead of the bolder re-roll, round 2. seed:414d6060d136c71b.
Signature interaction: a per-character flap cascade on every status change; the row being read is ringed
and opens to show its diff, then closes to one line. Motion grammar: flaps only, 2-3 intermediate
characters per cell, under 350ms, staggered by cell; lamps pulse; nothing else moves. Reduced motion sets
every flap instantly.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
