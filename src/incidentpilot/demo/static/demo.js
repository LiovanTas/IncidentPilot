/* IncidentPilot demo page. Plain JS, no dependencies.
 *
 * The server streams one event per step of an investigation (Server-Sent Events). This
 * file turns them into the departure board: the ranker's shortlist, statuses that flap
 * over as Claude rules on each commit, the investigation log, and the verdict. Replays
 * and live runs send the same events, so this code never needs to know which it is
 * showing beyond the labels. */

"use strict";

const $ = (id) => document.getElementById(id);
const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const FLAP_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";

const state = {
  scenarios: [],
  liveEnabled: false,
  model: "",
  selected: null,
  mode: "replay",
  speed: 1,
  running: false,
  source: null,
  run: null,          // per-run state, see resetRun()
};

/* ------------------------------------------------------------------ helpers */

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function svgIcon(kind) {
  const paths = {
    check: '<path d="M3 9.5l4 4 8-9" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="square"/>',
    cross: '<path d="M4 4l10 10M14 4L4 14" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="square"/>',
    dash: '<path d="M4 9h10" fill="none" stroke="currentColor" stroke-width="2.4"/>',
  };
  const span = document.createElement("span");
  span.innerHTML = `<svg viewBox="0 0 18 18" aria-hidden="true">${paths[kind]}</svg>`;
  return span.firstChild;
}

function mmss(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function sameCommit(a, b) {
  if (!a || !b) return false;
  const n = Math.min(a.length, b.length, 40);
  return n >= 7 && a.slice(0, n).toLowerCase() === b.slice(0, n).toLowerCase();
}

function shortSha(sha) { return (sha || "").slice(0, 7); }

function formatWhen(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("en-GB", {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
    timeZone: "UTC",
  }) + " UTC";
}

function formatDay(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

function ago(fromIso, toIso) {
  const ms = new Date(toIso) - new Date(fromIso);
  if (Number.isNaN(ms)) return "";
  if (ms < 0) return "landed after the alert";
  const minutes = Math.round(ms / 60000);
  if (minutes < 60) return `landed ${minutes}m before`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours < 48) return `landed ${hours}h${rest ? ` ${rest}m` : ""} before`;
  return `landed ${Math.round(hours / 24)} days before`;
}

/* -------------------------------------------------------------------- flaps */

function flap(cells, { size = "", tone = "", label = "" } = {}) {
  const node = el("span", { class: ["flap", size, tone].filter(Boolean).join(" "),
                            role: "img", "aria-label": label });
  for (let i = 0; i < cells; i += 1) {
    node.append(el("span", { class: "cell", "aria-hidden": "true" },
                   el("span", { class: "ch", text: " " })));
  }
  return node;
}

/* Set a flap display's text. Each character that changes cycles through a couple of
 * random characters first, staggered left to right: the board's signature move. */
function setFlap(node, text, { animate = true, tone } = {}) {
  const cells = node.querySelectorAll(".ch");
  const padded = String(text).toUpperCase().padEnd(cells.length).slice(0, cells.length);
  node.setAttribute("aria-label", String(text).trim());
  if (tone !== undefined) {
    node.classList.remove("amber", "red", "dim");
    if (tone) node.classList.add(tone);
  }
  cells.forEach((ch, i) => {
    const target = padded[i] === " " ? " " : padded[i];
    if (ch.dataset.target === target && ch.textContent === target) return;
    ch.dataset.target = target;
    const token = (ch._token || 0) + 1;
    ch._token = token;
    if (!animate || REDUCED) { ch.textContent = target; return; }
    const spins = 1 + (i % 3 === 0 ? 2 : 1);
    let k = 0;
    const tick = () => {
      if (ch._token !== token) return;
      k += 1;
      ch.textContent = k > spins ? target
        : FLAP_CHARS[Math.floor(Math.random() * FLAP_CHARS.length)];
      ch.classList.remove("tick");
      void ch.offsetWidth;
      ch.classList.add("tick");
      if (k <= spins) setTimeout(tick, 75);
    };
    setTimeout(tick, i * 22);
  });
}

/* ------------------------------------------------------------------- set-up */

async function init() {
  $("sev").replaceWith(Object.assign(flap(4, { label: "Severity" }), { id: "sev" }));
  $("sev").classList.add("sev");
  $("clock").replaceWith(Object.assign(flap(5, { size: "sm", label: "00:00" }), { id: "clock" }));
  setFlap($("clock"), "00:00", { animate: false });
  ghostRows();

  document.querySelectorAll(".switch button").forEach((button) => {
    button.addEventListener("click", () => setMode(button.dataset.mode));
  });
  $("speed").addEventListener("change", (e) => { state.speed = Number(e.target.value); });
  $("run").addEventListener("click", startRun);
  setupTabs();

  try {
    const response = await fetch("/api/demo/scenarios");
    const body = await response.json();
    state.scenarios = body.scenarios;
    state.liveEnabled = body.live_enabled;
    state.model = body.model;
  } catch (err) {
    showError(`Could not reach the demo server: ${err.message}`);
    return;
  }
  // Deep links: /demo?incident=INC-02&mode=live&speed=2&autorun=1
  const params = new URLSearchParams(window.location.search);
  if (params.get("mode") === "live" && state.liveEnabled) state.mode = "live";
  const speed = Number(params.get("speed"));
  if (params.has("speed") && [0, 1, 2, 4].includes(speed)) {
    state.speed = speed;
    $("speed").value = String(speed);
  }
  renderIncidentList();
  renderModeControls();
  const wanted = state.scenarios.find((s) => s.id === params.get("incident"));
  const first = wanted || state.scenarios[0];
  if (first) select(first.id);
  if (first && params.get("autorun") === "1" && !$("run").disabled) startRun();
}

function ghostRows() {
  const box = $("ghost-rows");
  box.replaceChildren();
  for (let i = 0; i < 6; i += 1) {
    box.append(el("div", { class: "ghost-row" },
      flap(2), flap(7), el("span", { class: "ghost-line" }), flap(10)));
  }
}

const SUITE_LABEL = {
  synthetic: (s) => [`Synthetic · `, el("b", { text: s.id })],
  fresh: () => ["Planted bug · ", el("b", { text: "no benchmark" })],
  negative: (s) => ["Negative control · ", el("b", { text: s.id })],
  realworld: (s) => ["Real regression · ", el("b", { text: s.repo })],
};

function renderIncidentList() {
  const list = $("incidents");
  list.replaceChildren(...state.scenarios.map((s) => {
    const status = state.mode === "replay" && !s.recorded_at ? " · no replay" : "";
    return el("li", {},
      el("button", {
        type: "button", class: "incident", "data-id": s.id,
        "aria-current": state.selected && state.selected.id === s.id ? "true" : "false",
        onclick: () => select(s.id),
      },
        el("span", { class: "incident-title", text: s.title }),
        el("span", { class: "incident-suite" }, ...SUITE_LABEL[s.suite](s), status)));
  }));
}

function setMode(mode) {
  if (mode === "live" && !state.liveEnabled) return;
  state.mode = mode;
  renderModeControls();
  renderIncidentList();
  updateRunButton();
}

function renderModeControls() {
  document.querySelectorAll(".switch button").forEach((button) => {
    const on = button.dataset.mode === state.mode;
    button.setAttribute("aria-checked", on ? "true" : "false");
    button.tabIndex = on ? 0 : -1;
    if (button.dataset.mode === "live") button.disabled = !state.liveEnabled;
  });
  $("speed-field").hidden = state.mode !== "replay";
  const hint = $("mode-hint");
  if (state.mode === "replay") {
    hint.textContent = "Recorded real runs. No API calls.";
  } else {
    hint.textContent = `Calls ${state.model} for real, a few cents a run.`;
  }
  if (!state.liveEnabled) {
    hint.append(el("br"), "Live needs ", el("code", { text: "ANTHROPIC_API_KEY" }), ".");
  }
  const dates = state.scenarios.map((s) => s.recorded_at).filter(Boolean).sort();
  $("rail-foot").textContent = dates.length
    ? `Replays recorded ${formatDay(dates[0])}${dates.length > 1 && formatDay(dates.at(-1)) !== formatDay(dates[0]) ? ` – ${formatDay(dates.at(-1))}` : ""} with ${state.scenarios.find((s) => s.recorded_model)?.recorded_model}.`
    : "No replays recorded yet. Run incidentpilot demo-record with an API key set.";
}

function select(id) {
  if (state.running) stopStream();
  const scenario = state.scenarios.find((s) => s.id === id);
  if (!scenario) return;
  state.selected = scenario;
  document.querySelectorAll(".incident").forEach((b) => {
    b.setAttribute("aria-current", b.dataset.id === id ? "true" : "false");
  });
  $("why").hidden = false;
  $("why-text").textContent = scenario.hook;
  showAlert(scenario);
  resetBoards();
  updateRunButton();
}

function showAlert(scenario) {
  const alert = scenario.alert;
  setFlap($("sev"), (alert.severity || "sev2").toUpperCase());
  $("alert-title").textContent = alert.title;
  const meta = $("alert-meta");
  meta.replaceChildren(
    el("span", {}, "Service ", el("b", { text: alert.service })),
    alert.started_at ? el("span", {}, "Fired ", el("b", { text: formatWhen(alert.started_at) })) : null,
    el("span", { class: "chip", id: "mode-chip" }, state.mode === "replay" ? "Replay" : "Live"),
  );
  $("alert-desc").textContent = alert.description || "";
  $("users-note").textContent = "";
  if (scenario.suite === "realworld") {
    // A real repository has no service map or traffic data, so user impact would be made
    // up. Show what is real instead: whose code it is and how many commits are suspects.
    $("users-label").textContent = "Repository";
    $("users").textContent = scenario.repo;
    $("rate-label").textContent = "Suspects";
    $("error-rate").textContent = "—";
    return;
  }
  $("users-label").textContent = "Users affected";
  $("rate-label").textContent = "Error rate";
  const rate = alert.metrics && alert.metrics.error_rate_after;
  $("error-rate").textContent = typeof rate === "number" ? `${(rate * 100).toFixed(1)}%` : "—";
  $("users").textContent = "—";
}

function updateRunButton() {
  const button = $("run");
  const note = $("run-note");
  const s = state.selected;
  let reason = "";
  if (!s) reason = "Choose an incident.";
  else if (state.mode === "replay" && !s.recorded_at) {
    reason = "No recording of this incident yet. Switch to Live, or record it with incidentpilot demo-record.";
  } else if (state.mode === "live" && !s.live_ready) {
    reason = `The ${s.repo} repository is not on this machine, so this one can only be replayed.`;
  } else if (state.mode === "replay") {
    reason = `Recorded ${formatDay(s.recorded_at)} with ${s.recorded_model}.`;
  } else {
    reason = `Runs ${state.model} live against the repository.`;
  }
  const blocked = !s || state.running
    || (state.mode === "replay" && !s.recorded_at) || (state.mode === "live" && !s.live_ready);
  button.disabled = blocked;
  $("run-label").textContent = state.running ? "Investigating…"
    : state.run && state.run.finished ? "Run again" : "Run investigation";
  note.textContent = state.running ? "" : reason;
}

/* ---------------------------------------------------------------- run state */

function resetRun() {
  state.run = {
    rows: new Map(),       // sha -> row record
    order: [],
    startedAt: null,
    alertStart: state.selected ? state.selected.alert.started_at : null,
    lastT: 0,
    lastWall: performance.now(),
    clockTimer: null,
    thinkingNode: null,
    verdict: null,
    recordedAt: null,
    mode: state.mode,
    finished: false,
    openDiff: null,
  };
}

function resetBoards() {
  resetRun();
  $("error").hidden = true;
  $("suspect-rows").replaceChildren();
  $("suspects-table").hidden = true;
  $("suspects-empty").hidden = false;
  $("suspects-sub").textContent = "The ranker's shortlist from the 72 hours before the alert";
  $("steps").replaceChildren(el("li", { class: "step system" },
    el("span", { class: "step-action", text: "—" }),
    el("div", {}, el("div", { class: "step-detail", text: "Steps appear here as Claude takes them: each diff it reads, each lookup, its reasoning in its own words." }))));
  $("verdict").hidden = true;
  ["panel-verdict", "panel-slack", "panel-report"].forEach((id) => $(id).replaceChildren());
  selectTab("tab-verdict");
  setFlap($("clock"), "00:00", { animate: false });
}

function stopStream() {
  if (state.source) state.source.close();
  state.source = null;
  state.running = false;
  if (state.run && state.run.clockTimer) clearInterval(state.run.clockTimer);
}

async function startRun() {
  if (!state.selected || state.running) return;
  resetBoards();
  state.running = true;
  state.run.mode = state.mode;
  updateRunButton();
  let runId;
  try {
    const response = await fetch("/api/demo/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario: state.selected.id, mode: state.mode, speed: state.speed }),
    });
    if (!response.ok) throw new Error((await response.json()).detail || response.statusText);
    runId = (await response.json()).run_id;
  } catch (err) {
    state.running = false;
    showError(`Could not start the run: ${err.message}`);
    updateRunButton();
    return;
  }
  const source = new EventSource(`/api/demo/runs/${runId}/events`);
  state.source = source;
  source.onmessage = (message) => handle(JSON.parse(message.data));
  source.onerror = () => {
    if (state.running) showError("Lost the connection to the demo server.");
    stopStream();
    updateRunButton();
  };
}

function startClock() {
  const run = state.run;
  if (run.clockTimer) clearInterval(run.clockTimer);
  const rate = run.mode === "replay" ? state.speed : 1;
  run.clockTimer = setInterval(() => {
    const elapsed = run.lastT + ((performance.now() - run.lastWall) / 1000) * rate;
    setFlap($("clock"), mmss(elapsed), { animate: false });
  }, 250);
}

/* ------------------------------------------------------------------- events */

function handle(event) {
  const run = state.run;
  if (typeof event.t === "number") {
    run.lastT = event.t;
    run.lastWall = performance.now();
  }
  switch (event.type) {
    case "start": onStart(event); break;
    case "alert": break;
    case "candidates": onCandidates(event); break;
    case "runbooks": onRunbooks(event); break;
    case "impact": onImpact(event); break;
    case "thinking_started": showThinking(); break;
    case "reasoning": addStep("reasoning", event.t, "Claude", event.text, true); break;
    case "note": addStep("reasoning", event.t, "Claude", event.text, true); break;
    case "tool_start": onToolStart(event); break;
    case "tool_result": onToolResult(event); break;
    case "verdict": onVerdict(event); break;
    case "done": onDone(event); break;
    case "answer": onAnswer(event); break;
    case "error": onError(event); break;
    case "end": onEnd(event); break;
    default: break;
  }
}

function onStart(event) {
  state.run.recordedAt = event.recorded_at || null;
  const chip = $("mode-chip");
  if (chip) {
    chip.className = event.mode === "live" ? "chip live" : "chip";
    chip.replaceChildren(event.mode === "live"
      ? `Live · ${state.model}`
      : `Replay · recorded ${formatDay(event.recorded_at)}${/stand-in/i.test(event.model || "") ? " · stand-in" : ""}${event.speed === 0 ? " · instant" : event.speed !== 1 ? ` · ${event.speed}×` : ""}`);
  }
  startClock();
}

function onCandidates(event) {
  const run = state.run;
  $("suspects-empty").hidden = true;
  $("suspects-table").hidden = false;
  const n = event.candidates.length;
  if (state.selected && state.selected.suite === "realworld") $("error-rate").textContent = String(n);
  $("suspects-sub").textContent = n
    ? `${n} commits shortlisted from the ${event.lookback_hours} hours before the alert, ranked by score`
    : "No commits landed in the window";
  addStep("system", event.t, "Shortlist ranked",
          n ? `${n} candidates. The ranker's top pick is ${shortSha(event.ranker_pick)}.`
            : "Nothing landed in the window.");
  event.candidates.forEach((c, i) => addRow(c, i + 1, sameCommit(c.sha, event.ranker_pick)));
  // The board fills top to bottom, the way a real one does.
  run.order.forEach((sha, i) => {
    const row = run.rows.get(sha);
    setTimeout(() => {
      setFlap(row.num, String(row.rank).padStart(2, "0"));
      setFlap(row.commit, shortSha(sha));
      if (!row.touched) setFlap(row.status, "WAITING", { tone: "dim" });
    }, REDUCED ? 0 : i * 90);
  });
}

function addRow(c, rank, rankerPick, { outside = false } = {}) {
  const run = state.run;
  const num = flap(2, { label: String(rank) });
  const commit = flap(7, { label: shortSha(c.sha) });
  const status = flap(10, { label: "Waiting", tone: "dim" });
  status.classList.add("status-flap");
  const lampSlot = el("span", { class: "lamp-slot", style: "display:inline-block;width:18px" });
  const metaBits = [];
  if (rankerPick) metaBits.push(el("span", { class: "tag", text: "Ranker's pick" }));
  if (outside) metaBits.push(el("span", { class: "tag outside", text: "Found outside the shortlist" }));
  if (typeof c.score === "number") metaBits.push(el("span", { class: "meta-score", text: `score ${c.score.toFixed(2)}` }));
  if (c.authored_at || c.landed_at) metaBits.push(el("span", { text: ago(c.landed_at || c.authored_at, run.alertStart) }));
  const tr = el("tr", { class: "row unread", "data-sha": c.sha },
    el("td", { class: "num" }, num),
    el("td", { class: "commit" }, commit),
    el("td", { class: "change" },
      el("span", { class: "change-text", title: c.subject || "", text: c.subject || "Commit outside the shortlist" }),
      el("div", { class: "change-meta" }, metaBits)),
    el("td", { class: "status" }, lampSlot, status));
  $("suspect-rows").append(tr);
  // `touched` is set by anything that rules on the row, so the board's initial fill,
  // which is staggered, can never paint WAITING over a status that already arrived.
  const record = { sha: c.sha, rank, tr, num, commit, status, lampSlot, diff: null, read: false,
                   diffRow: null, touched: false };
  run.rows.set(c.sha, record);
  run.order.push(c.sha);
  tr.addEventListener("click", () => { if (record.diff) toggleDiff(record); });
  return record;
}

function findRow(sha) {
  for (const row of state.run.rows.values()) if (sameCommit(row.sha, sha)) return row;
  return null;
}

function onRunbooks(event) {
  const ids = [...new Set(event.runbooks.map((r) => r.runbook_id))];
  if (!ids.length) return;
  addStep("system", event.t, "Runbooks retrieved", ids.join(", "));
}

function onImpact(event) {
  if (state.selected && state.selected.suite === "realworld") return;
  const impact = event.impact;
  $("users").textContent = `~${impact.affected_users_point.toLocaleString("en-US")}`;
  $("users-note").textContent = `${impact.affected_users_low.toLocaleString("en-US")}–${impact.affected_users_high.toLocaleString("en-US")}`;
}

const ACTIONS = {
  get_commit_diff: (a) => `Read diff ${shortSha(a.sha)}`,
  search_runbooks: () => "Search runbooks",
  get_service_topology: (a) => `Check topology · ${a.service}`,
  get_file_history: () => "File history",
  list_commit_candidates: (a) => `Widen search${a.lookback_hours ? ` · ${a.lookback_hours} h` : ""}`,
  estimate_user_impact: () => "Re-estimate impact",
  read_file_at_commit: (a) => `Read file at ${shortSha(a.sha)}`,
};

function onToolStart(event) {
  const action = (ACTIONS[event.name] || (() => event.name))(event.args || {});
  const detail = event.name === "search_runbooks" ? `“${event.args.query}”`
    : event.name === "get_file_history" || event.name === "read_file_at_commit" ? event.args.path
    : "";
  const node = addStep("tool", event.t, action, detail);
  node.dataset.step = event.step;
  if (event.name === "get_commit_diff") {
    const row = findRow(event.args.sha);
    if (row) {
      state.run.rows.forEach((r) => r.tr.classList.remove("reading"));
      closeDiff();
      row.tr.classList.add("reading");
      row.tr.classList.remove("unread");
      row.touched = true;
      setFlap(row.status, "READING", { tone: "amber" });
      row.tr.scrollIntoView({ block: "nearest", behavior: REDUCED ? "auto" : "smooth" });
    }
  }
}

function filesFromStat(output) {
  const files = [];
  for (const line of output.split("\n")) {
    const m = line.match(/^\s(\S.*?)\s+\|\s+(\d+|Bin)/);
    if (m) files.push(m[1].trim());
  }
  return files;
}

function diffExcerpt(output) {
  if (/^(tool|git) error/.test(output)) return [{ cls: "del", text: output.slice(0, 300) }];
  const out = [];
  let file = null;
  for (const line of output.split("\n")) {
    if (line.startsWith("diff --git")) { file = line.split(" b/").pop(); continue; }
    if (line.startsWith("@@")) { if (file) { out.push({ cls: "file", text: file }); file = null; } continue; }
    if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("index ")) continue;
    if (line.startsWith("+") || line.startsWith("-")) {
      out.push({ cls: line[0] === "+" ? "add" : "del", text: line });
      if (out.length >= 16) { out.push({ cls: "file", text: "…" }); break; }
    }
  }
  return out;
}

function onToolResult(event) {
  const step = document.querySelector(`.step[data-step="${event.step}"] .step-detail`);
  if (event.name === "get_commit_diff") {
    const files = filesFromStat(event.output || "");
    if (step && files.length) step.textContent = files.slice(0, 3).join(", ") + (files.length > 3 ? ` +${files.length - 3}` : "");
    const row = findRow(event.args.sha);
    if (row) {
      row.diff = diffExcerpt(event.output || "");
      row.read = true;
      row.tr.classList.add("clickable");
      row.tr.title = "Show or hide this diff";
      setFlap(row.status, "READ", { tone: "" });
      openDiff(row);
    }
  } else if (step) {
    const summary = summarise(event.name, event.output || "");
    if (summary) step.textContent = summary;
  }
}

/* One plain line per tool result for the log; the raw output stays in the report. */
function summarise(name, output) {
  if (/^(tool|git) error/.test(output)) return output.slice(0, 160);
  const join = (items) => (items && items.length ? items.join(", ") : "nothing");
  try {
    if (name === "get_service_topology") {
      const t = JSON.parse(output);
      return t.service ? `Calls ${join(t.depends_on)}. Called by ${join(t.called_by)}.` : output.slice(0, 160);
    }
    if (name === "estimate_user_impact") {
      const t = JSON.parse(output);
      return `About ${t.affected_users_point.toLocaleString("en-US")} users over ${t.duration_minutes} min.`;
    }
  } catch { /* fall through to the text summaries */ }
  if (name === "search_runbooks") {
    const ids = [...new Set([...output.matchAll(/^### (\S+) ::/gm)].map((m) => m[1]))];
    return ids.length ? `Matched ${ids.join(", ")}` : "No runbook matched.";
  }
  if (name === "get_file_history") {
    const n = output.split("\n").filter((l) => l.trim()).length;
    return `${n} recent commit${n === 1 ? "" : "s"} touch this path.`;
  }
  if (name === "read_file_at_commit") {
    return `${output.split("\n").length} lines read.`;
  }
  const first = output.split("\n").find((l) => l.trim()) || "";
  return first.length > 160 ? `${first.slice(0, 159)}…` : first;
}

function openDiff(row) {
  closeDiff();
  if (!row.diff || !row.diff.length) return;
  const pre = el("pre", { class: "diff" },
    row.diff.map((d) => el("span", { class: d.cls, text: `${d.text}\n` })));
  // The wrapper grows from nothing, so the rows below slide down instead of jumping.
  const wrap = el("div", { class: "diff-wrap" }, el("div", { class: "diff-inner" }, pre));
  row.diffRow = el("tr", { class: "diff-row" }, el("td", { colspan: "4" }, wrap));
  if (row.tr.classList.contains("reading")) row.diffRow.classList.add("reading");
  row.tr.after(row.diffRow);
  state.run.openDiff = row;
  if (REDUCED) wrap.classList.add("open");
  else requestAnimationFrame(() => requestAnimationFrame(() => wrap.classList.add("open")));
}

function closeDiff() {
  const open = state.run && state.run.openDiff;
  if (open && open.diffRow) open.diffRow.remove();
  if (open) open.diffRow = null;
  if (state.run) state.run.openDiff = null;
}

function toggleDiff(row) {
  if (row.diffRow) closeDiff(); else openDiff(row);
}

function showThinking() {
  hideThinking();
  const node = el("li", { class: "step thinking" },
    flap(5, { size: "sm", label: "" }),
    el("div", {}, el("div", { class: "step-action" },
      el("span", { class: "lamp pulse", "aria-hidden": "true" }), "Claude is thinking")));
  setFlap(node.firstChild, mmss(state.run.lastT), { animate: false });
  $("steps").append(node);
  state.run.thinkingNode = node;
  node.scrollIntoView({ block: "nearest" });
}

function hideThinking() {
  if (state.run && state.run.thinkingNode) state.run.thinkingNode.remove();
  if (state.run) state.run.thinkingNode = null;
}

function addStep(kind, t, action, detail, clamp = false) {
  hideThinking();
  const list = $("steps");
  if (list.querySelector(".step.system .step-action")?.textContent === "—") list.replaceChildren();
  const time = flap(5, { size: "sm", label: mmss(t || 0) });
  setFlap(time, mmss(t || 0), { animate: false });
  const detailNode = el("div", { class: "step-detail" });
  const text = detail || "";
  if (clamp && text.length > 320) {
    detailNode.textContent = `${text.slice(0, 300).trimEnd()}…`;
    const more = el("button", { type: "button", class: "step-more", text: "Show all" });
    more.addEventListener("click", () => { detailNode.textContent = text; more.remove(); });
    const node = el("li", { class: `step ${kind}` }, time,
      el("div", {}, el("div", { class: "step-action", text: action }), detailNode, more));
    list.append(node);
    node.scrollIntoView({ block: "nearest" });
    return node;
  }
  detailNode.textContent = text;
  const node = el("li", { class: `step ${kind}` }, time,
    el("div", {}, el("div", { class: "step-action", text: action }), detailNode));
  list.append(node);
  node.scrollIntoView({ block: "nearest" });
  return node;
}

/* ------------------------------------------------------------------ verdict */

function onVerdict(event) {
  hideThinking();
  closeDiff();
  const run = state.run;
  const d = event.diagnosis;
  run.verdict = d;
  run.rows.forEach((r) => {
    r.tr.classList.remove("reading");
    if (r.diffRow) r.diffRow.classList.remove("reading");
  });
  const blamed = d.offending_sha;
  let matchedRow = blamed ? findRow(blamed) : null;
  if (blamed && !matchedRow) {
    matchedRow = addRow({ sha: blamed, subject: "" }, run.order.length + 1, false, { outside: true });
    setFlap(matchedRow.num, String(matchedRow.rank).padStart(2, "0"));
    setFlap(matchedRow.commit, shortSha(blamed));
  }
  run.order.forEach((sha, i) => {
    const row = run.rows.get(sha);
    row.touched = true;
    const delay = REDUCED ? 0 : i * 70;
    setTimeout(() => {
      row.tr.classList.remove("unread", "ruled", "identified");
      if (row === matchedRow) {
        row.tr.classList.add("identified");
        row.lampSlot.replaceChildren(el("span", { class: "lamp", "aria-hidden": "true" }));
        setFlap(row.status, "IDENTIFIED", { tone: "amber" });
      } else if (row.read) {
        row.tr.classList.add("ruled");
        setFlap(row.status, "RULED OUT", { tone: "red" });
      } else {
        row.tr.classList.add("unread");
        setFlap(row.status, "NOT READ", { tone: "dim" });
      }
    }, delay);
  });
  addStep("system", event.t, blamed ? `Verdict · ${shortSha(blamed)}` : "Verdict · no commit",
          d.needs_human ? "Flagged for a human to review." : `Confidence ${Math.round(d.confidence * 100)}%.`);
  renderVerdict(d, matchedRow);
  $("verdict").hidden = false;
  setTimeout(() => $("verdict").scrollIntoView({ block: "nearest", behavior: REDUCED ? "auto" : "smooth" }),
             REDUCED ? 0 : 500);
}

function renderVerdict(d, row) {
  const panel = $("panel-verdict");
  const conf = flap(4, { size: "lg", label: `${Math.round(d.confidence * 100)} percent` });
  const cause = el("div", {},
    el("div", { class: "cause-line" },
      el("p", { class: "label", style: "margin:0", text: d.offending_sha ? "Probable cause" : "Finding" }),
      d.offending_sha ? flap(7, { label: shortSha(d.offending_sha), tone: "amber" }) : null,
      d.needs_human ? el("span", { class: "flag-human" }, "Needs human review") : null,
      d.source === "heuristic" ? el("span", { class: "flag-human" }, "Ranker fallback, not the agent") : null),
    el("p", { class: "cause", text: d.root_cause }),
    d.reasoning ? el("p", { class: "reasoning-text", text: d.reasoning }) : null,
    d.evidence && d.evidence.length
      ? el("ul", { class: "evidence" }, d.evidence.slice(0, 4).map((e) => el("li", { text: e })))
      : null,
    d.rollback_command
      ? el("div", { class: "rollback" }, el("p", { class: "label", text: "Rollback" }),
           el("code", { text: d.rollback_command }))
      : null,
    el("p", { class: "run-foot", id: "run-foot", text: "Finishing…" }));
  const side = el("div", { class: "side-stats" },
    el("div", {}, el("p", { class: "label", text: "Confidence" }), conf),
    el("div", { class: "answer", id: "answer" },
      el("p", { class: "label", style: "margin:0", text: "Known answer" }),
      el("p", { text: "Revealed when the run ends." })));
  panel.replaceChildren(el("div", { class: "verdict-grid" }, cause, side));
  const cf = cause.querySelector(".cause-line .flap");
  if (cf) setFlap(cf, shortSha(d.offending_sha));
  setFlap(conf, `${Math.round(d.confidence * 100)}%`.padStart(4));
  void row;
}

function onDone(event) {
  const run = state.run;
  const bits = [`${event.duration_s} s`, `${event.tool_calls} tool calls`];
  if (typeof event.cost_usd === "number") bits.push(`$${event.cost_usd.toFixed(3)}`);
  // A replay names the model the recording says it came from, so a stand-in can't pass as real.
  bits.push(run.mode === "replay" && state.selected && state.selected.recorded_model
    ? state.selected.recorded_model : event.model);
  bits.push(run.mode === "replay" ? `recorded ${formatDay(run.recordedAt)}` : "live run");
  const foot = $("run-foot");
  if (foot) foot.textContent = bits.join(" · ");
  renderSlack(event.slack || []);
  renderReport(event.report_md || "");
  const tab = new URLSearchParams(window.location.search).get("tab");
  if (tab === "slack" || tab === "report") selectTab(`tab-${tab}`);
}

function onAnswer(event) {
  const box = $("answer");
  if (!box) return;
  const kids = [el("p", { class: "label", style: "margin:0", text: "Known answer" })];
  let result;
  if (event.matched === true) result = ["check", event.suite === "negative" ? "Correct · no commit" : "Match"];
  else if (event.matched === false) result = ["cross", "Different"];
  else result = ["dash", "Not available"];
  box.className = `answer ${event.matched === true ? "match" : event.matched === false ? "miss" : ""}`;
  kids.push(el("div", { class: "answer-result" }, svgIcon(result[0]), result[1]));
  if (event.suite === "negative") {
    kids.push(el("p", { text: "Nothing in the code caused this alert. The right answer is to name no commit." }));
  } else if (event.sha) {
    kids.push(el("p", {}, el("b", { style: "color:#F2F2F2", text: shortSha(event.sha) }), ` ${event.subject}`));
  } else if (event.subject) {
    kids.push(el("p", { text: event.subject }));
  }
  kids.push(el("p", { style: "margin-top:6px", text: `Source: ${event.source}.` }));
  box.replaceChildren(...kids);
}

function onError(event) {
  hideThinking();
  showError(event.message);
  stopStream();
  updateRunButton();
}

function onEnd() {
  hideThinking();
  state.run.finished = true;
  stopStream();
  setFlap($("clock"), mmss(state.run.lastT), { animate: false });
  updateRunButton();
}

function showError(message) {
  const box = $("error");
  box.textContent = message;
  box.hidden = false;
}

/* --------------------------------------------------------------------- tabs */

function setupTabs() {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => selectTab(tab.id));
    tab.addEventListener("keydown", (e) => {
      const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!step) return;
      e.preventDefault();
      const next = tabs[(i + step + tabs.length) % tabs.length];
      selectTab(next.id);
      next.focus();
    });
  });
}

function selectTab(id) {
  document.querySelectorAll('[role="tab"]').forEach((tab) => {
    const on = tab.id === id;
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
    $(tab.getAttribute("aria-controls")).hidden = !on;
  });
}

/* ---------------------------------------------------------------- slack brief */

const SLACK_EMOJI = {
  rotating_light: "\u{1F6A8}", warning: "⚠️", large_yellow_circle: "\u{1F7E1}",
};

function mrkdwn(text) {
  let html = escapeHtml(text).replace(/:([a-z_]+):/g, (m, name) => SLACK_EMOJI[name] || m);
  html = html.replace(/```([\s\S]*?)```/g, (_, code) => `<pre>${code}</pre>`);
  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  html = html.replace(/&lt;!date\^\d+\^[^|]*\|([^&]*)&gt;/g, "$1");
  html = html.replace(/&lt;(https?:[^|&]+)\|([^&]+)&gt;/g, '<a href="$1" target="_blank" rel="noopener">$2</a>');
  html = html.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<b>$2</b>");
  html = html.replace(/(^|[\s(])_([^_\n]+)_/g, "$1<i>$2</i>");
  return html;
}

function renderSlack(blocks) {
  const body = el("div", {});
  for (const block of blocks) {
    if (block.type === "header") {
      const node = el("div", { class: "sb-header" });
      node.innerHTML = mrkdwn(block.text.text);
      body.append(node);
    }
    else if (block.type === "divider") body.append(el("hr", { class: "sb-divider" }));
    else if (block.type === "context") {
      const node = el("div", { class: "sb-context" });
      node.innerHTML = block.elements.map((e) => mrkdwn(e.text)).join(" ");
      body.append(node);
    } else if (block.type === "section") {
      if (block.text) {
        const node = el("div", { class: "sb-section" });
        node.innerHTML = mrkdwn(block.text.text);
        body.append(node);
      }
      if (block.fields) {
        const grid = el("div", { class: "sb-fields" });
        block.fields.forEach((f) => { const c = el("div", {}); c.innerHTML = mrkdwn(f.text); grid.append(c); });
        body.append(grid);
      }
    }
  }
  $("panel-slack").replaceChildren(
    el("div", { class: "slack" },
      el("div", { class: "slack-msg" },
        el("div", { class: "slack-avatar", "aria-hidden": "true", text: "IP" }),
        el("div", {},
          el("div", { class: "slack-who" }, "IncidentPilot", el("span", { class: "app", text: "APP" }),
             el("time", { text: "now" })),
          body))),
    el("p", { class: "slack-note", text: "The exact Block Kit payload IncidentPilot would post to #incidents. The demo never posts it." +
      (state.selected && state.selected.suite === "realworld"
        ? " This repository has no service map, so its impact fields use default traffic assumptions." : "") }));
}

/* ----------------------------------------------------------------- report */

function inlineMd(text) {
  let html = escapeHtml(text);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  html = html.replace(/(^|[\s(])_([^_]+)_(?=[\s).,;:]|$)/g, "$1<i>$2</i>");
  html = html.replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<i>$2</i>");
  html = html.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return html;
}

function renderMarkdown(md) {
  const lines = md.replace(/\r/g, "").split("\n");
  const out = [];
  let i = 0;
  let para = [];
  const flush = () => { if (para.length) { out.push(`<p>${inlineMd(para.join(" "))}</p>`); para = []; } };
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      flush();
      const code = [];
      i += 1;
      while (i < lines.length && !lines[i].startsWith("```")) { code.push(lines[i]); i += 1; }
      out.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      i += 1;
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    if (heading) { flush(); out.push(`<h${heading[1].length}>${inlineMd(heading[2])}</h${heading[1].length}>`); i += 1; continue; }
    if (/^---+\s*$/.test(line)) { flush(); out.push("<hr>"); i += 1; continue; }
    if (line.startsWith("|")) {
      flush();
      const rows = [];
      while (i < lines.length && lines[i].startsWith("|")) { rows.push(lines[i]); i += 1; }
      const cells = (row) => row.replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map((c) => c.replace(/\\\|/g, "|").trim());
      const head = cells(rows[0]);
      const body = rows.slice(2).map(cells);
      out.push(`<div class="table-wrap"><table><thead><tr>${head.map((c) => `<th>${inlineMd(c)}</th>`).join("")}</tr></thead><tbody>${
        body.map((r) => `<tr>${r.map((c) => `<td>${inlineMd(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const ordered = line.match(/^\s*\d+\.\s+(.*)$/);
    if (bullet || ordered) {
      flush();
      const tag = bullet ? "ul" : "ol";
      const items = [];
      const re = bullet ? /^\s*[-*]\s+(.*)$/ : /^\s*\d+\.\s+(.*)$/;
      while (i < lines.length && re.test(lines[i])) { items.push(lines[i].match(re)[1]); i += 1; }
      out.push(`<${tag}>${items.map((t) => `<li>${inlineMd(t)}</li>`).join("")}</${tag}>`);
      continue;
    }
    if (!line.trim()) { flush(); i += 1; continue; }
    para.push(line.trim());
    i += 1;
  }
  flush();
  return out.join("\n");
}

function renderReport(md) {
  const node = el("div", { class: "report" });
  node.innerHTML = renderMarkdown(md);
  $("panel-report").replaceChildren(node);
}

document.addEventListener("DOMContentLoaded", init);
