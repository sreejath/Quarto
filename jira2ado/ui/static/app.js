/* jira2ado browser view. Talks only to the local helper that served this page. */
"use strict";

const KEY = new URLSearchParams(location.hash.slice(1)).get("key") || "";
const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + "s")}`;

const S = {
  jira: null, ado: null, jiraUrl: "", adoUrl: "", settings: {}, problems: {}, connecting: false, editConnect: false,
  projects: [], projectsError: "", source: "",
  boards: [], boardsLoading: false, board: "",
  sprints: [], sprintsLoading: false, sprintsError: "", picked: new Set(), sprintFilter: "",
  includeDone: false, includeHierarchy: false, historyMode: true,
  targets: [], target: "", other: "", isOther: false,
  iterations: [], areas: [], iterationsFor: "", iterationsError: "", iterMode: "sprint", iteration: "", newIteration: "", area: "",
  job: null, since: 0, log: [], phase: "setup", checkedSig: "", checkedAt: null, error: "",
  run: null, choice: "skip", confirm: "", tab: "where", stamps: {}, quitting: false,
  // Interactive validation table state
  ticketStates: {},     // {jiraKey: adoState}
  statusOverrides: {},  // {jiraStatus: adoState}
  ticketActions: {},    // {jiraKey: "create" | "skip" | "duplicate" | "sync"}
  lastChangedKey: "",   // key whose status dropdown was just changed (shows "Apply to all" pill)
  searchFilter: "",
  outcomeFilter: "all", // "all" | "create" | "duplicates" | "there"
  drawerMode: null,     // null | "help" | "ticket"
  drawerTicketKey: null,
  peopleEdits: {},      // {account_id: ado_identity}
  savingPeople: false,
};

/* ------------------------------------------------------------ server calls */

async function api(path, body) {
  const opts = { headers: { "X-Jira2ado-Key": KEY } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new Error("The local jira2ado server is not responding. Start it again, then reload this page.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `The request failed (${res.status}).`);
  return data;
}

async function downloadServerFile(path, filename) {
  let res;
  try {
    res = await fetch(`/api/download?path=${encodeURIComponent(path)}`, {
      headers: { "X-Jira2ado-Key": KEY },
    });
  } catch (e) {
    toast("Could not reach the local server to download the file.");
    return;
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    toast(err.error || `Download failed (${res.status}).`);
    return;
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename || "download.csv";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 4200);
}

/* ------------------------------------------------------------ helpers */

const connected = () => Boolean(S.jira && S.ado);
const targetName = () => (S.isOther ? S.other.trim() : S.target);
const targetInfo = () => S.targets.find((t) => t.name === targetName());
const chosenIteration = () => (S.iterMode === "one" ? (S.iteration === "__new" ? S.newIteration.trim() : S.iteration) : "");
const busy = () => S.phase === "checking" || S.phase === "importing" || S.phase === "rolling_back";

function isDoublePaste(tok) {
  const v = (tok || "").trim();
  return v.length >= 20 && v.length % 2 === 0 && v.slice(0, v.length / 2) === v.slice(v.length / 2);
}

function displayedSprints() {
  if (!S.sprintFilter.trim()) return S.sprints;
  const q = S.sprintFilter.trim().toLowerCase();
  return S.sprints.filter((sp) =>
    `${sp.name} ${sp.state || ""} ${sp.goal || ""}`.toLowerCase().includes(q)
  );
}

function signature() {
  return JSON.stringify({
    s: S.source, b: S.board, p: [...S.picked].sort(), t: targetName(),
    i: chosenIteration(), a: S.area, d: S.includeDone, h: S.includeHierarchy, hm: S.historyMode,
  });
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso + "T12:00:00");
  const opts = { day: "numeric", month: "short" };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString("en-CA", opts);
}

function sprintMeta(sp) {
  const state = { active: "Active", future: "Future", closed: "Closed" }[sp.state] || "No dates";
  const dates = sp.start && sp.end ? `, ${fmtDate(sp.start)} to ${fmtDate(sp.end)}` : "";
  const goal = sp.goal ? ` · Goal: ${sp.goal.slice(0, 60)}` : "";
  return `<b>${esc(state)}</b>${esc(dates)}${esc(goal)}`;
}

function belowProject(path, project) {
  if (!path) return "";
  if (path === project) return "";
  return path.startsWith(project + "\\") ? path.slice(project.length + 1) : path;
}

function isDuplicateTicket(t) {
  return t.outcome === "already_there" || t.outcome === "possible" || (t.in_run_duplicates && t.in_run_duplicates.length > 0);
}

function effectiveAction(t) {
  const chosen = S.ticketActions[t.key] || t.action;
  if (chosen === "sync" && t.outcome !== "already_there") return "skip";
  if (chosen) return chosen;
  if (t.outcome === "already_there") return "skip";
  if (t.outcome === "possible") return S.choice === "create" ? "duplicate" : "skip";
  return "create";
}

function effectiveState(t) {
  if (S.ticketStates[t.key]) return S.ticketStates[t.key];
  if (S.statusOverrides[t.jira_status]) return S.statusOverrides[t.jira_status];
  return t.ado_state || "";
}

/* ------------------------------------------------------------ outcomes */

function outcome(t, run) {
  const j = run.journal[t.key];
  const res = run.result || {};
  const failed = (res.failed || []).includes(t.key) && !(j && j.revs >= t.revisions);
  const tagged = (res.tagged || []).includes(t.key);
  const synced = (res.synced || []).includes(t.key);
  let note = run.verify[t.key] || "";
  if (note.startsWith("already there before this run")) {
    const diff = note.split(": ").slice(1).join(": ");
    note = diff ? `Differs from Jira: ${diff}` : "";
  }
  if (failed) return { cls: "failed", text: "Failed", reason: note || "See the log for the error." };
  if (synced && j) return { cls: "done", text: "Delta Synced", id: j.id, reason: note };
  if (j) {
    if (j.found) return { cls: "there", text: "Already in ADO", id: j.id, reason: note };
    if (j.revs >= t.revisions) {
      if (tagged) return { cls: "tagged", text: "Created (Duplicate)", id: j.id, reason: note };
      return { cls: "done", text: t.outcome === "imported" ? "Imported earlier" : "Created", id: j.id, reason: note };
    }
    return { cls: "working", text: `Migrating (${j.revs}/${t.revisions})`, id: j.id };
  }
  if (run.held[t.key] !== undefined) return { cls: "out", text: "Skipped", reason: run.held[t.key] || "Skipped by user selection." };

  const act = effectiveAction(t);
  if (S.phase === "importing") {
    if (act === "skip") return { cls: "out", text: "Skipping" };
    return { cls: "working", text: "Waiting..." };
  }
  if (t.outcome === "already_there") {
    if (act === "duplicate" || act === "create") return { cls: "maybe", text: "Will create duplicate", id: t.ado_id };
    if (act === "sync") return { cls: "create", text: "Will sync delta", id: t.ado_id };
    return { cls: "there", text: "Duplicate (Exact Key)", id: t.ado_id };
  }
  if (t.outcome === "possible") {
    if (act === "duplicate" || act === "create") return { cls: "maybe", text: "Will create duplicate" };
    return { cls: "maybe", text: "Possible duplicate" };
  }
  if (act === "skip") return { cls: "out", text: "Will be skipped" };
  if (t.in_run_duplicates && t.in_run_duplicates.length) {
    return { cls: "maybe", text: "In-run duplicate (Create)" };
  }
  if (t.outcome === "partial") return { cls: "create", text: "Will be finished" };
  return { cls: "create", text: "Ready to migrate" };
}

function tally(run) {
  const c = { create: 0, there: 0, maybe: 0, done: 0, out: 0, failed: 0, duplicatesTotal: 0 };
  for (const t of run.preview.tickets) {
    if (isDuplicateTicket(t)) c.duplicatesTotal++;
    const o = outcome(t, run);
    const act = effectiveAction(t);
    if (o.cls === "done" || o.cls === "tagged") c.done++;
    else if (o.cls === "failed") c.failed++;
    else if (o.cls === "out") c.out++;
    else if (S.phase !== "imported" && (act === "create" || act === "duplicate" || act === "sync")) c.create++;
    else if (o.cls === "there") c.there++;
    else if (o.cls === "maybe") c.maybe++;
  }
  return c;
}

function toCreate(run) {
  let count = 0;
  for (const t of run.preview.tickets) {
    const j = run.journal[t.key];
    if (j && (j.found || j.revs >= t.revisions)) continue;
    const act = effectiveAction(t);
    if (act === "create" || act === "duplicate" || act === "sync") count++;
  }
  return count;
}

/* ------------------------------------------------------------ rendering: header and steps */

function renderWho() {
  const parts = [];
  if (S.jira) parts.push(`<span>Jira: <b>${esc(S.jira)}</b></span>`);
  if (S.ado) parts.push(`<span>Azure DevOps: <b>${esc(S.ado)}</b></span>`);
  $("#who").innerHTML = parts.join("");
}

function setStep(id, state, html) {
  const el = $(id);
  el.className = `step ${state}`;
  el.innerHTML = html;
}

function renderConnect() {
  if (connected() && !S.editConnect) {
    setStep("#step-connect", "done", `
      <h2>Connect</h2>
      <div class="step-body">
        <p class="okline">Jira: <b>${esc(S.jira)}</b> <span class="soft">(${esc(S.jiraUrl.replace(/^https?:\/\//, ""))})</span></p>
        <p class="okline">Azure DevOps: <b>${esc(S.ado)}</b> <span class="soft">(${esc(S.adoUrl.replace(/^https?:\/\//, ""))})</span></p>
        <div class="row"><button class="quiet" type="button" data-action="edit-connect">Change connection / tokens</button></div>
      </div>`);
    return;
  }
  const p = S.problems;
  const defJiraUrl = S.jiraUrl || S.settings.jira_url || "";
  const defAdoUrl = S.adoUrl || S.settings.ado_url || "";
  setStep("#step-connect", "current", `
    <h2>Connect</h2>
    <p class="lede">Tokens stay in memory on this machine until you quit. Nothing secret is written to disk.</p>
    <form class="step-body" data-action="connect-form" autocomplete="off">
      <label class="field">Atlassian Jira URL
        <input type="url" id="jira-url" value="${esc(defJiraUrl)}" placeholder="https://your-org.atlassian.net" required>
      </label>
      <label class="field">Jira email
        <input type="email" id="jira-email" value="${esc(S.settings.jira_email || "")}" placeholder="you@company.com" autocomplete="username" required>
      </label>
      <label class="field">Jira API token
        <input type="password" id="jira-token" autocomplete="off" required>
      </label>
      ${p.jira ? `<p class="problem">${esc(p.jira)}</p>` : S.jira ? `<p class="okline">Jira: <b>${esc(S.jira)}</b></p>` : ""}
      <label class="field">Azure DevOps Organization URL
        <input type="url" id="ado-url" value="${esc(defAdoUrl)}" placeholder="https://dev.azure.com/your-org" required>
      </label>
      <label class="field">Azure DevOps PAT
        <input type="password" id="ado-token" autocomplete="off" required>
        <span class="hint">Needs Work Items (Read, write &amp; manage) scope and Bypass rules permission.</span>
      </label>
      ${p.ado ? `<p class="problem">${esc(p.ado)}</p>` : S.ado ? `<p class="okline">Azure DevOps: <b>${esc(S.ado)}</b></p>` : ""}
      <div class="row">
        <button class="btn" type="submit" ${S.connecting ? "disabled" : ""}>${S.connecting ? "Connecting..." : "Connect"}</button>
        ${S.editConnect ? `<button class="quiet" type="button" data-action="cancel-connect">Keep current connection</button>` : ""}
      </div>
    </form>`);
}

function renderTickets() {
  if (!connected()) {
    setStep("#step-tickets", "locked", `<h2>Source Tickets</h2><p class="lede">Connect first.</p>`);
    return;
  }
  const prevScroll = document.querySelector("#step-tickets .sprints")?.scrollTop ?? 0;
  const activeEl = document.activeElement;
  const stepRoot = document.querySelector("#step-tickets");
  const activeAction = activeEl && stepRoot && stepRoot.contains(activeEl) ? (activeEl.dataset?.action || "") : "";
  const activeVal = activeEl ? String(activeEl.value ?? "") : "";
  const options = S.projects.map((p) =>
    `<option value="${esc(p.key)}" ${p.key === S.source ? "selected" : ""}>${esc(p.key)}: ${esc(p.name)}</option>`).join("");
  const boardOpts = S.boards.map((b) =>
    `<option value="${esc(b.id)}" ${String(b.id) === String(S.board) ? "selected" : ""}>${esc(b.name)} (${esc(b.type)})</option>`).join("");
  const shown = displayedSprints();
  const shownPicked = shown.filter((sp) => S.picked.has(String(sp.id))).length;
  const hiddenPicked = Math.max(0, S.picked.size - shownPicked);
  let list;
  if (S.sprintsLoading) list = `<div class="empty-list">Loading sprints...</div>`;
  else if (S.sprintsError) list = `<div class="empty-list">${esc(S.sprintsError)}</div>`;
  else if (!S.source) list = `<div class="empty-list">Choose a Jira project to see its boards and sprints.</div>`;
  else if (!S.sprints.length) list = `<div class="empty-list">No sprints found on the selected board.</div>`;
  else if (!shown.length) list = `<div class="empty-list">No sprints match "${esc(S.sprintFilter)}".</div>`;
  else list = shown.map((sp) => `
      <label class="sprint ${sp.state === "active" ? "active-sprint" : ""}">
        <input type="checkbox" data-action="pick-sprint" value="${esc(sp.id)}" ${S.picked.has(String(sp.id)) ? "checked" : ""} ${busy() ? "disabled" : ""}>
        <span class="sprint-name">${esc(sp.name)}</span>
        <span class="sprint-meta">${sprintMeta(sp)}</span>
      </label>`).join("");
  const sprintToolbar = S.sprints.length ? `
    <div class="sprint-toolbar">
      <input type="search" class="sprint-filter" data-action="filter-sprints" placeholder="Filter sprints..." value="${esc(S.sprintFilter)}" aria-label="Filter sprints" ${busy() ? "disabled" : ""}>
      <button type="button" class="btn secondary btn-sm" data-action="select-displayed-sprints" ${!shown.length || busy() ? "disabled" : ""}>Select all displayed (${shown.length})</button>
      ${shownPicked ? `<button type="button" class="quiet" data-action="clear-displayed-sprints" ${busy() ? "disabled" : ""}>Clear</button>` : ""}
      ${hiddenPicked ? `<button type="button" class="quiet" data-action="clear-all-sprints" ${busy() ? "disabled" : ""}>Clear all (${S.picked.size})</button>` : ""}
    </div>` : "";
  const state = S.source && S.picked.size ? "done" : "current";
  setStep("#step-tickets", state, `
    <h2>Source Tickets</h2>
    <p class="lede">Choose the Jira project, board, and one or more sprints.</p>
    <div class="step-body">
      <label class="field">Jira project
        <select data-action="select-project" ${busy() ? "disabled" : ""}>
          <option value="">${S.projects.length ? "Choose a project" : esc(S.projectsError || "Loading projects...")}</option>
          ${options}
        </select>
      </label>
      <label class="field">Jira board
        <select data-action="select-board" ${!S.source || busy() ? "disabled" : ""}>
          <option value="">${S.boardsLoading ? "Loading boards..." : `All boards in ${esc(S.source || "project")} (${S.boards.length})`}</option>
          ${boardOpts}
        </select>
      </label>
      <div class="field">Sprints (${S.picked.size} selected${hiddenPicked ? ` · ${hiddenPicked} hidden by filter` : ""})
        ${sprintToolbar}
        <div class="sprints" role="group" aria-label="Sprints">${list}</div>
      </div>
      <label class="check"><input type="checkbox" data-action="include-done" ${S.includeDone ? "checked" : ""} ${busy() ? "disabled" : ""}>
        <span>Include tickets whose status category is <b>Done</b></span></label>
      <label class="check"><input type="checkbox" data-action="include-hierarchy" ${S.includeHierarchy ? "checked" : ""} ${busy() ? "disabled" : ""}>
        <span>Auto-include parent Epics &amp; child Sub-tasks outside the sprint</span></label>
      <label class="check"><input type="checkbox" data-action="history-mode" ${S.historyMode ? "checked" : ""} ${busy() ? "disabled" : ""}>
        <span>Migrate full changelog revision history, worklogs &amp; links</span></label>
    </div>`);
  const box = document.querySelector("#step-tickets .sprints");
  if (box) box.scrollTop = prevScroll;
  if (activeAction) {
    const target = activeAction === "pick-sprint"
      ? Array.from(document.querySelectorAll('#step-tickets input[data-action="pick-sprint"]')).find((el) => String(el.value) === activeVal)
      : document.querySelector(`#step-tickets [data-action="${activeAction}"]`);
    if (target && typeof target.focus === "function") target.focus({ preventScroll: true });
  }
}

function renderDestination() {
  if (!connected()) {
    setStep("#step-destination", "locked", `<h2>Destination Project</h2><p class="lede">Connect first.</p>`);
    return;
  }
  const name = targetName();
  const info = targetInfo();
  const opts = S.targets.map((t) =>
    `<option value="${esc(t.name)}" ${!S.isOther && t.name === S.target ? "selected" : ""}>${esc(t.name)}</option>`).join("");
  const showOther = !S.targets.some((t) => !t.configured) || S.isOther;
  let note = "";
  if (name) {
    if (!info || info.confirm_with_project_name) note = "Requires typing the project name before migrating.";
    else note = "Statuses will be automatically matched to the closest state in this project.";
  }
  let iterPick = "";
  if (S.iterMode === "one") {
    if (!name) iterPick = `<p class="hint">Choose the project first.</p>`;
    else if (S.iterationsError) iterPick = `<p class="problem">${esc(S.iterationsError)}</p>`;
    else if (S.iterationsFor !== name) iterPick = `<p class="hint">Loading iterations...</p>`;
    else {
      iterPick = `<select data-action="select-iteration" aria-label="Iteration" ${busy() ? "disabled" : ""}>
          <option value="">Choose an iteration</option>
          ${S.iterations.map((p) => `<option value="${esc(p)}" ${p === S.iteration ? "selected" : ""}>${esc(p)}</option>`).join("")}
          <option value="__new" ${S.iteration === "__new" ? "selected" : ""}>Create a new iteration...</option>
        </select>
        ${S.iteration === "__new" ? `<input type="text" data-action="new-iteration" aria-label="New iteration name" placeholder="Name of the new iteration" value="${esc(S.newIteration)}">
        <p class="hint">Created automatically during migration.</p>` : ""}`;
    }
  }
  const state = name && (S.iterMode === "sprint" || chosenIteration()) ? "done" : "current";
  setStep("#step-destination", state, `
    <h2>Destination Project</h2>
    <p class="lede">Choose the target Azure DevOps project and iteration placement.</p>
    <div class="step-body">
      <label class="field">Azure DevOps project
        <select data-action="select-target" ${busy() ? "disabled" : ""}>
          <option value="">Choose a project</option>
          ${opts}
          ${showOther ? `<option value="__other" ${S.isOther ? "selected" : ""}>Another project...</option>` : ""}
        </select>
      </label>
      ${S.isOther ? `<input type="text" data-action="other-target" aria-label="Project name" placeholder="Project name, exactly as in Azure DevOps" value="${esc(S.other)}">` : ""}
      ${note ? `<p class="hint">${esc(note)}</p>` : ""}
      <div class="field">Iteration placement
        <label class="check"><input type="radio" name="iter" value="sprint" data-action="iter-mode" ${S.iterMode === "sprint" ? "checked" : ""} ${busy() ? "disabled" : ""}>
          <span>Named after each ticket's Jira sprint (auto-creates &amp; subscribes team)</span></label>
        <label class="check"><input type="radio" name="iter" value="one" data-action="iter-mode" ${S.iterMode === "one" ? "checked" : ""} ${busy() ? "disabled" : ""}>
          <span>Put every ticket in one specific iteration</span></label>
        ${iterPick}
      </div>
    </div>`);
}

function progressList(job) {
  const mark = { waiting: "", running: "•", done: "✓", failed: "✗", stopped: "–" };
  return `<ol class="progress">${job.steps.map((s) =>
    `<li class="${esc(s.status)}"><span class="mark" aria-hidden="true">${mark[s.status] || ""}</span><span>${esc(s.label)}</span></li>`).join("")}</ol>`;
}

function renderCheck() {
  if (!connected()) {
    setStep("#step-check", "locked", `<h2>Validate</h2><p class="lede">Connect first.</p>`);
    return;
  }
  const ready = S.source && S.picked.size && targetName() && (S.iterMode === "sprint" || chosenIteration());
  const stale = S.checkedSig && S.checkedSig !== signature();
  let body;
  if (S.phase === "checking" && S.job) {
    body = `${progressList(S.job)}<div class="row"><button class="quiet" type="button" data-action="stop">Stop</button></div>`;
  } else {
    const done = S.run && !stale && S.checkedAt;
    body = `
      ${done ? `<p class="summary-line">Validated at ${esc(S.checkedAt)}. <b>${esc(plural(S.run.preview.issues, "ticket"))}</b> ready for review.</p>` : ""}
      ${stale ? `<p class="problem">Selection changed since last validation. Click Validate again before migrating.</p>` : ""}
      ${S.error && S.phase === "setup" ? `<p class="problem">${esc(S.error)}</p>` : ""}
      <div class="row"><button class="btn ${done ? "secondary" : ""}" type="button" data-action="check" ${ready && !busy() ? "" : "disabled"}>${done ? "Validate again" : "Validate"}</button></div>`;
  }
  const state = S.phase === "checking" ? "current" : (S.run && !stale ? "done" : (ready ? "current" : ""));
  setStep("#step-check", state, `
    <h2>Validate</h2>
    <p class="lede">Fetches sprint tickets, maps statuses to the closest ADO state, and highlights duplicates.</p>
    <div class="step-body">${body}</div>`);
}

function renderImport() {
  const stale = S.checkedSig && S.checkedSig !== signature();
  if (!S.run || !S.run.preview || stale || S.phase === "checking") {
    setStep("#step-import", "locked", `<h2>Migrate</h2><p class="lede">Validate the tickets first.</p>`);
    return;
  }
  const run = S.run;
  const pv = run.preview;
  const project = pv.project;
  let body = "";
  if ((S.phase === "importing" || S.phase === "rolling_back") && S.job) {
    body = `${progressList(S.job)}<div class="row"><button class="quiet" type="button" data-action="stop">Stop</button></div>`;
  } else if (S.phase === "imported") {
    const c = tally(run);
    const files = run.files || {};
    body = `
      <div class="banner ${c.failed ? "bad" : ""}">
        <p>${esc(summaryText(run))}</p>
      </div>
      <div class="row">
        ${files["verify.csv"] ? `<button class="btn secondary btn-sm" type="button" data-action="download-file" data-path="${esc(files["verify.csv"])}" data-name="verify.csv">Download Verify CSV</button>` : ""}
        <button class="btn danger btn-sm" type="button" data-action="rollback">Undo / Rollback Run</button>
      </div>`;
  } else if (S.phase === "import_failed") {
    const n = toCreate(run);
    const c = tally(run);
    body = `
      <div class="banner bad">
        <p><b>Migration did not finish:</b> ${esc(S.error || "See the Log tab for details.")}</p>
        <p>${esc(plural(c.done, "ticket"))} completed; ${esc(plural(n, "ticket"))} remaining. Retrying is safe and idempotent — completed revisions are skipped and partial tickets are resumed from their live Azure DevOps revision.</p>
      </div>
      ${pv.confirm_with_project_name && S.confirm.trim() !== project ? `
        <label class="field">Type <b>${esc(project)}</b> to confirm retry / resume
          <input type="text" data-action="confirm" value="${esc(S.confirm)}" autocomplete="off" spellcheck="false">
        </label>` : ""}
      <div class="row">
        <button class="btn" type="button" data-action="import" ${pv.confirm_with_project_name && S.confirm.trim() !== project ? "disabled" : ""}>Retry / Resume Remaining (${n})</button>
        ${c.done > 0 ? `<button class="btn danger btn-sm" type="button" data-action="rollback">Undo / Rollback Run</button>` : ""}
      </div>`;
  } else {
    const n = toCreate(run);
    const hasDups = pv.tickets.some(isDuplicateTicket);
    let dup = "";
    if (hasDups) {
      dup = `<p class="hint">Duplicates are highlighted in the table. Use the per-ticket dropdown or the bulk toolbar buttons to <b>Skip</b> or <b>Create Duplicate</b> (or <b>Sync Delta</b> on exact Jira key matches).</p>`;
    }
    let go;
    if (!n) {
      go = `<p class="summary-line">No tickets selected for migration. Change a duplicate's action to <b>Create Duplicate</b> (or <b>Sync Delta</b> for exact key matches) if you want to migrate it.</p>`;
    } else if (pv.confirm_with_project_name) {
      go = `
        <label class="field">Type <b>${esc(project)}</b> to confirm migration
          <input type="text" data-action="confirm" value="${esc(S.confirm)}" autocomplete="off" spellcheck="false">
        </label>
        <div class="row"><button class="btn" type="button" data-action="import" ${S.confirm.trim() === project ? "" : "disabled"}>Migrate ${esc(plural(n, "ticket"))}</button></div>`;
    } else {
      go = `
        <div class="row"><button class="btn" type="button" data-action="import">Migrate ${esc(plural(n, "ticket"))} to ${esc(project)}</button></div>`;
    }
    body = `${dup}${S.error ? `<p class="problem">${esc(S.error)}</p>` : ""}${go}`;
  }
  const state = S.phase === "imported" ? "done" : "current";
  setStep("#step-import", state, `
    <h2>Migrate</h2>
    <p class="lede">Creates corresponding tickets in Azure DevOps with attachments, history, and comments.</p>
    <div class="step-body">${body}</div>`);
}

function summaryText(run) {
  const c = tally(run);
  const project = run.preview.project;
  const parts = [`Migrated ${plural(c.done, "work item")} in ${project}.`];
  if (c.there) parts.push(`${plural(c.there, "ticket was", "tickets were")} already there.`);
  if (c.out) parts.push(`${plural(c.out, "ticket was", "tickets were")} skipped.`);
  if (c.failed) parts.push(`${plural(c.failed, "ticket")} failed. See the Log tab for details.`);
  return parts.join(" ");
}

/* ------------------------------------------------------------ rendering: the validation ticket list */

function filterTickets(tickets, run) {
  return tickets.filter((t) => {
    if (S.outcomeFilter === "create" && !["create", "duplicate", "sync"].includes(effectiveAction(t))) return false;
    if (S.outcomeFilter === "duplicates" && !isDuplicateTicket(t)) return false;
    if (S.outcomeFilter === "there" && t.outcome !== "already_there") return false;
    if (S.searchFilter) {
      const q = S.searchFilter.toLowerCase();
      const hay = `${t.key} ${t.title} ${t.jira_type} ${t.ado_type} ${t.assignee || ""} ${t.jira_status} ${effectiveState(t)}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function renderManifest() {
  const m = $("#manifest");
  if (!S.run || !S.run.preview) {
    const checking = S.phase === "checking" && S.job;
    m.innerHTML = `
      <div class="empty">
        <h2>${checking ? "Validating sprint tickets..." : "Ready to validate tickets"}</h2>
        <p>${checking ? "Fetching issues, changelogs, comments, worklogs, and attachments from Jira, and matching statuses and duplicates against Azure DevOps. Nothing is written during validation."
          : "Select your Jira project, board, sprints, and destination Azure DevOps project on the left, then click Validate to review tickets, adjust status dropdowns, and resolve any duplicates."}</p>
        ${checking ? liveStrip() : ""}
      </div>
      ${S.log.length ? tabsHtml() : ""}`;
    return;
  }
  const run = S.run;
  const pv = run.preview;
  const r = pv.run || {};
  const sprints = (r.sprints || []).map((s) => `${s.name}${s.state ? ` (${s.state})` : ""}`);
  const places = pv.places || [];
  let where = "";
  if (places.length === 1) {
    const it = belowProject(places[0].iteration, pv.project);
    where = it ? `${it}${places[0].iteration_exists === false ? " (will be created)" : ""}` : "Project root iteration";
  } else if (places.length > 1) {
    where = `${places.length} sprint iterations`;
  }
  const c = tally(run);
  const after = S.phase === "importing" || S.phase === "imported";
  const counts = after ? [
    ["there", c.done, "migrated"], ["there", c.there, "already in ADO"],
    ["create", c.create, S.phase === "importing" ? "remaining" : "pending"],
    ["out", c.out, "skipped"], ["out", c.failed, "failed"],
  ].filter((x, i) => i < 2 || x[1]) : [
    ["", pv.issues, plural(pv.issues, "ticket").replace(/^\d+ /, "")],
    ["create", toCreate(run), "will be migrated"],
    ["there", c.there, "exact key matches"],
    ["maybe", c.duplicatesTotal, "highlighted duplicates"],
  ];

  const visibleTickets = filterTickets(pv.tickets, run);
  const dupCount = pv.tickets.filter(isDuplicateTicket).length;

  m.innerHTML = `
    <div class="route">
      <div>
        <div class="end-label">Source (Jira)</div>
        <div class="end-name">${esc(r.from || "")}</div>
        <div class="end-sub">${esc(sprints.join(", "))}${r.include_done ? " · all statuses" : " · excluding Done"}</div>
      </div>
      <div class="route-arrow" aria-hidden="true">→</div>
      <div>
        <div class="end-label">Destination (Azure DevOps)</div>
        <div class="end-name">${esc(pv.project)}</div>
        <div class="end-sub">${esc(where)}</div>
      </div>
    </div>
    <div class="counts">${counts.map(([cls, n, label]) =>
      `<div class="count ${cls}"><b>${n}</b><span>${esc(label)}</span></div>`).join("")}</div>
    ${busy() ? liveStrip() : ""}
    <div class="table-toolbar">
      <div class="filter-group">
        <input type="search" placeholder="Filter by key, summary, assignee..." value="${esc(S.searchFilter)}" data-action="search-tickets" aria-label="Search tickets">
        <button type="button" class="pill-filter ${S.outcomeFilter === "all" ? "active" : ""}" data-action="filter-outcome" data-filter="all">All (${pv.tickets.length})</button>
        <button type="button" class="pill-filter ${S.outcomeFilter === "create" ? "active" : ""}" data-action="filter-outcome" data-filter="create">To Migrate (${toCreate(run)})</button>
        ${dupCount ? `<button type="button" class="pill-filter ${S.outcomeFilter === "duplicates" ? "active" : ""}" data-action="filter-outcome" data-filter="duplicates">Duplicates (${dupCount})</button>` : ""}
      </div>
      <div class="row">
        ${dupCount && !busy() && S.phase !== "imported" ? `
          <button type="button" class="btn secondary btn-sm" data-action="bulk-dup" data-mode="skip">Skip All Duplicates</button>
          <button type="button" class="btn secondary btn-sm" data-action="bulk-dup" data-mode="duplicate">Create All Duplicates</button>
        ` : ""}
        <button type="button" class="btn secondary btn-sm" data-action="export-validation-csv">Export Table CSV</button>
      </div>
    </div>
    <div class="table-wrap">
      <table class="tickets">
        <thead>
          <tr>
            <th scope="col">Issue Key</th>
            <th scope="col">Issue Type</th>
            <th scope="col">Summary</th>
            <th scope="col">Assignee</th>
            <th scope="col">Status (Jira → ADO Dropdown)</th>
            <th scope="col" class="outcome-h">Outcome &amp; Duplicate Action</th>
          </tr>
        </thead>
        <tbody>${visibleTickets.length ? visibleTickets.map((t) => ticketRow(t, run)).join("") : `<tr><td colspan="6" class="soft">No tickets match the current filter.</td></tr>`}</tbody>
      </table>
    </div>
    ${tabsHtml()}`;
}

function liveStrip() {
  const job = S.job;
  if (!job) return "";
  const step = job.steps.find((s) => s.status === "running");
  const last = S.log.length ? S.log[S.log.length - 1] : "";
  return `<div class="live"><b>${esc(step ? step.label : "Working")}</b><span class="last">${esc(last)}</span>
    <button class="quiet" type="button" data-action="stop">Stop</button></div>`;
}

function ticketRow(t, run) {
  const o = outcome(t, run);
  const sig = o.cls + o.text;
  const changed = S.stamps[t.key] && S.stamps[t.key] !== sig;
  S.stamps[t.key] = sig;
  const isDup = isDuplicateTicket(t);
  const link = o.id ? ` <a href="${esc(run.item_url + o.id)}" target="_blank" rel="noopener">#${esc(o.id)}</a>` : "";

  const metaParts = [
    t.revisions ? plural(t.revisions, "rev") : "",
    t.comments ? plural(t.comments, "comment") : "",
    t.attachments ? plural(t.attachments, "attachment") : "",
    t.worklogs ? plural(t.worklogs, "worklog") : "",
    t.links ? plural(t.links, "link") : "",
    t.hierarchy_auto_included ? "auto-included hierarchy" : "",
  ].filter(Boolean).join(" · ");

  const kinds = { "Jira key in the title": "Jira key in title of", "same title": "Same summary as", "same Jira key": "Exact Jira key on" };
  const dupList = [];
  if (t.outcome === "already_there" && t.ado_id) {
    dupList.push(`<li>⚠ Exact Jira key already in ADO: <a href="${esc(run.item_url + t.ado_id)}" target="_blank" rel="noopener">#${esc(t.ado_id)}</a></li>`);
  }
  for (const mm of (t.matches || [])) {
    const extra = [mm.state || ""].concat((mm.jira_keys || []).map((k) => `from ${k}`)).filter(Boolean).join(", ");
    dupList.push(`<li>⚠ ${esc(kinds[mm.kind] || mm.kind)} <a href="${esc(run.item_url + mm.id)}" target="_blank" rel="noopener">#${esc(mm.id)}</a>${extra ? ` (${esc(extra)})` : ""}: ${esc(mm.title)}</li>`);
  }
  if (t.in_run_duplicates && t.in_run_duplicates.length) {
    dupList.push(`<li>⚠ Duplicate summary in this sprint with: <b>${esc(t.in_run_duplicates.join(", "))}</b></li>`);
  }
  const matchesHtml = dupList.length ? `<ul class="matches">${dupList.join("")}</ul>` : "";

  // Status dropdown populated with valid ADO states for this ticket's work item type
  const curState = effectiveState(t);
  const statePool = Array.from(new Set([...(t.valid_states || []), curState].filter(Boolean)));
  const isEdited = Boolean(S.ticketStates[t.key] || S.statusOverrides[t.jira_status]);
  const isRecommended = !isEdited && (t.state_match_reason === "fuzzy" || t.state_match_reason === "category");
  const reasonLabel = isEdited ? "✎ Edited by you" : ({
    exact: "✓ Identical state match",
    configured: "✓ Mapped state",
    fuzzy: "★ Recommended closest match",
    category: "★ Recommended category match",
  }[t.state_match_reason] || "Mapped state");
  const reasonCls = isEdited ? "override" : (t.state_match_reason === "exact" ? "exact" : (isRecommended ? "recommended" : ""));

  const statusDropdown = statePool.length ? `
    <select class="status-select ${isRecommended ? "recommended-select" : ""}" data-action="ticket-state" data-key="${esc(t.key)}" data-jira-status="${esc(t.jira_status)}" aria-label="Target ADO state for ${esc(t.key)}" ${busy() || S.phase === "imported" ? "disabled" : ""}>
      ${statePool.map((st) => {
        const recMark = st === t.ado_state && (t.state_match_reason === "fuzzy" || t.state_match_reason === "category");
        const label = recMark ? `★ ${st} (Recommended)` : st;
        return `<option value="${esc(st)}" ${st === curState ? "selected" : ""}>${esc(label)}</option>`;
      }).join("")}
    </select>` : `<div><b>${esc(curState)}</b></div>`;

  const applyAllBtn = S.lastChangedKey === t.key ? `
    <div><button type="button" class="quiet" data-action="apply-status-all" data-jira-status="${esc(t.jira_status)}" data-ado-state="${esc(curState)}">Apply "${esc(curState)}" to all "${esc(t.jira_status)}" tickets</button></div>` : "";

  // Per-ticket duplicate / migration action selector: Sync Delta is ONLY allowed for exact Jira key matches (S2)
  const act = effectiveAction(t);
  const canSync = t.outcome === "already_there" && Boolean(t.ado_id);
  const actionSelect = (S.phase !== "imported" && !busy() && (isDup || act === "skip")) ? `
    <div>
      <select class="dup-action-select" data-action="ticket-action" data-key="${esc(t.key)}" aria-label="Action for ${esc(t.key)}">
        <option value="skip" ${act === "skip" ? "selected" : ""}>Skip (Do not create)</option>
        <option value="duplicate" ${act === "duplicate" || act === "create" ? "selected" : ""}>${isDup ? "Create duplicate entry" : "Create in ADO"}</option>
        ${canSync ? `<option value="sync" ${act === "sync" ? "selected" : ""}>Sync delta onto existing #${esc(t.ado_id)}</option>` : ""}
      </select>
    </div>` : "";

  const assigneeText = t.assignee || "Unassigned";
  const assigneeSub = t.ado_assignee ? `<div class="sub">${esc(t.ado_assignee)}</div>`
    : (assigneeText !== "Unassigned" ? `<div class="sub" style="color:var(--amber)">No ADO identity</div>` : "");

  return `
    <tr class="${isDup ? "dup-highlight" : ""}">
      <td class="key">
        <a href="${esc(run.jira_browse + t.key)}" target="_blank" rel="noopener">${esc(t.key)}</a>
        <div><button type="button" class="quiet" data-action="inspect-ticket" data-key="${esc(t.key)}">Inspect</button></div>
      </td>
      <td>
        <span class="type-badge">${esc(t.jira_type)}</span>
        <div class="sub">→ ${esc(t.ado_type)}</div>
      </td>
      <td>
        <div class="title">${esc(t.title)}</div>
        <div class="sub">${esc(metaParts)}</div>
        ${matchesHtml}
      </td>
      <td class="assignee-cell">
        <div>${esc(assigneeText)}</div>
        ${assigneeSub}
      </td>
      <td class="status-cell">
        <div class="sub">Jira: <b>${esc(t.jira_status)}</b></div>
        ${statusDropdown}
        <div class="match-tag ${reasonCls}">${esc(reasonLabel)}</div>
        ${applyAllBtn}
      </td>
      <td class="outcome">
        <span class="stamp ${o.cls}${changed ? " changed" : ""}">${esc(o.text)}${link}</span>
        ${actionSelect}
        ${o.reason ? `<div class="reason">${esc(o.reason)}</div>` : ""}
      </td>
    </tr>`;
}

/* ------------------------------------------------------------ rendering: detail tabs */

function tabsHtml() {
  const pv = S.run && S.run.preview;
  const tabs = pv ? [["where", "Where they go"], ["statuses", "Statuses"], ["fields", "Fields"],
    ["people", `People${pv.people_without_identity.length ? ` (${pv.people_without_identity.length})` : ""}`],
    ["warnings", `Warnings${pv.warnings.length ? ` (${pv.warnings.length})` : ""}`], ["log", "Log"]] : [["log", "Log"]];
  const tab = tabs.some(([id]) => id === S.tab) ? S.tab : tabs[0][0];
  return `
    <div class="tabs">
      <div class="tablist" role="tablist">${tabs.map(([id, label]) =>
        `<button class="tab" role="tab" type="button" data-action="tab" data-tab="${id}" aria-selected="${id === tab}">${esc(label)}</button>`).join("")}</div>
      <div class="panel" role="tabpanel">${panelHtml(tab, pv)}</div>
    </div>`;
}

function panelHtml(tab, pv) {
  if (tab === "log") {
    return S.log.length ? `<pre class="log" id="log">${esc(S.log.join("\n"))}</pre>`
      : `<p class="soft">The live log of validation or migration in this window appears here.</p>`;
  }
  if (tab === "where") {
    const files = (S.run && S.run.files) || {};
    return `<table><thead><tr><th>Items</th><th>Area Path</th><th>Iteration Path</th><th>Status</th></tr></thead><tbody>
      ${pv.places.map((p) => `<tr><td class="num">${p.count}</td><td>${esc(p.area)}</td><td>${esc(p.iteration)}</td>
        <td class="${p.iteration_exists === false ? "note" : "soft"}">${p.iteration_exists === false ? "Will be created & subscribed during migration" : p.iteration_exists ? "Exists in ADO" : ""}</td></tr>`).join("")}
      </tbody></table>
      <p class="soft">Migration mode: <b>${esc(pv.mode)}</b> · Links: <b>${esc(pv.links)}</b>.</p>
      ${files["duplicates.csv"] ? `<div class="row"><button class="btn secondary btn-sm" type="button" data-action="download-file" data-path="${esc(files["duplicates.csv"])}" data-name="duplicates.csv">Download Duplicate Report CSV</button></div>` : ""}`;
  }
  if (tab === "statuses") {
    const hasRecommended = pv.statuses.some((s) => s.needs_attention || s.match_reason === "fuzzy" || s.match_reason === "category");
    const banner = hasRecommended ? `
      <div class="banner warn">
        <p><b>★ Highlighted Statuses:</b> Rows marked with <b>★ Recommended</b> do not have an identically named state on the destination work item type in <b>${esc(pv.project)}</b>. Jira2ADO has highlighted the closest matching Azure DevOps state automatically. You can override any status using the dropdown in the ticket table above, or add custom states in Azure DevOps (<i>Organization Settings → Process</i>) and click <b>Validate again</b>.</p>
      </div>` : "";
    return `${banner}<table><thead><tr><th>ADO Work Item Type</th><th>Jira Status</th><th>Target ADO State</th><th>Match Type</th><th>Tickets</th><th>Status Guidance</th></tr></thead><tbody>
      ${pv.statuses.map((s) => {
        const cur = S.statusOverrides[s.jira] || s.ado;
        const needsAttention = Boolean(s.needs_attention || s.match_reason === "fuzzy" || s.match_reason === "category");
        const recCat = s.recommended_ado_category || "InProgress";
        return `<tr class="${needsAttention ? "status-attention" : ""}">
          <td>${esc(s.type)}</td>
          <td><b>${esc(s.jira)}</b></td>
          <td><b>${needsAttention ? `★ ${esc(cur)}` : esc(cur)}</b>${s.exists === false ? ` <span class="flag">not a state in ${esc(pv.project)}</span>` : ""}</td>
          <td class="soft">${esc(s.match_reason || (s.mapped ? "configured" : "category"))}</td>
          <td class="num">${s.count}</td>
          <td>${needsAttention
            ? `<span class="match-tag recommended">★ Recommended fallback — no "${esc(s.jira)}" state in ADO (Category: ${esc(recCat)})</span>`
            : `<span class="match-tag exact">✓ Exact / configured match</span>`}</td>
        </tr>`;
      }).join("")}
      </tbody></table>`;
  }
  if (tab === "fields") {
    const missing = Object.entries(pv.missing_fields || {});
    return `<table><thead><tr><th>Azure DevOps Field</th><th>Mapped from Jira Field</th></tr></thead><tbody>
      ${pv.custom_fields.length ? pv.custom_fields.map((f) => `<tr><td>${esc(f.label)} <span class="soft">(${esc(f.ado)})</span></td><td>${f.jira ? esc(f.jira) : `<span class="soft">Auto-captured in history if present</span>`}</td></tr>`).join("") : `<tr><td colspan="2" class="soft">Standard fields + automatic capture of all unmapped custom fields enabled.</td></tr>`}
      </tbody></table>
      ${missing.some(([, v]) => v.length) ? `<p>Fields not present on the destination work item type (their values are automatically preserved in the work item's <i>From Jira</i> note so zero data is lost):</p>
        <ul>${missing.filter(([, v]) => v.length).map(([wit, v]) => `<li><b>${esc(wit)}</b>: ${esc(v.join(", "))}</li>`).join("")}</ul>` : ""}`;
  }
  if (tab === "people") {
    const list = pv.people_list || [];
    if (!list.length) return `<p>Everyone in this run has an Azure DevOps identity.</p>`;
    return `
      <p class="soft">Edit Azure DevOps identities directly below and click <b>Save User Mappings</b>. Unmapped users will have their Jira display name preserved on comments and history notes.</p>
      <table>
        <thead><tr><th>Jira Display Name</th><th>Jira Email</th><th>Azure DevOps Identity (Email / UPN)</th></tr></thead>
        <tbody>
          ${list.map((u) => {
            const val = S.peopleEdits[u.account_id] !== undefined ? S.peopleEdits[u.account_id] : (u.ado_identity || "");
            return `<tr>
              <td><b>${esc(u.name)}</b></td>
              <td class="soft">${esc(u.jira_email || "—")}</td>
              <td><input type="text" data-action="edit-person" data-account="${esc(u.account_id)}" value="${esc(val)}" placeholder="user@company.com"></td>
            </tr>`;
          }).join("")}
        </tbody>
      </table>
      <div class="row" style="margin-top:12px">
        <button type="button" class="btn secondary btn-sm" data-action="save-people" ${S.savingPeople ? "disabled" : ""}>${S.savingPeople ? "Saving..." : "Save User Mappings"}</button>
      </div>`;
  }
  if (tab === "warnings") {
    return pv.warnings.length ? `<ul>${pv.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : `<p>No warnings.</p>`;
  }
  return "";
}

function safeDescriptionSrcdoc(rawHtml) {
  const clean = String(rawHtml || "<p><i>(No description)</i></p>")
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "")
    .replace(/\sdata-action\s*=/gi, " data-ignored=")
    .replace(/\son[a-z]+\s*=/gi, " data-ignored-event=");
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 13.5px; line-height: 1.5; color: #1b2333; margin: 10px; overflow-wrap: anywhere; }
    pre, code { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; background: #f3f5fa; padding: 2px 4px; border-radius: 4px; }
    pre { padding: 8px; overflow-x: auto; }
    table { border-collapse: collapse; width: 100%; margin: 8px 0; }
    th, td { border: 1px solid #d6dce6; padding: 6px 8px; text-align: left; }
    img { max-width: 100%; height: auto; }
  </style></head><body>${clean}</body></html>`;
}

/* ------------------------------------------------------------ Drawer: Ticket Inspector & Help Guide */

function renderDrawer() {
  const drawer = $("#drawer");
  const backdrop = $("#drawer-backdrop");
  if (!S.drawerMode) {
    drawer.classList.add("hidden");
    backdrop.classList.add("hidden");
    return;
  }
  drawer.classList.remove("hidden");
  backdrop.classList.remove("hidden");

  if (S.drawerMode === "help") {
    drawer.innerHTML = `
      <div class="drawer-head">
        <h2>Jira2ADO — Help &amp; Setup Guide</h2>
        <button type="button" class="btn secondary btn-sm" data-action="close-drawer">Close</button>
      </div>
      <div class="drawer-body">
        <h3>1. Required Credentials &amp; Minimum Permissions</h3>
        <p>Jira2ADO is <b>100% read-only on Jira</b> and writes only to your chosen Azure DevOps destination project:</p>
        <ul>
          <li><b>Atlassian Jira URL</b>: <code>https://your-org.atlassian.net</code> (HTTPS required)</li>
          <li><b>Jira API Token</b>: Create one at <code>https://id.atlassian.com/manage-profile/security/api-tokens</code> (only read access to the source project and board is needed)</li>
          <li><b>Azure DevOps Organization URL</b>: <code>https://dev.azure.com/your-org</code> (HTTPS required)</li>
          <li><b>Azure DevOps PAT (Personal Access Token)</b>: Create one in Azure DevOps under <i>User Settings → Personal access tokens</i> with minimum scopes:
            <ul>
              <li><b>Work Items</b>: <i>Read &amp; write</i> (required)</li>
              <li><b>Project and team</b>: <i>Read &amp; write</i> (optional — only needed when auto-creating sprint iterations and subscribing team boards)</li>
            </ul>
          </li>
        </ul>

        <h3>2. Enabling "Bypass Rules" in Azure DevOps</h3>
        <p>Jira2ADO uses the Azure DevOps <code>bypassRules=true</code> flag so that migrated work items preserve their original Jira <code>CreatedDate</code>, <code>ChangedDate</code>, <code>ClosedDate</code>, and workflow states across all revisions:</p>
        <ol>
          <li>Open your Azure DevOps Project → <b>Project Settings</b> → <b>Permissions</b> → <b>Users</b>.</li>
          <li>Select your user account.</li>
          <li>Set <b>Bypass rules on work item updates</b> to <b>Allow</b>.</li>
        </ol>

        <h3>3. Dynamic Status Discovery &amp; Highlighted Recommendations</h3>
        <p>When you click <b>Validate</b>, Jira2ADO queries both Jira and your destination Azure DevOps project for all live statuses and state categories:</p>
        <ul>
          <li><b>Identical Match (✓)</b>: If your ADO project has a state with the exact same name as Jira, it is selected automatically.</li>
          <li><b>Highlighted Recommendation (★)</b>: When no identically named state exists in ADO, Jira2ADO highlights the closest matching state with a <b>★ Recommended</b> badge in both the ticket dropdown and the <i>Statuses</i> tab.</li>
          <li><b>Interactive Override</b>: Change any ticket's target state via its dropdown, or click <i>Apply to all</i> to update every ticket with that Jira status.</li>
        </ul>

        <h3>4. Duplicate Detection &amp; Safe Resolution</h3>
        <p>Jira2ADO checks three levels of duplicates before creating anything:</p>
        <ul>
          <li><b>Exact Jira Key Match</b>: An item in ADO already carries the exact Jira key in its tags or <code>Custom.JiraKey</code> field.</li>
          <li><b>Possible Duplicate</b>: An ADO item's title contains the Jira key or matches the Jira summary.</li>
          <li><b>In-Run Duplicate</b>: Two tickets inside the chosen sprint share the exact same summary.</li>
        </ul>
        <p>For each duplicate, you can choose:</p>
        <ul>
          <li><b>Skip</b>: Leave the ticket out so no duplicate is created.</li>
          <li><b>Create Duplicate Entry</b>: Create a new work item in ADO tagged <code>Possible duplicate</code> and linked to the existing item.</li>
          <li><b>Sync Delta (Exact Key Matches Only)</b>: Safely update an existing work item that shares the exact Jira key with the latest state, fields, comments, and attachments.</li>
        </ul>

        <h3>5. What Data is Migrated ("EVERYTHING")</h3>
        <ul>
          <li>Summary, Description (ADF &amp; Wiki markup converted to HTML), Status, Priority, Assignee, Reporter/Creator, Story Points / Effort.</li>
          <li>All <b>Attachments</b> &amp; inline images (links rewritten to point to the uploaded ADO copies).</li>
          <li>Full <b>Change History</b> (each Jira change becomes an ADO revision with original author &amp; timestamp).</li>
          <li>All <b>Comments</b> with original author &amp; timestamp, plus resolved <code>@mentions</code>.</li>
          <li>All <b>Worklogs &amp; Time Tracking</b> (Original Estimate, Completed Work, Remaining Work, and worklog notes).</li>
          <li>All <b>Links</b>: Parent Epics, Child Sub-tasks, Issue Links (Blocks, Relates, Duplicate), cross-ticket link rewriting, Remote Web/Confluence Links, and Git PR/Branch/Commit links.</li>
        </ul>
      </div>`;
    return;
  }

  if (S.drawerMode === "ticket" && S.run && S.run.preview) {
    const t = S.run.preview.tickets.find((x) => x.key === S.drawerTicketKey);
    if (!t) { S.drawerMode = null; renderDrawer(); return; }
    const cfs = Object.entries(t.custom_fields || {});
    drawer.innerHTML = `
      <div class="drawer-head">
        <h2>${esc(t.key)} — Ticket Inspector</h2>
        <button type="button" class="btn secondary btn-sm" data-action="close-drawer">Close</button>
      </div>
      <div class="drawer-body">
        <h3>Overview</h3>
        <dl class="kv-grid">
          <dt>Summary</dt><dd><b>${esc(t.title)}</b></dd>
          <dt>Type Mapping</dt><dd>${esc(t.jira_type)} → <b>${esc(t.ado_type)}</b></dd>
          <dt>Status Mapping</dt><dd>${esc(t.jira_status)} → <b>${esc(effectiveState(t))}</b> (${esc(t.state_match_reason)})</dd>
          <dt>Assignee</dt><dd>${esc(t.assignee)} ${t.ado_assignee ? `(${esc(t.ado_assignee)})` : ""}</dd>
          <dt>Reporter</dt><dd>${esc(t.reporter || "—")}</dd>
          <dt>Priority</dt><dd>${esc(t.priority || "—")}</dd>
          <dt>Target Area</dt><dd>${esc(t.area || "—")}</dd>
          <dt>Target Iteration</dt><dd>${esc(t.iteration || "—")}</dd>
          <dt>Tags</dt><dd>${esc(t.tags || "—")}</dd>
          <dt>History &amp; Media</dt><dd>${plural(t.revisions, "revision")}, ${plural(t.comments, "comment")}, ${plural(t.attachments, "attachment")}, ${plural(t.worklogs || 0, "worklog")}, ${plural(t.links || 0, "link")}</dd>
        </dl>

        ${cfs.length ? `
          <h3>Mapped Custom &amp; Scheduling Fields</h3>
          <dl class="kv-grid">
            ${cfs.map(([k, v]) => `<dt>${esc(k.split(".").pop())}</dt><dd>${esc(typeof v === "string" ? v.replace(/<[^>]+>/g, " ").slice(0, 200) : v)}</dd>`).join("")}
          </dl>` : ""}

        ${(t.matches && t.matches.length) ? `
          <h3>Matched Duplicates in Azure DevOps</h3>
          <ul>
            ${t.matches.map((m) => `<li><b>#${esc(m.id)}</b> (${esc(m.kind)}, State: ${esc(m.state || "—")}): ${esc(m.title)}</li>`).join("")}
          </ul>` : ""}

        <h3>Rendered Description Preview</h3>
        <iframe class="html-preview-frame" sandbox="" title="Rendered Description Preview" srcdoc="${esc(safeDescriptionSrcdoc(t.description_html))}"></iframe>
      </div>`;
  }
}

function render() {
  if (S.quitting) return;
  renderWho();
  renderConnect();
  renderTickets();
  renderDestination();
  renderCheck();
  renderImport();
  renderManifest();
  renderDrawer();
  const log = $("#log");
  if (log) log.scrollTop = log.scrollHeight;
}

/* ------------------------------------------------------------ loading choices */

async function loadProjects() {
  try {
    const d = await api("/api/jira/projects");
    S.projects = d.projects;
    const wanted = S.settings.source || (S.projects[0] && S.projects[0].key) || "";
    if (!S.source && wanted && S.projects.some((p) => p.key === wanted)) {
      S.source = wanted;
      loadBoardsAndSprints();
    }
  } catch (e) {
    S.projectsError = e.message;
  }
  render();
}

async function loadBoardsAndSprints() {
  if (!S.source) return;
  S.boardsLoading = true;
  S.boards = [];
  render();
  try {
    const b = await api(`/api/jira/boards?project=${encodeURIComponent(S.source)}`);
    S.boards = b.boards || [];
    if (S.settings.board && S.boards.some((x) => String(x.id) === String(S.settings.board))) {
      S.board = String(S.settings.board);
    } else {
      S.board = "";
    }
  } catch (e) {
    S.boards = [];
  }
  S.boardsLoading = false;
  await loadSprints();
}

async function loadSprints() {
  S.sprintsLoading = true;
  S.sprintsError = "";
  S.sprints = [];
  S.picked = new Set();
  render();
  try {
    const qs = `project=${encodeURIComponent(S.source)}` + (S.board ? `&board=${encodeURIComponent(S.board)}` : "");
    const d = await api(`/api/jira/sprints?${qs}`);
    S.sprints = d.sprints;
    S.sprints.filter((sp) => sp.state === "active").forEach((sp) => S.picked.add(String(sp.id)));
  } catch (e) {
    S.sprintsError = e.message;
  }
  S.sprintsLoading = false;
  render();
}

async function loadTargets() {
  try {
    const d = await api("/api/targets");
    S.targets = d.targets;
    const wanted = S.settings.target;
    if (!S.target && wanted && S.targets.some((t) => t.name === wanted)) S.target = wanted;
  } catch (e) {
    toast(e.message);
  }
  render();
}

async function loadIterations() {
  const name = targetName();
  if (!name || S.iterationsFor === name) return;
  S.iterationsError = "";
  try {
    const d = await api(`/api/ado/iterations?project=${encodeURIComponent(name)}`);
    S.iterations = d.iterations || [];
    S.areas = d.areas || [];
    S.iterationsFor = name;
  } catch (e) {
    S.iterationsError = e.message;
  }
  render();
}

/* ------------------------------------------------------------ actions */

async function connect(form) {
  const jUrl = form.querySelector("#jira-url").value.trim();
  const email = form.querySelector("#jira-email").value.trim();
  const jt = form.querySelector("#jira-token").value.trim();
  const aUrl = form.querySelector("#ado-url").value.trim();
  const at = form.querySelector("#ado-token").value.trim();
  if (!email || !jt || !at || !jUrl || !aUrl) {
    S.problems = {
      jira: !jUrl || !email || !jt ? "Fill in the Jira URL, email, and API token." : "",
      ado: !aUrl || !at ? "Fill in the Azure DevOps URL and PAT." : "",
    };
    render();
    return;
  }
  if (isDoublePaste(jt) || isDoublePaste(at)) {
    S.problems = {
      jira: isDoublePaste(jt) ? "That looks like the Jira API token pasted twice. Paste it once." : "",
      ado: isDoublePaste(at) ? "That looks like the Azure DevOps token pasted twice. Paste it once." : "",
    };
    render();
    return;
  }
  S.connecting = true;
  S.problems = {};
  render();
  try {
    const d = await api("/api/connect", {
      jira_url: jUrl, jira_email: email, jira_token: jt, ado_url: aUrl, ado_token: at,
    });
    S.jira = d.jira;
    S.ado = d.ado;
    S.jiraUrl = d.jira_url || jUrl;
    S.adoUrl = d.ado_url || aUrl;
    S.problems = d.problems || {};
    S.settings.jira_email = email;
    S.settings.jira_url = S.jiraUrl;
    S.settings.ado_url = S.adoUrl;
    if (connected() && !Object.keys(S.problems).length) {
      S.editConnect = false;
      loadProjects();
      loadTargets();
    }
  } catch (e) {
    S.problems = { jira: e.message };
  }
  S.connecting = false;
  render();
}

async function startCheck() {
  S.error = "";
  S.log = [];
  S.since = 0;
  S.run = null;
  S.stamps = {};
  S.confirm = "";
  S.choice = "skip";
  S.ticketStates = {};
  S.statusOverrides = {};
  S.ticketActions = {};
  S.lastChangedKey = "";
  try {
    S.job = await api("/api/check", {
      source: S.source,
      board: S.board || null,
      target: targetName(),
      sprints: [...S.picked],
      iteration: chosenIteration(),
      area: S.area || null,
      include_done: S.includeDone,
      include_hierarchy: S.includeHierarchy,
      history: S.historyMode,
    });
    S.phase = "checking";
    S.checkedSig = "";
    S.tab = "log";
    render();
    poll();
  } catch (e) {
    S.error = e.message;
    render();
  }
}

async function persistOverrides() {
  if (!S.run || !S.run.config) return;
  try {
    await api("/api/override", {
      config: S.run.config,
      ticket_states: S.ticketStates,
      status_overrides: S.statusOverrides,
      ticket_actions: S.ticketActions,
    });
  } catch (e) {
    toast(e.message);
  }
}

async function startImport() {
  S.error = "";
  const pv = S.run.preview;
  try {
    S.job = await api("/api/import", {
      config: S.run.config,
      possible_duplicates: S.choice,
      ticket_states: S.ticketStates,
      status_overrides: S.statusOverrides,
      ticket_actions: S.ticketActions,
      confirm: pv.confirm_with_project_name ? S.confirm.trim() : "yes",
    });
    S.log = [];
    S.since = 0;
    S.phase = "importing";
    render();
    poll();
  } catch (e) {
    S.error = e.message;
    render();
  }
}

async function startRollback() {
  if (!S.run || !S.run.config || !S.run.preview) return;
  const pv = S.run.preview;
  let confirmToken = "yes";
  if (pv.confirm_with_project_name) {
    const typed = S.confirm.trim() === pv.project
      ? pv.project
      : (prompt(`Type "${pv.project}" to confirm rolling back this migration (work items created during this run will be moved to the Recycle Bin):`) || "").trim();
    if (typed !== pv.project) {
      toast(`Type "${pv.project}" to confirm rollback.`);
      return;
    }
    confirmToken = typed;
  } else if (!confirm("Are you sure you want to roll back this migration? Work items created during this run will be moved to the Azure DevOps Recycle Bin (pre-existing items are never deleted).")) {
    return;
  }
  S.error = "";
  try {
    S.job = await api("/api/rollback", { config: S.run.config, confirm: confirmToken });
    S.log = [];
    S.since = 0;
    S.phase = "rolling_back";
    S.tab = "log";
    render();
    poll();
  } catch (e) {
    toast(e.message);
  }
}

function exportValidationCsv() {
  if (!S.run || !S.run.preview) return;
  const rows = [["Jira Key", "Jira Type", "ADO Type", "Summary", "Assignee", "ADO Identity", "Jira Status", "Target ADO State", "Duplicate / Outcome", "Action"]];
  for (const t of S.run.preview.tickets) {
    const o = outcome(t, S.run);
    rows.push([
      t.key, t.jira_type, t.ado_type, t.title, t.assignee || "Unassigned",
      t.ado_assignee || "", t.jira_status, effectiveState(t), o.text, effectiveAction(t),
    ]);
  }
  const csvContent = rows.map((r) => r.map((v) => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`).join(",")).join("\n");
  const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `validation-${S.source || "tickets"}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

async function poll() {
  if (!S.job) return;
  try {
    const d = await api(`/api/jobs/${S.job.id}?since=${S.since}`);
    S.log = S.log.concat(d.log);
    S.since = d.next;
    S.job = d;
    if ((d.kind === "import" || d.kind === "rollback") && S.run) {
      const fresh = await api(`/api/run?config=${encodeURIComponent(S.run.config)}`);
      S.run = { ...fresh, preview: fresh.preview || S.run.preview };
    }
    if (d.status === "running") {
      render();
      setTimeout(poll, 800);
      return;
    }
    if (d.kind === "check") {
      if (d.status === "done" && d.result.run) {
        S.run = d.result.run;
        S.phase = "checked";
        S.checkedSig = signature();
        S.checkedAt = new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });
        S.tab = "where";
      } else {
        S.phase = "setup";
        S.error = d.status === "stopped" ? "Validation was stopped." : (d.error || "Validation failed. See the Log tab for details.");
      }
    } else if (d.kind === "rollback") {
      if (d.result && d.result.run) S.run = { ...d.result.run, preview: S.run.preview };
      S.phase = "checked";
      toast(d.status === "done" ? "Rollback completed. Work items created in this run were removed." : (d.error || "Rollback failed."));
    } else {
      if (d.result && d.result.run) S.run = { ...d.result.run, preview: S.run.preview };
      if (d.status === "done") {
        S.phase = "imported";
        S.error = "";
      } else {
        S.phase = "import_failed";
        S.error = d.status === "stopped"
          ? "Migration was stopped. Click Retry / Resume Remaining to continue where it left off."
          : (d.error || "Migration failed. See the Log tab for details, then click Retry / Resume Remaining.");
      }
    }
    render();
  } catch (e) {
    toast(e.message);
    setTimeout(poll, 2000);
  }
}

document.addEventListener("submit", (e) => {
  const form = e.target.closest("[data-action=connect-form]");
  if (form) {
    e.preventDefault();
    connect(form);
  }
});

document.addEventListener("click", async (e) => {
  const el = e.target.closest("[data-action]");
  if (!el || el.tagName === "INPUT" || el.tagName === "SELECT" || el.tagName === "FORM") return;
  const action = el.dataset.action;
  if (action === "edit-connect") { S.editConnect = true; S.problems = {}; render(); }
  if (action === "cancel-connect") { S.editConnect = false; render(); }
  if (action === "check") startCheck();
  if (action === "import") startImport();
  if (action === "rollback") startRollback();
  if (action === "export-validation-csv") exportValidationCsv();
  if (action === "download-file") downloadServerFile(el.dataset.path, el.dataset.name);
  if (action === "open-help") { S.drawerMode = "help"; renderDrawer(); }
  if (action === "close-drawer") { S.drawerMode = null; renderDrawer(); }
  if (action === "inspect-ticket") {
    S.drawerMode = "ticket";
    S.drawerTicketKey = el.dataset.key;
    renderDrawer();
  }
  if (action === "select-displayed-sprints") {
    const boxes = document.querySelectorAll('#step-tickets .sprints input[data-action="pick-sprint"]');
    boxes.forEach((cb) => {
      if (!cb.disabled) S.picked.add(String(cb.value));
    });
    render();
  }
  if (action === "clear-displayed-sprints") {
    const boxes = document.querySelectorAll('#step-tickets .sprints input[data-action="pick-sprint"]');
    boxes.forEach((cb) => {
      S.picked.delete(String(cb.value));
    });
    render();
  }
  if (action === "clear-all-sprints") {
    S.picked.clear();
    render();
  }
  if (action === "filter-outcome") {
    S.outcomeFilter = el.dataset.filter;
    renderManifest();
  }
  if (action === "bulk-dup" && S.run && S.run.preview) {
    const mode = el.dataset.mode;
    if (mode !== "skip" && mode !== "duplicate") return;
    for (const t of S.run.preview.tickets) {
      if (isDuplicateTicket(t)) {
        S.ticketActions[t.key] = mode;
      }
    }
    persistOverrides();
    render();
    toast(`Set all duplicates to: ${mode === "skip" ? "Skip" : "Create Duplicate"}`);
  }
  if (action === "apply-status-all") {
    const jst = el.dataset.jiraStatus;
    const ast = el.dataset.adoState;
    S.statusOverrides[jst] = ast;
    if (S.run && S.run.preview) {
      for (const t of S.run.preview.tickets) {
        if (t.jira_status === jst) S.ticketStates[t.key] = ast;
      }
    }
    S.lastChangedKey = "";
    persistOverrides();
    render();
    toast(`Mapped all "${jst}" tickets to "${ast}".`);
  }
  if (action === "save-people" && S.run) {
    S.savingPeople = true;
    render();
    try {
      await api("/api/users/update", { config: S.run.config, users: S.peopleEdits });
      toast("Saved user identity mappings to users.csv.");
    } catch (err) {
      toast(err.message);
    }
    S.savingPeople = false;
    render();
  }
  if (action === "stop" && S.job) {
    try { await api(`/api/jobs/${S.job.id}/stop`, {}); } catch (err) { toast(err.message); }
  }
  if (action === "tab") { S.tab = el.dataset.tab; render(); }
  if (action === "open-file" || action === "reveal") {
    try {
      const d = await api("/api/open", { path: el.dataset.path, reveal: action === "reveal" });
      if (!d.ok) toast(`The file is at ${d.path}`);
    } catch (err) { toast(err.message); }
  }
});

document.addEventListener("change", (e) => {
  const el = e.target;
  const action = el.dataset && el.dataset.action;
  if (!action) return;
  if (action === "select-project") {
    S.source = el.value;
    S.board = "";
    S.sprintFilter = "";
    if (S.source) loadBoardsAndSprints();
    else { S.boards = []; S.sprints = []; S.picked = new Set(); render(); }
  }
  if (action === "select-board") {
    S.board = el.value;
    S.sprintFilter = "";
    loadSprints();
  }
  if (action === "pick-sprint") { if (el.checked) S.picked.add(el.value); else S.picked.delete(el.value); render(); }
  if (action === "include-done") { S.includeDone = el.checked; render(); }
  if (action === "include-hierarchy") { S.includeHierarchy = el.checked; render(); }
  if (action === "history-mode") { S.historyMode = el.checked; render(); }
  if (action === "select-target") {
    S.isOther = el.value === "__other";
    if (!S.isOther) S.target = el.value;
    S.iteration = "";
    render();
    loadIterations();
  }
  if (action === "iter-mode") { S.iterMode = el.value; render(); loadIterations(); }
  if (action === "select-iteration") { S.iteration = el.value; render(); }
  if (action === "choice") { S.choice = el.value; render(); }
  if (action === "other-target") { S.other = el.value; S.iterationsFor = ""; render(); loadIterations(); }
  if (action === "ticket-state") {
    const key = el.dataset.key;
    S.ticketStates[key] = el.value;
    S.lastChangedKey = key;
    persistOverrides();
    render();
  }
  if (action === "ticket-action") {
    const key = el.dataset.key;
    S.ticketActions[key] = el.value;
    persistOverrides();
    render();
  }
});

document.addEventListener("input", (e) => {
  const el = e.target;
  const action = el.dataset && el.dataset.action;
  if (action === "confirm") {
    S.confirm = el.value;
    const btn = document.querySelector("[data-action=import]");
    if (btn) btn.disabled = S.confirm.trim() !== S.run.preview.project;
  }
  if (action === "new-iteration") {
    S.newIteration = el.value;
    const check = document.querySelector("[data-action=check]");
    if (check) check.disabled = !(S.source && S.picked.size && targetName() && chosenIteration()) || busy();
  }
  if (action === "other-target") S.other = el.value;
  if (action === "filter-sprints") {
    S.sprintFilter = el.value;
    renderTickets();
    renderCheck();
    renderImport();
    const input = document.querySelector("[data-action=filter-sprints]");
    if (input) {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
  }
  if (action === "search-tickets") {
    S.searchFilter = el.value;
    renderManifest();
    const input = document.querySelector("[data-action=search-tickets]");
    if (input) {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
  }
  if (action === "edit-person") {
    S.peopleEdits[el.dataset.account] = el.value;
  }
});

$("#quit").addEventListener("click", async () => {
  if (busy()) { toast("A run is still going. Stop it first, or wait for it to finish."); return; }
  try { await api("/api/quit", {}); } catch (e) { /* stopping */ }
  S.quitting = true;
  document.body.innerHTML = `<div class="stopped-page"><h1>jira2ado has stopped</h1>
    <p>All tokens have been cleared from memory. You can close this browser tab.</p></div>`;
});

async function init() {
  if (!KEY) {
    document.body.innerHTML = `<div class="stopped-page"><h1>Open jira2ado from its launcher</h1>
      <p>This page requires the session key printed in the terminal window that started jira2ado.</p></div>`;
    return;
  }
  try {
    const d = await api("/api/state");
    S.jira = d.jira;
    S.ado = d.ado;
    S.jiraUrl = d.jira_url || "";
    S.adoUrl = d.ado_url || "";
    S.settings = d.settings || {};
    if (connected()) {
      loadProjects();
      loadTargets();
    }
    if (d.running) {
      S.job = await api(`/api/jobs/${d.running}`);
      S.phase = S.job.kind === "check" ? "checking" : (S.job.kind === "rollback" ? "rolling_back" : "importing");
      poll();
    }
  } catch (e) {
    toast(e.message);
  }
  render();
}

init();
