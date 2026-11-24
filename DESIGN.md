---
name: IncidentPilot
description: An incident investigation played out on a split-flap concourse departure board.
colors:
  delay-amber: "#FFB000"
  amber-hover: "#FFC23D"
  amber-ink: "#1A1300"
  cancel-red: "#B00020"
  cancel-red-text: "#FF5A6E"
  diff-green-text: "#7FD99A"
  flap-black: "#0B0B0D"
  flap-cell: "#17181B"
  frame-steel: "#2A2D31"
  frame-deep: "#222428"
  rule-line: "#34373C"
  steel-mid: "#5B5E63"
  steel-light: "#A7AAAF"
  unlit-grey: "#8C9095"
  letter-white: "#F2F2F2"
typography:
  display:
    fontFamily: "Sofia Sans Condensed, Arial Narrow, sans-serif"
    fontSize: "30px"
    fontWeight: 700
    lineHeight: 1.05
    letterSpacing: "0.06em"
  flap:
    fontFamily: "Sofia Sans Condensed, Arial Narrow, sans-serif"
    fontSize: "20px"
    fontWeight: 600
    lineHeight: 1
  headline:
    fontFamily: "Sofia Sans, Segoe UI, sans-serif"
    fontSize: "24px"
    fontWeight: 600
    lineHeight: 1.3
  title:
    fontFamily: "Sofia Sans Condensed, Arial Narrow, sans-serif"
    fontSize: "17px"
    fontWeight: 600
    letterSpacing: "0.14em"
  body:
    fontFamily: "Sofia Sans, Segoe UI, sans-serif"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.45
    fontFeature: "tnum"
  label:
    fontFamily: "Sofia Sans Condensed, Arial Narrow, sans-serif"
    fontSize: "13px"
    fontWeight: 600
    letterSpacing: "0.14em"
  mono:
    fontFamily: "JetBrains Mono, Consolas, monospace"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.55
rounded:
  none: "0px"
  cell: "2px"
spacing:
  hair: "4px"
  xs: "8px"
  sm: "10px"
  md: "14px"
  lg: "18px"
  xl: "28px"
components:
  button-primary:
    backgroundColor: "{colors.delay-amber}"
    textColor: "{colors.amber-ink}"
    typography: "{typography.title}"
    rounded: "{rounded.none}"
    padding: "13px 20px"
  button-primary-hover:
    backgroundColor: "{colors.amber-hover}"
    textColor: "{colors.amber-ink}"
  button-primary-disabled:
    backgroundColor: "{colors.steel-mid}"
    textColor: "{colors.flap-black}"
  mode-switch-on:
    backgroundColor: "{colors.letter-white}"
    textColor: "{colors.flap-black}"
    rounded: "{rounded.none}"
    padding: "9px 0"
  mode-switch-off:
    backgroundColor: "{colors.flap-black}"
    textColor: "{colors.steel-light}"
    rounded: "{rounded.none}"
    padding: "9px 0"
  select:
    backgroundColor: "{colors.flap-black}"
    textColor: "{colors.letter-white}"
    rounded: "{rounded.none}"
    padding: "8px 10px"
  incident-item:
    backgroundColor: "{colors.frame-deep}"
    textColor: "{colors.letter-white}"
    rounded: "{rounded.none}"
    padding: "5px 10px"
  incident-item-current:
    backgroundColor: "{colors.flap-black}"
    textColor: "{colors.letter-white}"
  flap-cell:
    backgroundColor: "{colors.flap-cell}"
    textColor: "{colors.letter-white}"
    typography: "{typography.flap}"
    rounded: "{rounded.cell}"
    width: "19px"
    height: "30px"
  flap-cell-sev:
    backgroundColor: "{colors.cancel-red}"
    textColor: "{colors.letter-white}"
  chip:
    backgroundColor: "{colors.frame-steel}"
    textColor: "{colors.steel-light}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "3px 9px"
  board-panel:
    backgroundColor: "{colors.frame-steel}"
    textColor: "{colors.letter-white}"
    rounded: "{rounded.none}"
  board-body:
    backgroundColor: "{colors.flap-black}"
    textColor: "{colors.letter-white}"
    padding: "6px 10px 10px"
---

# Design System: IncidentPilot

## Overview

**Creative North Star: "The Departure Board"**

The investigation plays out on a concourse split-flap board. Suspect commits are departures, ranked by score; as the agent reads each diff, the row's status flaps over to IDENTIFIED under an amber lamp or to RULED OUT in red, struck through and left on the board. Everything sits on steel: dark frame panels set into a brushed-steel bezel, with flap-black boards inside them built from fixed character cells split by a hairline seam at mid-height. Information is read the way a traveller reads a board: condensed capitals, wide tracking, tabular figures, one row per thing.

The surface is an Operate tool, driven live by a presenter over a screen share at 1440x900 and read by an audience through compressed video. That sets the density and the contrast: large condensed caps, letter white on near-black, no meaning carried by colour alone (ruled-out rows are struck through as well as red; identified rows carry a lamp as well as amber). Replay is the default mode and every recording says it is a recording, in the mode switch, the run chip, the run note and the rail foot. (The "scripted stand-in" recording visible in the review captures is scratch-only; real replays come from `incidentpilot demo-record`.)

Motion has one grammar. Flaps flap; lamps pulse; the diff drawer opens and closes. Nothing else moves. The board refuses the category default of a dark dashboard of cards and charts, and the agent-as-chat-transcript.

**Key Characteristics:**
- Split-flap cells are the system's atom: severity, the elapsed clock, commit hashes, statuses, the probable cause and confidence all render as flaps.
- Steel frame, flap-black board, letter white: three material layers, no others.
- Amber is the agent's colour and the board's lamp; red is cancellation.
- Condensed caps for anything the board would print; plain sans for sentences a person reads; mono only for code.
- Square corners everywhere except the 2px flap cell.
- No shipping rasters: the brushed steel is inline SVG fractal noise over a gradient.

**Open opportunities (not built):** hinge pins on the flap cells, a large flap headline, flap tiles for the stat numbers, and a lit bezel frame. These fit the world but are not part of the shipped system; build one only as a deliberate system change.

## Colors

A cold steel-and-black board lit by one warm lamp colour and one cancellation red.

### Primary
- **Delay Amber** (delay-amber): the agent's voice and the board's lamp. Used for the IDENTIFIED status and row tint, the ring around the row being read, the CLAUDE reasoning steps in the investigation log, lamps, the evidence bullets, a matched known answer, the needs-human flag, and the primary Run investigation button. Also carries the system's focus ring, text selection and current-selection markers (the selected incident's border, the active tab's underline).
- **Amber Ink** (amber-ink): text on amber fills only.

### Secondary
- **Cancel Red** (cancel-red): the SEV flap fill, error banner border, and a missed known answer's border. A fill colour, not a text colour.
- **Cancel Red, lifted** (cancel-red-text): red as text, lifted for contrast on flap-black. RULED OUT flaps, ruled-out change text (always struck through at 2px), deleted diff lines.

### Tertiary
- **Diff Green** (diff-green-text): added lines inside diffs only. Not a success colour; success on this board is amber.

### Neutral
- **Flap Black** (flap-black): every board body, the investigation log, inputs, code wells.
- **Flap Cell** (flap-cell): the upper half of a flap cell; the lower half darkens to #121315 so the seam reads.
- **Frame Steel** (frame-steel): panel frames that hold the boards. **Frame Deep** (frame-deep) for unselected list items set into a frame.
- **Rule Line** (rule-line): dividers between board head and body, tabs, footers.
- **Steel Mid** (steel-mid) and **Steel Light** (steel-light): the bezel gradient stops, input and chip strokes, and steel-light doubles as the secondary text colour for labels, column heads and metadata.
- **Unlit Grey** (unlit-grey): flaps and rows that have not been read yet, and disabled controls.
- **Letter White** (letter-white): flap characters and primary text. Never pure #FFF on the board.

### Named Rules
**The One Lamp Rule.** Amber marks the agent and what it is doing (reading, identifying, reasoning, a match), the lamps, the primary action, and where focus or selection currently sits. It never decorates a heading, a border at rest, or a neutral control.

**The Never Removed Rule.** A ruled-out suspect stays on the board in red with its change text struck through. Red plus strike-through, never red alone, and never a disappearing row.

## Typography

**Display Font:** Sofia Sans Condensed (with Arial Narrow)
**Body Font:** Sofia Sans (with Segoe UI)
**Label/Mono Font:** JetBrains Mono (with Consolas), diffs and commands only

**Character:** A transit board's condensed caps, widely tracked, carry everything the board prints; a humanist sans of the same family carries the sentences the agent writes. All three faces are self-hosted woff2 with no CDN, so the demo runs offline.

### Hierarchy
- **Display** (700, 30px, 1.05, 0.06em, uppercase): the alert title on the alert board; also the stat values (700, 30px, 0.04em). 24px at phone width.
- **Flap** (600, 20px in a 19x30 cell): every flap. Small flaps are 15px in 15x24 cells (clock, log timestamps); large are 32px in 30x46 cells (confidence).
- **Headline** (Sofia Sans 600, 24px, 1.3, max 48ch, balanced): the probable cause sentence in the verdict. The one place a sentence is set large, because it is a sentence.
- **Title** (600, 17px, 0.14em, uppercase): board headings (Suspect commits, Investigation), the Run button, tabs at 15px.
- **Body** (400, 16px, 1.45, tabular figures): reasoning and description prose, 72-78ch max. Metadata runs 12.5-14.5px in steel light.
- **Label** (600, 13px, 0.14em, uppercase, steel light): field labels for data (Mode, Elapsed, Users affected, Confidence, Known answer), table column heads at 12px.
- **Mono** (13px, 1.55): diffs, rollback commands, inline code.

### Named Rules
**The Board Prints Caps Rule.** If the board would print it (a title, status, hash, field name, column head, button), it is Sofia Sans Condensed uppercase with tracking. If a person or the agent wrote it as a sentence, it is Sofia Sans in sentence case.

**The Mono Is For Code Rule.** JetBrains Mono appears only where the content is code: diffs, the git revert line, inline identifiers. Never for numbers or labels; numbers get tabular figures in the sans faces.

## Layout

A 264px left rail and a fluid main column inside a 14px steel gutter (14px padding, 14px gaps). The rail is sticky and scrolls on its own: brand, Replay/Live switch, speed, the incident list with suite labels, the recording date at its foot. The main column stacks the alert board (title and meta on the left; stats and the Run button right-aligned) over a work grid of the suspects board (fluid) and a 340px sticky investigation log on the right; the verdict board lands beneath the suspects board and scrolls into view.

Spacing steps observed in use: 4, 8, 10, 14, 18 and 28px, with 14px as the structural gutter and 4px between board rows.

Responsive behaviour: below 1240px the log drops beneath the verdict (max 520px tall, not sticky); below 1000px the alert board and verdict grid go single-column; below 820px the rail moves under the main column, the incident list scrolls horizontally, flap cells shrink to 12x22, and each suspect row becomes two lines (number, commit and status, then the change in full).

## Elevation & Depth

Depth is material layering, not floating surfaces: steel bezel, frame panel, flap-black board, flap cell. Panels sit on the bezel with one soft shadow and a faint top highlight, like a mounted plate; the cells carry their own depth through the two-tone gradient and the black seam.

### Shadow Vocabulary
- **Mounted panel** (`box-shadow: 0 2px 6px rgba(0,0,0,.35), inset 0 1px 0 rgba(255,255,255,.05)`): every frame panel.
- **Primary button** (`box-shadow: 0 2px 4px rgba(0,0,0,.4)`): the Run button only; it drops on press (1px) and disappears when disabled.

### Named Rules
**The Plate Rule.** Only panels and the primary action cast a shadow, and only a short soft one. Nothing floats, glows or blurs.

## Shapes

Square. Panels, inputs, buttons, chips, tags, the mode switch and the diff well all have 0 radius and 1px strokes (#3A3D42 on panels, steel-mid on controls, rule-line on wells). The flap cell is the one exception at 2px, with a 1px black seam at exactly 50% height; lamps are the one circle (10px, radial amber gradient).

## Components

### Buttons
- **Shape:** square (0px), no stroke.
- **Primary:** Run investigation, amber fill, amber-ink text, condensed caps 17px with 0.14em tracking, inline SVG chevron, 13px 20px padding. There is one per screen.
- **Hover / Focus:** lightens to amber-hover; 1px press-down on active; 2px amber focus ring offset 2px. Disabled goes steel-mid with no shadow.
- **Text action:** "show more" in the log is an underlined amber text button.

### Mode switch
- **Style:** a two-segment radiogroup in a flap-black well with a steel-mid stroke and 3px inset. The checked segment is letter-white with flap-black text; unchecked is steel-light text on black.
- **Rule:** Replay is first and checked by default. A hint line under it says what the mode does and costs.

### Chips and tags
- **Chip:** condensed caps 13px, steel-mid stroke, steel-light text, no fill. The run chip reads "Replay · recorded <date>" with stand-in, instant or speed appended; the live variant takes an amber stroke and text.
- **Tag:** 11.5px caps in a 1px box inside suspect metadata (RANKER'S PICK); the outside-window variant is amber.

### Cards / Containers
- **Corner Style:** square.
- **Background:** frame-steel panel with a board head (title plus a steel-light subline, divided by rule-line) over a flap-black board body.
- **Shadow Strategy:** the mounted panel shadow (see Elevation & Depth).
- **Internal Padding:** board head 14px 18px 12px; board body 6px 10px 10px; tab panels 18px.

### Inputs / Fields
- **Style:** the speed select is flap-black, steel-mid 1px stroke, square, condensed caps 15px.
- **Focus:** the global 2px amber ring.

### Navigation
- **Incident list:** stacked frame-deep buttons, condensed caps titles at 14px over a 12px suite line (suite and incident id in white, "no replay" in steel light). Hover darkens; the current incident goes flap-black with a 1px amber border. Scrolls horizontally at phone width.
- **Result tabs:** Verdict, Slack brief, Resolution report; condensed caps 15px, a 2px amber underline on the selected tab.

### Split-flap display (signature)
A row of fixed character cells (2px gap; 19x30, 15x24 or 30x46). Each changing cell spins through 2-3 random characters at 75ms a tick, staggered 22ms per cell left to right, each tick an 85ms rotateX flip that dims to 55% at its midpoint. Tones: default white, amber (identified, probable cause), red (ruled out), unlit grey (not read). The SEV flap fills each cell cancel-red with no gap. Under reduced motion every flap sets instantly. A flap always carries an aria-label with its plain text.

### Suspects board
A flap table: No., Commit, Change, Status. Change text is condensed caps 16px, clamped to two lines, over a score and age metadata line. Row states: reading (2px amber inset ring, warm tint, its diff drawer open), identified (amber text, amber-tinted row, pulsing lamp), ruled out (red, struck through), unread (unlit grey). The diff drawer opens by animating grid rows over 350ms and closes back to one line.

### Investigation log
Timestamped steps on flap-black: a small-flap clock, then a condensed caps action and a steel-light detail. The agent's reasoning steps are labelled CLAUDE in amber with brighter prose; system steps stay steel light; the thinking step pulses a lamp.

### Verdict
Probable cause flap and headline sentence, reasoning prose, amber-bulleted evidence, rollback command in a code well; a 280px side column with a large confidence flap and the known-answer box (amber stroke for MATCH, red for a miss, inline SVG mark). A run footer gives time, tool calls, model and whether it was recorded or live.

### Slack brief (deliberate exception)
A white Block Kit preview card (6px radius, Segoe UI, Slack's own greys and link blue) that sits outside the board world on purpose: it shows what would be posted, and it is never posted from the demo.

## Do's and Don'ts

### Do:
- **Do** render any status, hash, clock or verdict value that changes as a split-flap display, and let it cascade per cell (2-3 intermediate characters, 75ms ticks, 22ms stagger).
- **Do** set every flap instantly and stop lamp pulses under prefers-reduced-motion.
- **Do** keep a ruled-out row on the board in cancel-red-text with a 2px strike-through.
- **Do** pair every colour signal with a second channel: strike-through, lamp, label or text.
- **Do** label every replay as a recording, with its date and model, in the run chip, run note and rail foot; mark stand-ins as stand-ins.
- **Do** keep amber for the agent, lamps, the primary action, and focus or current selection.
- **Do** draw textures as inline SVG; the brushed steel is feTurbulence noise in a data URI.
- **Do** size type for a compressed 1440x900 screen share: labels and metadata at 12-13px in caps or steel light, prose at 15-16px.

### Don't:
- **Don't** build the board out of rounded cards, charts or a chat transcript of the agent.
- **Don't** remove a ruled-out suspect, or mark it with red alone.
- **Don't** use amber for decoration, resting borders or headings.
- **Don't** animate anything other than flaps, lamps and the diff drawer (short background-colour easing on hover and row state aside): no fades, slides or parallax.
- **Don't** use JetBrains Mono outside code.
- **Don't** ship raster textures or images in the demo.
