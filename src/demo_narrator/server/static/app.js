/* demo-narrator web UI.
   Flow follows docs/design/Demo Narrator UI.dc.html (turns 2 + 3):
   Projects -> a 4-step run (work items, flows & beats, narration, render),
   with Jobs and Library as top-level destinations and real share links.
   Hash routing, no build step. */

const view = document.getElementById("view");
const chrome = document.getElementById("chrome");

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const enc = encodeURIComponent;

const api = async (path, opts) => {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res.text();
};
const post = (path, body, method = "POST") =>
  api(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

let pollTimer = null;
const logOffsets = {};

/* ---------- small helpers ---------- */

function toast(message, kind = "err") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = String(message).slice(0, 300);
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 6000);
}
async function act(promise) {
  try { return await promise; } catch (e) { toast(e.message); throw e; }
}
async function busyClick(btn, fn, { busyLabel = "Working…", keepDisabled = false } = {}) {
  if (!btn || btn.dataset.busy === "1") return;
  btn.dataset.busy = "1";
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = busyLabel;
  try {
    await fn();
  } finally {
    delete btn.dataset.busy;
    if (!keepDisabled) { btn.disabled = false; btn.innerHTML = orig; }
  }
}
const words = (s) => (s || "").trim().split(/\s+/).filter(Boolean).length;

const mark = (sec) => {
  const s = Math.floor(sec || 0);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

function clock(sec) {
  if (sec == null) return "—";
  const s = Math.round(sec);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}
const parseTs = (iso) => new Date(/[Z+]|-\d{2}:\d{2}$/.test(iso.slice(10)) ? iso : iso + "Z");

function ago(iso) {
  if (!iso) return "—";
  const mins = Math.round((Date.now() - parseTs(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  if (mins < 1440) return `${Math.floor(mins / 60)}h ago`;
  return `${Math.floor(mins / 1440)}d ago`;
}
// Expiries point forwards, so they need their own wording.
function until(iso) {
  if (!iso) return "Never";
  const mins = Math.round((parseTs(iso).getTime() - Date.now()) / 60000);
  if (mins <= 0) return "Expired";
  if (mins < 60) return `in ${mins}m`;
  if (mins < 1440) return `in ${Math.round(mins / 60)}h`;
  return `in ${Math.round(mins / 1440)}d`;
}

const hhmm = (iso) => (iso
  ? parseTs(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—");

function duration(job) {
  if (!job.started_at || !job.finished_at) return "—";
  return clock((parseTs(job.finished_at) - parseTs(job.started_at)) / 1000);
}

/* ---------- header env chips ---------- */

async function loadMeta() {
  try {
    const m = await api("/api/meta");
    document.getElementById("envClaude").textContent = `claude · ${m.claude_mode}`;
    document.getElementById("envTts").textContent = `tts · ${m.tts_provider}`;
    window.__meta = m;
  } catch { /* header chips are decoration; never block the app on them */ }
}

/* ---------- run state (survives a reload, one run at a time) ---------- */

const RUN_KEY = "dn.run";
let run = null;

function loadRun(project) {
  if (run && run.project === project) return run;
  try {
    const saved = JSON.parse(sessionStorage.getItem(`${RUN_KEY}.${project}`) || "null");
    if (saved) { run = saved; return run; }
  } catch { /* corrupt state just starts a fresh run */ }
  run = {
    project, sprint: null, sprintNum: null, tasks: [],
    maxSteps: 14, polish: true,
    flows: [], captions: true, bookends: true, provider: null,
    generated: [],
  };
  return run;
}
function saveRun() {
  if (run) sessionStorage.setItem(`${RUN_KEY}.${run.project}`, JSON.stringify(run));
}

/* ---------- router ---------- */

function route() {
  clearInterval(pollTimer);
  chrome.innerHTML = "";
  const hash = location.hash || "#/";
  const nav = (name) => {
    document.querySelectorAll("nav.tabs a").forEach((a) => a.classList.remove("active"));
    const el = document.querySelector(`[data-nav="${name}"]`);
    if (el) el.classList.add("active");
  };

  if (hash.startsWith("#/p/")) {
    nav("projects");
    const parts = hash.slice(4).split("/").map(decodeURIComponent);
    const project = parts[0];
    if (parts[1] === "flow" && parts[2]) return renderFlowEditor(project, parts[2]);
    if (parts[1] === "profile") return renderProfileEditor(project);
    if (parts[1] === "setup") return renderSetupAssistant(project);
    if (parts[1] === "run") return renderWizard(project, Number(parts[2] || 1));
    return (location.hash = `#/p/${enc(project)}/run/1`);
  }
  if (hash.startsWith("#/r/")) { nav("library"); return renderRender(decodeURIComponent(hash.slice(4))); }
  if (hash === "#/library") { nav("library"); return renderLibrary(); }
  if (hash === "#/jobs") { nav("jobs"); return renderJobs(); }
  nav("projects");
  renderProjects();
}
window.addEventListener("hashchange", route);

/* =======================================================================
   2a · Projects
   ======================================================================= */

async function renderProjects() {
  view.innerHTML = `
    <div class="pagehead">
      <div>
        <h1>Projects</h1>
        <p>One profile per target app — <span class="mono">profiles/&lt;name&gt;.yaml</span></p>
      </div>
      <div class="actions">
        <button id="newProject" class="primary">+ New project</button>
      </div>
    </div>
    <div id="newForm"></div>
    <div class="grid2" id="plist"><div class="muted">Loading projects…</div></div>
    <div class="card pad0" style="margin-top:24px">
      <div class="cardhead"><strong>Latest jobs</strong><a class="link" href="#/jobs">All jobs</a></div>
      <div id="latestJobs"><div class="muted" style="padding:14px 18px">Loading…</div></div>
    </div>`;

  document.getElementById("newProject").onclick = toggleNewProject;

  const projects = await act(api("/api/projects"));
  const list = document.getElementById("plist");
  if (!projects.length) {
    list.innerHTML = `<div class="card" style="grid-column:1/-1">
      <div class="empty">
        <div class="big">No projects yet</div>
        <div>Add the app you want to demo — it becomes a profile you can run against.</div>
      </div></div>`;
  } else {
    list.innerHTML = projects.map(projectCard).join("");
    projects.forEach((p) => { if (!p.error) loadHealth(p.name); });
    bindProjectCards();
  }
  loadLatestJobs();
}

function projectCard(p) {
  if (p.error) {
    return `<div class="projcard bad">
      <div class="top"><div><div class="pname">${esc(p.name)}</div>
        <div class="mono-sub">${esc(p.error)}</div></div>
        <span class="pill bad"><span class="dot"></span>Unreadable</span></div>
      <div class="foot"><span class="note bad">Fix the YAML to use this project</span>
        <a class="btn sm" href="#/p/${enc(p.name)}/profile">Edit profile</a></div></div>`;
  }
  const route = [esc(p.base_url), p.route_prefix ? esc(p.route_prefix) : null, esc(p.locale)]
    .filter(Boolean).join(" · ");
  return `<div class="projcard" data-proj="${esc(p.name)}">
    <div class="top">
      <div><div class="pname">${esc(p.name)}</div><div class="mono-sub">${route}</div></div>
      <span class="pill" data-health-pill><span class="dot"></span>Checking…</span>
    </div>
    <div class="grid3" data-health-tiles>
      <div class="tile"><div class="k">App</div><div class="v off">…</div></div>
      <div class="tile"><div class="k">Session</div><div class="v off">…</div></div>
      <div class="tile"><div class="k">Tracker</div><div class="v ${p.tracker ? "" : "off"}">${
        p.tracker ? esc(p.tracker_label || "Configured") : "Not configured"}</div></div>
    </div>
    <div class="foot">
      <span class="note" data-health-note>${p.flows} flow${p.flows === 1 ? "" : "s"}${
        p.storage_state ? ` · storage_state ${esc(p.storage_state)}` : ""}</span>
      <div class="row tight" data-actions>
        <a class="btn sm" href="#/p/${enc(p.name)}/profile">Profile</a>
        <button class="sm" data-refresh>Refresh session</button>
        <button class="sm primary" data-start>Start a run</button>
      </div>
    </div>
  </div>`;
}

function bindProjectCards() {
  view.querySelectorAll("[data-proj]").forEach((card) => {
    const name = card.dataset.proj;
    const start = card.querySelector("[data-start]");
    if (start) start.onclick = () => (location.hash = `#/p/${enc(name)}/run/1`);
    const refresh = card.querySelector("[data-refresh]");
    if (refresh) refresh.onclick = (e) => busyClick(e.currentTarget, async () => {
      await act(post(`/api/projects/${enc(name)}/session/refresh`, {}));
      toast("Session refresh queued — follow it in Jobs.", "ok");
      loadLatestJobs();
    }, { busyLabel: "Refreshing…" });
  });
}

async function loadHealth(project) {
  const card = view.querySelector(`[data-proj="${CSS.escape(project)}"]`);
  if (!card) return;
  const pill = card.querySelector("[data-health-pill]");
  const tiles = card.querySelector("[data-health-tiles]").children;
  let h;
  try {
    h = await api(`/api/projects/${enc(project)}/health`);
  } catch {
    pill.className = "pill bad";
    pill.innerHTML = `<span class="dot"></span>Health check failed`;
    return;
  }
  const stale = h.session && h.session_age_hours > 48;
  const blocked = !h.app_reachable || !h.session;

  tiles[0].className = `tile${h.app_reachable ? "" : " bad"}`;
  tiles[0].querySelector(".v").className = `v ${h.app_reachable ? "ok" : "bad"}`;
  tiles[0].querySelector(".v").textContent = h.app_reachable
    ? `Reachable · ${h.app_status}` : "Unreachable";
  const sv = tiles[1].querySelector(".v");
  sv.className = `v ${h.session ? (stale ? "warn" : "") : "bad"}`;
  sv.textContent = h.session ? `${h.session_age_hours}h old` : "None";

  pill.className = `pill ${blocked ? "bad" : stale || h.build_error ? "warn" : "ok"}`;
  pill.innerHTML = `<span class="dot"></span>${
    blocked ? "Blocked" : h.build_error ? "Build error" : stale ? "Session stale" : "Ready to run"}`;
  card.classList.toggle("bad", blocked);

  if (blocked) {
    const note = card.querySelector("[data-health-note]");
    note.className = "note bad";
    note.textContent = !h.app_reachable
      ? "Start the dev server, then refresh" : "No session — refresh or sign in";
    const actions = card.querySelector("[data-actions]");
    actions.innerHTML = `<a class="btn sm" href="#/p/${enc(project)}/profile">Profile</a>
      <a class="btn sm" href="#/p/${enc(project)}/setup">Setup assistant</a>
      <button class="sm dark" data-retry>Retry health</button>`;
    actions.querySelector("[data-retry]").onclick = (e) =>
      busyClick(e.currentTarget, () => loadHealth(project), { busyLabel: "Checking…" });
  }
}

function toggleNewProject() {
  const box = document.getElementById("newForm");
  if (box.innerHTML) { box.innerHTML = ""; return; }
  box.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="row" style="align-items:flex-start;gap:14px">
        <label class="field grow"><span>Project name</span>
          <input type="text" id="npName" placeholder="my-app" autocomplete="off" style="width:100%"></label>
        <label class="field grow"><span>App URL</span>
          <input type="text" id="npUrl" placeholder="http://localhost:4200" autocomplete="off" style="width:100%"></label>
      </div>
      <label class="field" style="margin-top:12px"><span>What is this app? (optional — the setup assistant refines it later)</span>
        <textarea id="npContext" rows="3" placeholder="e.g. A carbon-assessment tool for construction. Users create projects, add building elements, and view LCA analytics."></textarea></label>
      <div class="row" style="margin-top:12px">
        <button class="primary" id="npCreate">Create project</button>
        <button id="npCancel">Cancel</button>
      </div>
    </div>`;
  document.getElementById("npName").focus();
  document.getElementById("npCancel").onclick = toggleNewProject;
  document.getElementById("npCreate").onclick = (e) => busyClick(e.currentTarget, async () => {
    const name = document.getElementById("npName").value.trim().toLowerCase();
    const base_url = document.getElementById("npUrl").value.trim();
    const context = document.getElementById("npContext").value.trim() || null;
    if (!name || !base_url) { toast("Name and URL are both required."); return; }
    const res = await act(post("/api/projects", { name, base_url, context }));
    toast(`Created "${res.name}".`, "ok");
    location.hash = `#/p/${enc(res.name)}/setup`;
  }, { busyLabel: "Creating…" });
}

async function loadLatestJobs() {
  const box = document.getElementById("latestJobs");
  if (!box) return;
  const jobs = await api("/api/jobs?limit=5");
  box.innerHTML = jobs.length
    ? `<table><tbody>${jobs.map((j) => `
        <tr><td style="width:100px">${statusPill(j.status)}</td>
          <td style="width:120px"><strong>${esc(j.kind)}</strong></td>
          <td class="muted">${esc(describeJob(j))}</td>
          <td class="num" style="width:80px">${duration(j)}</td>
          <td style="width:60px"><a class="link" href="#/jobs">log</a></td></tr>`).join("")}</tbody></table>`
    : `<div class="muted" style="padding:14px 18px">Nothing has run yet.</div>`;
}

function statusPill(status) {
  const map = { queued: "warn", running: "run", succeeded: "ok", failed: "bad", cancelled: "" };
  const dot = status === "running" || status === "succeeded" || status === "failed" ? `<span class="dot"></span>` : "";
  return `<span class="pill ${map[status] || ""}">${dot}${esc(status)}</span>`;
}

function describeJob(j) {
  const p = j.params || {};
  if (j.kind === "generate") {
    // The API fans one job out per work item, so params carry a single task_id.
    const ids = p.task_id ?? (p.tasks || []).map((t) => t.id).join(", ");
    return `${p.project || ""}${ids ? ` · task ${ids}` : ""}${p.max_steps ? ` · max_steps ${p.max_steps}` : ""}${p.polish ? " · polish" : ""}`;
  }
  if (j.kind === "render") {
    return `${p.project || ""} · ${(p.flows || []).length} flow${(p.flows || []).length === 1 ? "" : "s"}${p.sprint ? ` · sprint ${p.sprint}` : ""}`;
  }
  if (j.kind === "session_interactive") return `${p.project || ""} · manual login on the server machine`;
  if (j.kind === "test_beat") return `${p.project || ""}${p.flow ? ` · ${p.flow}` : ""} · beat ${p.beat_index ?? 0}`;
  return p.project || "";
}

/* =======================================================================
   The run wizard (2b–2e)
   ======================================================================= */

const STEPS = ["Work items", "Flows & beats", "Narration", "Render"];

function wizardChrome(project, step) {
  chrome.innerHTML = `
    <div class="runbar">
      <a class="exit" href="#/">← Exit run</a>
      <span class="proj">${esc(project)}</span>
      <span class="meta" id="runMeta"></span>
    </div>
    <div class="stepper">
      ${STEPS.map((label, i) => {
        const n = i + 1;
        const cls = n === step ? "on" : n < step ? "done" : "";
        const mark = n < step ? "✓" : n;
        return `${i ? `<div class="bar"></div>` : ""}
          <div class="step ${cls}" data-step="${n}">
            <span class="num">${mark}</span><span class="label">${label}</span>
          </div>`;
      }).join("")}
    </div>`;
  chrome.querySelectorAll(".step").forEach((el) => {
    const n = Number(el.dataset.step);
    if (n < step) el.onclick = () => (location.hash = `#/p/${enc(project)}/run/${n}`);
  });
  api(`/api/projects/${enc(project)}/health`).then((h) => {
    const m = document.getElementById("runMeta");
    if (m) m.textContent = h.session ? `session ${h.session_age_hours}h` : "no session";
  }).catch(() => {});
}

function renderWizard(project, step) {
  loadRun(project);
  const n = Math.min(4, Math.max(1, step || 1));
  wizardChrome(project, n);
  if (n === 1) return stepWorkItems(project);
  if (n === 2) return stepFlows(project);
  if (n === 3) return stepNarration(project);
  return stepRender(project);
}

const goStep = (project, n) => (location.hash = `#/p/${enc(project)}/run/${n}`);

/* ---------- 2b · step 1: work items ---------- */

let taskItems = [];
let taskQuery = "", taskState = "", taskPage = 0, taskLoadToken = 0;
const PAGE_SIZE = 12;

async function stepWorkItems(project) {
  view.innerHTML = `
    <div class="pagehead">
      <div>
        <h1>What shipped this sprint?</h1>
        <p>Each selected item becomes one <span class="mono">generate</span> job → one flow with a beat per task.</p>
      </div>
      <div class="actions" id="sprintPick"><span class="muted">Loading sprints…</span></div>
    </div>
    <div class="card pad0">
      <div class="cardhead" style="gap:12px">
        <div class="row grow" id="taskFilters"></div>
        <div class="row tight">
          <label class="field" style="display:flex;align-items:center;gap:7px">
            <span style="margin:0">max_steps</span>
            <input type="number" id="maxSteps" min="4" max="40" value="${run.maxSteps}" style="width:64px">
          </label>
          <label class="switch">polish <input type="checkbox" id="polish" ${run.polish ? "checked" : ""}></label>
        </div>
      </div>
      <div id="tasks"><div class="muted" style="padding:16px 18px">Pick a sprint to list its work items.</div></div>
    </div>
    <div id="selected"></div>
    <div class="wizfoot">
      <span class="hint" id="genHint">Select the items you want to demo.</span>
      <div class="row tight">
        <button class="primary" id="genBtn" disabled>Generate flows</button>
        <button id="skipGen">Skip — I already have flows</button>
      </div>
    </div>`;

  document.getElementById("maxSteps").onchange = (e) => { run.maxSteps = Number(e.target.value); saveRun(); };
  document.getElementById("polish").onchange = (e) => { run.polish = e.target.checked; saveRun(); };
  document.getElementById("skipGen").onclick = () => goStep(project, 2);
  document.getElementById("genBtn").onclick = (e) => busyClick(e.currentTarget, async () => {
    const n = run.tasks.length;
    for (const t of run.tasks) await saveHint(project, t);
    await act(post(`/api/projects/${enc(project)}/generate`, {
      tasks: run.tasks.map((t) => ({ id: t.id, guidance: (t.guidance || "").trim() || null })),
      max_steps: run.maxSteps,
      polish: run.polish,
    }));
    toast(`Generating ${n} flow${n > 1 ? "s" : ""} — follow it on the next step.`, "ok");
    goStep(project, 2);
  }, { busyLabel: "Starting…" });

  try {
    const sprints = await api(`/api/projects/${enc(project)}/sprints`);
    const box = document.getElementById("sprintPick");
    box.innerHTML = `<select id="sprint">${sprints.map((s) =>
      `<option value="${esc(s.name)}" ${s.name === run.sprint || (!run.sprint && s.timeframe === "current") ? "selected" : ""}>${
        esc(s.name)}${s.timeframe === "current" ? " · current" : ""}</option>`).join("")}</select>`;
    const sel = document.getElementById("sprint");
    sel.onchange = () => { run.sprint = sel.value; rememberSprintNumber(); saveRun(); loadTasks(project, sel.value); };
    run.sprint = sel.value;
    rememberSprintNumber();
    saveRun();
    if (sel.value) loadTasks(project, sel.value);
  } catch (e) {
    document.getElementById("sprintPick").innerHTML =
      `<span class="pill bad">tracker unavailable</span>`;
    document.getElementById("tasks").innerHTML =
      `<div class="empty"><div class="big">No issue tracker for this project</div>
        <div>Add an <span class="mono">issue_tracker</span> block to the profile, or skip ahead and render flows you already have.</div></div>`;
    toast(e.message);
  }
}

function rememberSprintNumber() {
  const m = String(run.sprint || "").match(/\d+/);
  run.sprintNum = m ? Number(m[0]) : null;
}

async function loadTasks(project, sprint) {
  const box = document.getElementById("tasks");
  box.innerHTML = `<div class="muted" style="padding:16px 18px">Loading work items…</div>`;
  const token = ++taskLoadToken;
  let items;
  try {
    items = await api(`/api/projects/${enc(project)}/sprints/${enc(sprint)}/work-items`);
  } catch (e) {
    box.innerHTML = `<div class="empty"><div class="big">Could not list work items</div><div>${esc(e.message).slice(0, 200)}</div></div>`;
    return;
  }
  if (token !== taskLoadToken) return;
  taskItems = items;
  taskQuery = ""; taskState = ""; taskPage = 0;
  renderTasks(project);
}

function taskFiltered() {
  const q = taskQuery.toLowerCase();
  return taskItems.filter((i) =>
    (!taskState || i.state === taskState) &&
    (!q || String(i.id).includes(q) || i.title.toLowerCase().includes(q) || i.type.toLowerCase().includes(q)));
}

function renderTasks(project) {
  const box = document.getElementById("tasks");
  if (!box) return;
  const filtered = taskFiltered();
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  taskPage = Math.min(taskPage, pages - 1);
  const pageItems = filtered.slice(taskPage * PAGE_SIZE, (taskPage + 1) * PAGE_SIZE);
  const counts = {};
  taskItems.forEach((i) => { counts[i.state] = (counts[i.state] || 0) + 1; });
  const states = Object.keys(counts).sort();
  const picked = new Set(run.tasks.map((t) => t.id));

  document.getElementById("taskFilters").innerHTML = `
    <input type="text" id="taskSearch" placeholder="Filter by id, title or type…" value="${esc(taskQuery)}" style="width:250px">
    <div class="segmented" id="stateSeg">
      ${states.map((s) => `<button data-state="${esc(s)}" class="${s === taskState ? "on" : ""}">${esc(s)} · ${counts[s]}</button>`).join("")}
      <button data-state="" class="${taskState ? "" : "on"}">All · ${taskItems.length}</button>
    </div>`;

  box.innerHTML = `
    <div class="scroll">
      <table>
        <thead><tr>
          <th style="width:38px"></th><th style="width:74px">id</th>
          <th>title</th><th style="width:150px">type</th>
          <th style="width:92px">state</th><th style="width:104px">guidance</th>
        </tr></thead>
        <tbody>${pageItems.map((i) => `
          <tr class="selectable">
            <td><input type="checkbox" data-task="${i.id}" ${picked.has(i.id) ? "checked" : ""}></td>
            <td class="num">${i.id}</td>
            <td class="wrap">${esc(i.title)}</td>
            <td class="num">${esc(i.type)}</td>
            <td>${stateBadge(i.state)}</td>
            <td>${hintCell(run.tasks.find((t) => t.id === i.id))}</td>
          </tr>`).join("") || `<tr><td colspan="6" class="muted" style="padding:18px">No matching work items.</td></tr>`}
        </tbody>
      </table>
    </div>
    <div class="row" style="padding:11px 18px;border-top:1px solid var(--line-soft)">
      <span class="muted grow">${filtered.length} shown · ${taskItems.length} in sprint</span>
      <button class="xs" id="taskPrev" ${taskPage === 0 ? "disabled" : ""}>← prev</button>
      <span class="muted2">page ${taskPage + 1} / ${pages}</span>
      <button class="xs" id="taskNext" ${taskPage >= pages - 1 ? "disabled" : ""}>next →</button>
    </div>`;

  const search = document.getElementById("taskSearch");
  search.oninput = () => { taskQuery = search.value; taskPage = 0; renderTasks(project); document.getElementById("taskSearch").focus(); };
  search.setSelectionRange(search.value.length, search.value.length);
  document.getElementById("stateSeg").querySelectorAll("button").forEach((b) => {
    b.onclick = () => { taskState = b.dataset.state; taskPage = 0; renderTasks(project); };
  });
  document.getElementById("taskPrev").onclick = () => { taskPage--; renderTasks(project); };
  document.getElementById("taskNext").onclick = () => { taskPage++; renderTasks(project); };
  box.querySelectorAll("[data-task]").forEach((cb) => {
    cb.onchange = () => {
      const id = Number(cb.dataset.task);
      if (cb.checked) {
        const item = taskItems.find((i) => i.id === id);
        if (!run.tasks.some((t) => t.id === id)) {
          run.tasks.push({ id, title: item ? item.title : "", guidance: "", saved: false });
          loadHint(project, id);
        }
      } else run.tasks = run.tasks.filter((t) => t.id !== id);
      saveRun();
      renderTasks(project);
    };
  });
  renderSelectedTasks(project);
}

function stateBadge(s) {
  const cls = { Done: "ok", Closed: "ok", Resolved: "ok", Removed: "bad" }[s] || "warn";
  return `<span class="pill ${cls}">${esc(s)}</span>`;
}

function renderSelectedTasks(project) {
  const slot = document.getElementById("selected");
  if (!slot) return;
  const btn = document.getElementById("genBtn");
  const hint = document.getElementById("genHint");
  btn.disabled = !run.tasks.length;
  btn.textContent = run.tasks.length ? `Generate ${run.tasks.length} flow${run.tasks.length > 1 ? "s" : ""}` : "Generate flows";
  hint.textContent = run.tasks.length
    ? `${run.tasks.length} item${run.tasks.length > 1 ? "s" : ""} selected · guidance is sent with each task`
    : "Select the items you want to demo.";

  if (!run.tasks.length) { slot.innerHTML = ""; return; }
  slot.innerHTML = `
    <h2 class="section">Guidance · ${run.tasks.length} selected</h2>
    ${run.tasks.map((t) => `
      <div class="card" style="margin-bottom:10px;padding:14px 16px">
        <div class="row" style="margin-bottom:8px">
          <strong class="grow">${t.id} · ${esc(t.title)}</strong>
          <span class="mono-sub">hints/task-${t.id}.md</span>
          <button class="xs" data-unsel="${t.id}">✕</button>
        </div>
        <textarea data-guid="${t.id}" rows="2" placeholder="Where the feature lives, exact buttons to click, which record to use…">${esc(t.guidance)}</textarea>
      </div>`).join("")}`;
  slot.querySelectorAll("[data-unsel]").forEach((b) => {
    b.onclick = () => {
      run.tasks = run.tasks.filter((t) => t.id !== Number(b.dataset.unsel));
      saveRun(); renderTasks(project);
    };
  });
  slot.querySelectorAll("[data-guid]").forEach((ta) => {
    const task = () => run.tasks.find((x) => x.id === Number(ta.dataset.guid));
    ta.oninput = () => {
      const t = task();
      if (t) { t.guidance = ta.value; t.saved = false; saveRun(); }
    };
    ta.onblur = () => saveHint(project, task());
  });
}

function hintCell(task) {
  if (!task) return `<span class="muted2">—</span>`;
  if (task.saved) return `<span class="pill ok">hint saved</span>`;
  return (task.guidance || "").trim()
    ? `<span class="pill warn">unsaved</span>` : `<span class="muted2">+ add</span>`;
}

// Guidance lives in hints/task-<id>.md so it outlives the run and any later
// regeneration picks it up; the generate call still carries it for this run.
async function loadHint(project, id) {
  try {
    const h = await api(`/api/projects/${enc(project)}/hints/${id}`);
    const t = run.tasks.find((x) => x.id === id);
    if (t && h.text && !t.guidance) {
      t.guidance = h.text;
      t.saved = true;
      saveRun();
      renderTasks(project);
    }
  } catch { /* a project without hints yet is normal */ }
}

async function saveHint(project, t) {
  if (!t || t.saved) return;
  try {
    await post(`/api/projects/${enc(project)}/hints/${t.id}`, { text: t.guidance || "" }, "PUT");
    t.saved = true;
    saveRun();
    const cell = view.querySelector(`[data-task="${t.id}"]`)?.closest("tr")?.lastElementChild;
    if (cell) cell.innerHTML = hintCell(t);
  } catch (e) {
    toast(`Could not save guidance for task ${t.id}: ${e.message}`);
  }
}

/* ---------- 2c · step 2: flows & beats ---------- */

async function stepFlows(project) {
  view.innerHTML = `
    <div class="pagehead">
      <div><h1>Flows &amp; beats</h1>
        <p>Each flow validates against the <span class="mono">FlowSpec</span> contract. Test one beat before committing to a full render.</p></div>
      <div class="actions"><span class="pill" id="genStatus"></span></div>
    </div>
    <div id="flowList"><div class="muted">Loading flows…</div></div>
    <div class="wizfoot">
      <span class="hint" id="flowHint">Pick the flows this video should contain.</span>
      <div class="row tight">
        <button id="backTo1">← Work items</button>
        <button class="primary" id="toNarration" disabled>Narration →</button>
      </div>
    </div>`;
  document.getElementById("backTo1").onclick = () => goStep(project, 1);
  document.getElementById("toNarration").onclick = () => goStep(project, 3);

  await loadFlowList(project);
  pollGenerateJobs(project);
}

async function loadFlowList(project) {
  const box = document.getElementById("flowList");
  if (!box) return;
  const flows = await act(api(`/api/projects/${enc(project)}/flows`));
  if (!flows.length) {
    box.innerHTML = `<div class="card"><div class="empty">
      <div class="big">No flows yet</div>
      <div>Generate one from a work item, or drop a hand-written
        <span class="mono">.demoflow.yaml</span> into this project's flows folder.</div></div></div>`;
    return;
  }
  box.innerHTML = flows.map((f) => flowCard(project, f)).join("");
  bindFlowCards(project);
  updateFlowFoot();
}

function flowCard(project, f) {
  const picked = run.flows.includes(f.name);
  const steps = f.steps ?? 0;
  const beats = f.beats ?? ((f.tasks || []).length || 1);
  const valid = f.error ? false : true;
  const preview = (f.step_preview || []).slice(0, 8);
  return `<div class="flowcard ${picked ? "picked" : ""}" data-flow="${esc(f.name)}">
    <div class="fhead">
      <input type="checkbox" data-pick ${picked ? "checked" : ""} ${valid ? "" : "disabled"} style="margin-top:3px">
      <div class="grow">
        <div class="ftitle">${esc(f.title || f.name)}</div>
        <div class="mono-sub">${esc(f.file)}${f.version ? ` · v${f.version}` : ""}${
          f.viewport ? ` · ${esc(f.viewport)}` : ""}</div>
      </div>
      ${valid ? `<span class="pill ok"><span class="dot"></span>valid</span>`
              : `<span class="pill bad"><span class="dot"></span>invalid</span>`}
    </div>
    ${valid ? `<div class="fbody">
      <div class="beatbox">
        <div class="bhead">${beats} beat${beats === 1 ? "" : "s"} · ${steps} step${steps === 1 ? "" : "s"}${
          (f.tasks || []).length ? ` · task ${(f.tasks || []).join(", ")}` : ""}</div>
        <div class="stepchips">
          ${preview.map((s) => `<span class="chip"><span class="act">${esc(s.action)}</span> ${esc(s.summary || "")}</span>`).join("")}
          ${steps > preview.length ? `<span class="chip more">+${steps - preview.length} more</span>` : ""}
        </div>
      </div>
    </div>` : `<div class="fbody"><div class="errbox">${esc(f.error || "This flow does not validate.")}</div></div>`}
    <div class="ffoot">
      <span class="muted">${f.narrated ?? "—"} of ${steps} steps carry narration${
        f.words != null ? ` · ${f.words} words` : ""}</span>
      <div class="row tight">
        ${valid ? `<button class="sm" data-test>▶ Test first beat</button>` : ""}
        <a class="btn sm" href="#/p/${enc(project)}/flow/${enc(f.name)}">Open editor</a>
      </div>
    </div>
  </div>`;
}

function bindFlowCards(project) {
  view.querySelectorAll("[data-flow]").forEach((card) => {
    const name = card.dataset.flow;
    const cb = card.querySelector("[data-pick]");
    if (cb) cb.onchange = () => {
      if (cb.checked) { if (!run.flows.includes(name)) run.flows.push(name); }
      else run.flows = run.flows.filter((f) => f !== name);
      card.classList.toggle("picked", cb.checked);
      saveRun();
      updateFlowFoot();
    };
    const test = card.querySelector("[data-test]");
    if (test) test.onclick = (e) => busyClick(e.currentTarget, async () => {
      const detail = await act(api(`/api/projects/${enc(project)}/flows/${enc(name)}`));
      await act(post(`/api/projects/${enc(project)}/flows/${enc(name)}/test-beat`,
        { flow: detail.flow, beat_index: 0, captions: false }));
      toast("Test render queued — it appears in Jobs as a short clip.", "ok");
    }, { busyLabel: "Queuing…" });
  });
}

function updateFlowFoot() {
  const btn = document.getElementById("toNarration");
  const hint = document.getElementById("flowHint");
  if (!btn) return;
  btn.disabled = !run.flows.length;
  hint.textContent = run.flows.length
    ? `${run.flows.length} flow${run.flows.length > 1 ? "s" : ""} selected for this render`
    : "Pick the flows this video should contain.";
}

// While generate jobs are in flight the flow list is stale by definition.
function pollGenerateJobs(project) {
  clearInterval(pollTimer);
  const tick = async () => {
    let jobs;
    try { jobs = await api("/api/jobs?limit=10"); } catch { return; }
    const mine = jobs.filter((j) => j.kind === "generate" && (j.params || {}).project === project);
    const busy = mine.filter((j) => j.status === "queued" || j.status === "running");
    const pill = document.getElementById("genStatus");
    if (!pill) { clearInterval(pollTimer); return; }
    if (busy.length) {
      pill.className = "pill run";
      pill.innerHTML = `<span class="dot"></span>${busy.length} generate job${busy.length > 1 ? "s" : ""} running`;
      pill.dataset.wasBusy = "1";
    } else {
      pill.className = "pill ok";
      pill.innerHTML = `<span class="dot"></span>${mine.length ? "generate jobs done" : "no generate jobs"}`;
      if (pill.dataset.wasBusy === "1") { delete pill.dataset.wasBusy; loadFlowList(project); }
    }
  };
  tick();
  pollTimer = setInterval(tick, 2500);
}

/* ---------- 2d · step 3: narration ---------- */

const narrationCache = {};   // flow name -> loaded {flow, dirty}

async function stepNarration(project) {
  if (!run.flows.length) return goStep(project, 2);
  view.innerHTML = `
    <div class="pagehead">
      <div><h1>Narration, step by step</h1>
        <p>One <span class="mono">say</span> per step. Timing comes from <span class="mono">pacing</span>,
          <span class="mono">min_duration_sec</span> and <span class="mono">settle_ms</span> — audio-paced steps
          stretch the video to fit the voice.</p></div>
      <div class="actions"><span class="pill">style.md + glossary.json applied</span></div>
    </div>
    <div id="narration"><div class="muted">Loading flows…</div></div>
    <div class="wizfoot">
      <span class="hint" id="narrHint"></span>
      <div class="row tight">
        <button id="backTo2">← Flows</button>
        <button class="primary" id="toRender">Render setup →</button>
      </div>
    </div>`;
  document.getElementById("backTo2").onclick = () => goStep(project, 2);
  document.getElementById("toRender").onclick = (e) => busyClick(e.currentTarget, async () => {
    await saveDirtyNarration(project);
    goStep(project, 4);
  }, { busyLabel: "Saving…" });

  const box = document.getElementById("narration");
  box.innerHTML = "";
  for (const name of run.flows) {
    const detail = await act(api(`/api/projects/${enc(project)}/flows/${enc(name)}`));
    narrationCache[name] = { flow: detail.flow, dirty: false };
    box.insertAdjacentHTML("beforeend", narrationFlow(name, detail.flow));
  }
  bindNarration(project);
  updateNarrHint();
}

function narrationFlow(name, flow) {
  return (flow.beats || []).map((beat, bi) => `
    <div class="card pad0" style="margin-bottom:16px" data-nflow="${esc(name)}" data-nbeat="${bi}">
      <div class="cardhead">
        <div><strong>${esc(beat.title || flow.title || name)}</strong>
          <div class="mono-sub">beat ${bi}${beat.task ? ` · task ${beat.task}` : ""} · ${beat.steps.length} steps</div></div>
        <span class="muted">${beat.steps.filter((s) => s.say).length} narrated</span>
      </div>
      <div style="padding:14px 16px">
        ${beat.steps.map((s, si) => narrationRow(s, si)).join("")}
      </div>
    </div>`).join("");
}

function narrationRow(step, si) {
  const timing = [
    step.pacing === "audio" ? "audio-paced" : "fixed",
    step.min_duration_sec ? `min ${step.min_duration_sec}s` : null,
    step.settle_ms ? `settle ${step.settle_ms}ms` : null,
  ].filter(Boolean).join(" · ");
  const w = words(step.say);
  return `<div class="narrow ${step.say ? "" : "empty"}" data-step="${si}">
    <div class="nhead">
      <span class="chip"><span class="act">${esc(step.action)}</span> ${esc(stepSummary(step))}</span>
      <span class="muted2">${esc(timing)}</span>
    </div>
    <div class="nbody">
      ${step.say
        ? `<textarea data-say rows="2">${esc(step.say)}</textarea>
           <div class="nfoot">
             <button class="xs" data-preview>▶ Preview</button>
             <button class="xs" data-regen>Regenerate</button>
             <button class="xs" data-clear>Clear</button>
             <span class="wordcount ${w > 20 ? "over" : ""}" data-wc>${w} words${w > 20 ? " · over the 20-word style rule" : ""}</span>
           </div>`
        : `<div class="row"><span class="grow">No narration on this step.</span>
             <button class="xs" data-add>+ add say</button></div>`}
    </div>
  </div>`;
}

function stepSummary(s) {
  if (s.path) return s.path;
  const l = s.locator || {};
  for (const k of ["testid", "role", "label", "text", "placeholder", "css"]) {
    if (l[k]) return `${k}=${l[k]}${l.name ? ` "${l.name}"` : ""}${l.nth != null ? ` nth:${l.nth}` : ""}`;
  }
  return s.value || "";
}

function bindNarration(project) {
  view.querySelectorAll("[data-nflow]").forEach((card) => {
    const name = card.dataset.nflow;
    const bi = Number(card.dataset.nbeat);
    const entry = narrationCache[name];
    card.querySelectorAll("[data-step]").forEach((rowEl) => {
      const si = Number(rowEl.dataset.step);
      const step = entry.flow.beats[bi].steps[si];

      const ta = rowEl.querySelector("[data-say]");
      if (ta) ta.oninput = () => {
        step.say = ta.value;
        entry.dirty = true;
        const w = words(ta.value);
        const wc = rowEl.querySelector("[data-wc]");
        wc.textContent = `${w} words${w > 20 ? " · over the 20-word style rule" : ""}`;
        wc.className = `wordcount ${w > 20 ? "over" : ""}`;
        updateNarrHint();
      };

      const add = rowEl.querySelector("[data-add]");
      if (add) add.onclick = () => {
        step.say = "";
        entry.dirty = true;
        rowEl.outerHTML = narrationRow(step, si);
        bindNarration(project);
      };

      const clear = rowEl.querySelector("[data-clear]");
      if (clear) clear.onclick = () => {
        delete step.say;
        entry.dirty = true;
        rowEl.outerHTML = narrationRow(step, si);
        bindNarration(project);
        updateNarrHint();
      };

      const prev = rowEl.querySelector("[data-preview]");
      if (prev) prev.onclick = (e) => busyClick(e.currentTarget, async () => {
        const text = (step.say || "").trim();
        if (!text) { toast("Nothing to preview on this step."); return; }
        const res = await fetch(`/api/projects/${enc(project)}/say/preview`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text, provider: run.provider }),
        });
        if (!res.ok) { toast(`${res.status} ${await res.text()}`); return; }
        const url = URL.createObjectURL(await res.blob());
        const audio = new Audio(url);
        audio.onended = () => URL.revokeObjectURL(url);
        await audio.play();
      }, { busyLabel: "Synthesising…" });

      const regen = rowEl.querySelector("[data-regen]");
      if (regen) regen.onclick = (e) => busyClick(e.currentTarget, async () => {
        const res = await act(post(`/api/projects/${enc(project)}/flows/${enc(name)}/regenerate-say`, {
          flow: entry.flow, beat_index: bi, step_index: si,
          task_id: entry.flow.beats[bi].task || null,
        }));
        step.say = res.say;
        entry.dirty = true;
        rowEl.outerHTML = narrationRow(step, si);
        bindNarration(project);
        updateNarrHint();
      }, { busyLabel: "Rewriting…" });
    });
  });
}

function updateNarrHint() {
  const hint = document.getElementById("narrHint");
  if (!hint) return;
  let steps = 0, narrated = 0, total = 0, dirty = false;
  for (const name of run.flows) {
    const entry = narrationCache[name];
    if (!entry) continue;
    if (entry.dirty) dirty = true;
    for (const beat of entry.flow.beats || []) for (const s of beat.steps) {
      steps++;
      if (s.say) { narrated++; total += words(s.say); }
    }
  }
  hint.textContent = `${narrated} of ${steps} steps narrated · ${total} words${dirty ? " · unsaved changes" : ""}`;
}

async function saveDirtyNarration(project) {
  for (const name of run.flows) {
    const entry = narrationCache[name];
    if (!entry || !entry.dirty) continue;
    await act(post(`/api/projects/${enc(project)}/flows/${enc(name)}`,
      { flow: entry.flow, note: "narration edited in the run wizard" }, "PUT"));
    entry.dirty = false;
  }
}

/* ---------- 2e · step 4: render ---------- */

async function stepRender(project) {
  if (!run.flows.length) return goStep(project, 2);
  view.innerHTML = `
    <div class="pagehead">
      <div><h1>Ready to render</h1>
        <p>Bookends are generated title and outro cards; captions burn in and also write a
          <span class="mono">.srt</span> sidecar.</p></div>
      <div class="actions"><span class="pill mono">POST /api/renders</span></div>
    </div>
    <div class="grid2" style="align-items:start">
      <div>
        <div id="bookend"></div>
        <h2 class="section">Flows in this render</h2>
        <div class="orderlist" id="order"></div>
        <div class="muted">Order sets the section order in the finished video · drag to reorder</div>
      </div>
      <div>
        <div class="card">
          <strong style="font-size:13px">Render options</strong>
          <div style="margin-top:14px;display:flex;flex-direction:column;gap:13px">
            <label class="field"><span>sprint · label on the title card</span>
              <input type="number" id="optSprint" value="${run.sprintNum ?? ""}" placeholder="none" style="width:110px"></label>
            <div><div style="font:600 11px var(--sans);color:var(--muted);margin-bottom:5px">provider</div>
              <div class="segmented" id="providerSeg">
                <button data-p="kokoro">kokoro</button><button data-p="elevenlabs">elevenlabs</button>
              </div></div>
            <label class="switch"><input type="checkbox" id="optCaptions" ${run.captions ? "checked" : ""}> captions <span class="muted2">burn in + .srt sidecar</span></label>
            <label class="switch"><input type="checkbox" id="optBookends" ${run.bookends ? "checked" : ""}> bookends <span class="muted2">title + outro cards</span></label>
          </div>
        </div>
        <div class="estimate" style="margin-top:14px" id="estimate">
          <div class="muted">Estimating…</div>
        </div>
        <div class="card" style="margin-top:14px">
          <div style="font:600 11px var(--sans);color:var(--muted);margin-bottom:7px">Writes to</div>
          <div class="writes">output/runs/&lt;run-id&gt;/<br>&nbsp;&nbsp;demo.mp4<br>&nbsp;&nbsp;narration.srt<br>&nbsp;&nbsp;script.md</div>
        </div>
      </div>
    </div>
    <div class="wizfoot">
      <span class="hint">Queues one <span class="mono">render</span> job · you can close this tab</span>
      <div class="row tight">
        <button id="backTo3">← Narration</button>
        <button class="primary" id="renderBtn">Render video</button>
      </div>
    </div>`;

  document.getElementById("backTo3").onclick = () => goStep(project, 3);
  document.getElementById("optSprint").onchange = (e) => {
    run.sprintNum = e.target.value ? Number(e.target.value) : null; saveRun(); drawBookend();
  };
  document.getElementById("optCaptions").onchange = (e) => { run.captions = e.target.checked; saveRun(); };
  document.getElementById("optBookends").onchange = (e) => {
    run.bookends = e.target.checked; saveRun(); drawBookend(); loadEstimate(project);
  };
  const seg = document.getElementById("providerSeg");
  const activeProvider = () => run.provider || (window.__meta && window.__meta.tts_provider) || "kokoro";
  const paintSeg = () => seg.querySelectorAll("button").forEach((b) =>
    b.classList.toggle("on", b.dataset.p === activeProvider()));
  seg.querySelectorAll("button").forEach((b) => {
    b.onclick = () => { run.provider = b.dataset.p; saveRun(); paintSeg(); loadEstimate(project); };
  });
  paintSeg();

  document.getElementById("renderBtn").onclick = (e) => busyClick(e.currentTarget, async () => {
    await act(post("/api/renders", {
      project,
      flows: run.flows,
      sprint: run.sprintNum,
      captions: run.captions,
      bookends: run.bookends,
      provider: activeProvider(),
    }));
    toast("Render queued — follow it in Jobs.", "ok");
    location.hash = "#/jobs";
  }, { busyLabel: "Queuing…", keepDisabled: true });

  drawOrder(project);
  drawBookend();
  loadEstimate(project);
}

function drawOrder(project) {
  const box = document.getElementById("order");
  box.innerHTML = run.flows.map((name, i) => `
    <div class="orderrow" draggable="true" data-name="${esc(name)}">
      <span class="handle">⠿</span>
      <span class="ord">${i + 1}</span>
      <div class="grow"><strong data-title>${esc(name)}</strong>
        <div class="mono-sub">${esc(name)}</div></div>
      <button class="xs" data-drop>✕</button>
    </div>`).join("");

  // Titles come from the flow list; the row renders immediately with the file name,
  // then the bookend preview is redrawn once the real titles land.
  api(`/api/projects/${enc(project)}/flows`).then((flows) => {
    box.querySelectorAll("[data-name]").forEach((row) => {
      const f = flows.find((x) => x.name === row.dataset.name);
      if (f) row.querySelector("[data-title]").textContent = f.title || f.name;
    });
    drawBookend();
  }).catch(() => {});

  let dragged = null;
  box.querySelectorAll(".orderrow").forEach((row) => {
    row.addEventListener("dragstart", () => { dragged = row; row.classList.add("dragging"); });
    row.addEventListener("dragend", () => { row.classList.remove("dragging"); dragged = null; });
    row.addEventListener("dragover", (e) => { e.preventDefault(); row.classList.add("dragover"); });
    row.addEventListener("dragleave", () => row.classList.remove("dragover"));
    row.addEventListener("drop", (e) => {
      e.preventDefault();
      row.classList.remove("dragover");
      if (!dragged || dragged === row) return;
      const from = run.flows.indexOf(dragged.dataset.name);
      const to = run.flows.indexOf(row.dataset.name);
      run.flows.splice(to, 0, run.flows.splice(from, 1)[0]);
      saveRun();
      drawOrder(project);
      drawBookend();
    });
    row.querySelector("[data-drop]").onclick = () => {
      run.flows = run.flows.filter((f) => f !== row.dataset.name);
      saveRun();
      if (!run.flows.length) return goStep(project, 2);
      drawOrder(project); drawBookend(); loadEstimate(project);
    };
  });
}

function drawBookend() {
  const box = document.getElementById("bookend");
  if (!box) return;
  if (!run.bookends) {
    box.innerHTML = `<div class="card"><div class="muted">Bookends are off — the video starts on the first step.</div></div>`;
    return;
  }
  const titles = run.flows.map((n) => {
    const el = document.querySelector(`[data-name="${CSS.escape(n)}"] [data-title]`);
    return el ? el.textContent : n;
  });
  box.innerHTML = `
    <div class="bookend">
      <div class="kicker">${run.sprintNum ? `Sprint ${run.sprintNum} · ` : ""}Product demo</div>
      <div class="title">What's new</div>
      <ol>${titles.map((t) => `<li>${esc(t)}</li>`).join("")}</ol>
      <div class="spec">1280×720 · crf 20 · preset medium — bookend preview</div>
    </div>`;
}

async function loadEstimate(project) {
  const box = document.getElementById("estimate");
  if (!box) return;
  const qs = run.flows.map((f) => `flows=${enc(f)}`).join("&");
  try {
    const est = await api(`/api/projects/${enc(project)}/estimate?${qs}&bookends=${run.bookends}`);
    const paid = (run.provider || est.provider) === "elevenlabs";
    box.innerHTML = `
      <div style="font:600 11px var(--sans);color:var(--muted);margin-bottom:4px">Estimate</div>
      <div class="big">${clock(est.seconds)}</div>
      <div class="muted" style="margin-top:6px">${est.steps} steps · ${est.words} words · ${
        paid ? "elevenlabs bills per character" : "kokoro is local, so no per-run cost"}. ffmpeg assembly adds a few minutes.</div>`;
  } catch (e) {
    box.innerHTML = `<div class="muted">Estimate unavailable — ${esc(e.message).slice(0, 120)}</div>`;
  }
}

/* =======================================================================
   2f · Jobs
   ======================================================================= */

let jobFilter = "all";

async function renderJobs() {
  view.innerHTML = `
    <div class="pagehead">
      <div><h1>Jobs</h1><p>The queue is single-worker, so jobs run in order. Latest 30 shown.</p></div>
      <div class="actions"><div class="segmented" id="jobSeg">
        <button data-f="all" class="on">All</button>
        <button data-f="running">Running</button>
        <button data-f="failed">Failed</button>
      </div></div>
    </div>
    <div id="jobActive"></div>
    <div class="card pad0" style="margin-top:18px">
      <div class="cardhead"><strong>History</strong></div>
      <div id="jobTable"><div class="muted" style="padding:14px 18px">Loading…</div></div>
    </div>`;
  document.getElementById("jobSeg").querySelectorAll("button").forEach((b) => {
    b.onclick = () => {
      jobFilter = b.dataset.f;
      document.getElementById("jobSeg").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      refreshJobs();
    };
  });
  clearInterval(pollTimer);
  const tick = () => refreshJobs().catch(() => {});
  await tick();
  pollTimer = setInterval(tick, 2500);
}

const RENDER_STAGES = ["session", "capture", "segmentation", "tts", "assembly"];

async function refreshJobs() {
  const jobs = await api("/api/jobs?limit=30");
  const active = document.getElementById("jobActive");
  const table = document.getElementById("jobTable");
  if (!active || !table) { clearInterval(pollTimer); return; }

  const isActive = (j) => j.status === "running" || j.status === "queued";
  const shown = jobs.filter((j) =>
    jobFilter === "all" ? true : jobFilter === "running" ? isActive(j) : j.status === "failed");

  const featured = shown.filter((j) => isActive(j) || j.status === "failed").slice(0, 3);
  const seen = new Set(featured.map((j) => j.id));

  for (const j of featured) {
    let card = document.getElementById(`job-${j.id}`);
    if (!card) {
      card = document.createElement("div");
      card.id = `job-${j.id}`;
      active.prepend(card);
    }
    paintJobCard(card, j);
  }
  [...active.children].forEach((el) => {
    if (!seen.has(el.id.replace("job-", ""))) el.remove();
  });
  if (!featured.length) {
    active.innerHTML = `<div class="card"><div class="muted">Nothing running or failed right now.</div></div>`;
  }

  const rest = shown.filter((j) => !seen.has(j.id));
  table.innerHTML = rest.length ? `
    <table><thead><tr>
      <th style="width:104px">status</th><th style="width:130px">kind</th><th>params</th>
      <th style="width:80px">duration</th><th style="width:80px">finished</th><th style="width:120px"></th>
    </tr></thead><tbody>
      ${rest.map((j) => `<tr>
        <td>${statusPill(j.status)}</td>
        <td><strong>${esc(j.kind)}</strong></td>
        <td class="muted">${esc(describeJob(j))}</td>
        <td class="num">${duration(j)}</td>
        <td class="num">${hhmm(j.finished_at)}</td>
        <td class="row tight" style="border:none">
          ${j.status === "failed" || j.status === "cancelled" ? `<button class="xs" data-retry="${j.id}">retry</button>` : ""}
          ${j.status === "queued" ? `<button class="xs" data-cancel="${j.id}">cancel</button>` : ""}
          <button class="xs" data-log="${j.id}">log</button>
        </td></tr>`).join("")}
    </tbody></table>` : `<div class="muted" style="padding:14px 18px">No jobs match this filter.</div>`;

  table.querySelectorAll("[data-retry]").forEach((b) => {
    b.onclick = (e) => busyClick(e.currentTarget, async () => {
      await act(post(`/api/jobs/${b.dataset.retry}/retry`, {}));
      toast("Requeued.", "ok"); refreshJobs();
    }, { busyLabel: "…" });
  });
  table.querySelectorAll("[data-cancel]").forEach((b) => {
    b.onclick = (e) => busyClick(e.currentTarget, async () => {
      await act(post(`/api/jobs/${b.dataset.cancel}/cancel`, {}));
      refreshJobs();
    }, { busyLabel: "…" });
  });
  table.querySelectorAll("[data-log]").forEach((b) => {
    b.onclick = () => showLogSheet(b.dataset.log);
  });
}

function paintJobCard(card, j) {
  const running = j.status === "running" || j.status === "queued";
  card.className = `jobcard ${j.status === "failed" ? "failed" : running ? "running" : ""}`;
  if (!card.dataset.built) {
    card.dataset.built = "1";
    card.innerHTML = `
      <div class="jhead">
        <div class="grow">
          <div class="row tight" style="margin-bottom:4px">
            <span data-pill></span><strong style="font-size:13.5px">${esc(j.kind)}</strong>
          </div>
          <div class="muted" data-desc></div>
          <div class="mono-sub" data-ids></div>
        </div>
        <div class="row tight" data-acts></div>
      </div>
      <div class="jbody">
        <div class="stages" data-stages></div>
        <div data-err></div>
        <div class="log" data-log style="display:none"></div>
      </div>`;
  }
  card.querySelector("[data-pill]").innerHTML = statusPill(j.status);
  card.querySelector("[data-desc]").textContent = describeJob(j);
  card.querySelector("[data-ids]").textContent =
    `job ${j.id}${j.started_at ? ` · started ${hhmm(j.started_at)}` : ""}`;

  const acts = card.querySelector("[data-acts]");
  acts.innerHTML = "";
  if (j.status === "failed" || j.status === "cancelled") {
    const r = document.createElement("button");
    r.className = "sm"; r.textContent = "Retry job";
    r.onclick = (e) => busyClick(e.currentTarget, async () => {
      await act(post(`/api/jobs/${j.id}/retry`, {})); refreshJobs();
    }, { busyLabel: "…" });
    acts.appendChild(r);
  }
  if (j.status === "queued") {
    const c = document.createElement("button");
    c.className = "sm"; c.textContent = "Cancel";
    c.onclick = (e) => busyClick(e.currentTarget, async () => {
      await act(post(`/api/jobs/${j.id}/cancel`, {})); refreshJobs();
    }, { busyLabel: "…" });
    acts.appendChild(c);
  }
  const follow = document.createElement("button");
  follow.className = "sm";
  const logEl = card.querySelector("[data-log]");
  follow.textContent = logEl.style.display === "none" ? "Follow log" : "Hide log";
  follow.onclick = () => {
    logEl.style.display = logEl.style.display === "none" ? "" : "none";
    follow.textContent = logEl.style.display === "none" ? "Follow log" : "Hide log";
  };
  acts.appendChild(follow);

  const errBox = card.querySelector("[data-err]");
  errBox.innerHTML = j.error ? `<div class="errbox">${esc(j.error).slice(0, 400)}</div>` : "";

  const stagesEl = card.querySelector("[data-stages]");
  if (j.kind === "render" && running) {
    const text = logEl.textContent.toLowerCase();
    stagesEl.innerHTML = RENDER_STAGES.map((s) => {
      const hit = text.includes(s);
      const last = RENDER_STAGES.filter((x) => text.includes(x)).pop();
      return `<span class="stage ${s === last ? "now" : hit ? "done" : ""}">${s}</span>`;
    }).join("");
  } else stagesEl.innerHTML = "";

  if (logEl.style.display !== "none") tailLog(j, logEl);
}

async function tailLog(j, el) {
  const off = logOffsets[j.id] || 0;
  try {
    const chunk = await api(`/api/jobs/${j.id}/log?offset=${off}`);
    if (chunk) {
      logOffsets[j.id] = off + chunk.length;
      el.textContent += chunk;
      el.scrollTop = el.scrollHeight;
    }
  } catch { /* a log that isn't on disk yet is not an error worth shouting about */ }
}

async function showLogSheet(jobId) {
  const text = await act(api(`/api/jobs/${jobId}/log`));
  openSheet(`
    <div class="shead"><h3>Log · ${esc(jobId)}</h3></div>
    <div class="sbody"><div class="log" style="max-height:52vh">${esc(text || "(empty)")}</div></div>
    <div class="sfoot"><button data-close>Close</button></div>`, { wide: true });
}

/* ---------- compact job panel (used by the flow editor's #jobs slot) ---------- */

function startJobPolling(project) {
  clearInterval(pollTimer);
  const tick = async () => {
    const box = document.getElementById("jobs");
    if (!box) { clearInterval(pollTimer); return; }
    let jobs;
    try { jobs = await api("/api/jobs?limit=10"); } catch { return; }
    const mine = jobs.filter((j) => !project || (j.params || {}).project === project);
    box.innerHTML = mine.length
      ? `<div class="card pad0"><table><tbody>${mine.map((j) => `
          <tr><td style="width:104px">${statusPill(j.status)}</td>
            <td style="width:130px"><strong>${esc(j.kind)}</strong></td>
            <td class="muted">${esc(j.error ? j.error.slice(0, 120) : describeJob(j))}</td>
            <td class="num" style="width:80px">${duration(j)}</td></tr>`).join("")}
        </tbody></table></div>`
      : `<div class="muted">No jobs yet.</div>`;
  };
  tick();
  pollTimer = setInterval(tick, 3000);
}

/* =======================================================================
   3a · Library
   ======================================================================= */

async function renderLibrary() {
  view.innerHTML = `
    <div class="pagehead">
      <div><h1>Library</h1><p>Every finished render, and who can see it.</p></div>
    </div>
    <div class="card pad0" id="lib"><div class="muted" style="padding:16px 18px">Loading…</div></div>`;
  const renders = await act(api("/api/renders"));
  const box = document.getElementById("lib");
  if (!renders.length) {
    box.innerHTML = `<div class="empty"><div class="big">Nothing rendered yet</div>
      <div>Start a run from a project and the finished video lands here.</div></div>`;
    return;
  }
  box.innerHTML = `
    <table><thead><tr>
      <th>title</th><th style="width:120px">project</th><th style="width:80px">length</th>
      <th style="width:110px">created</th><th style="width:190px">share</th><th style="width:90px"></th>
    </tr></thead><tbody>
      ${renders.map((r) => `<tr class="selectable">
        <td class="wrap"><a class="link" href="#/r/${r.id}">${esc(r.title)}</a>
          ${r.video_exists ? "" : `<div class="muted2">video missing on disk</div>`}</td>
        <td class="num">${esc(r.project)}</td>
        <td class="num">${clock(r.duration_sec)}${
          r.chapter_count ? `<div class="muted2">${r.chapter_count} chapter${r.chapter_count === 1 ? "" : "s"}</div>` : ""}</td>
        <td class="num">${ago(r.created_at)}</td>
        <td>${shareCell(r.share)}</td>
        <td><a class="link" href="#/r/${r.id}">Open →</a></td>
      </tr>`).join("")}
    </tbody></table>`;
}

function shareCell(share) {
  if (!share) return `<span class="muted2">Not shared</span>`;
  const label = { active: "Shared", expired: "Expired", revoked: "Revoked" }[share.state] || share.state;
  const cls = { active: "ok", expired: "warn", revoked: "" }[share.state] || "";
  const bits = [];
  if (share.has_password) bits.push("password");
  if (!share.allow_download) bits.push("no download");
  return `<div class="sharecell">
    <span class="pill ${cls}">${cls === "ok" ? `<span class="dot"></span>` : ""}${label}</span>
    <span class="muted2">${share.views} view${share.views === 1 ? "" : "s"}${bits.length ? ` · ${bits.join(" · ")}` : ""}</span>
  </div>`;
}

/* =======================================================================
   3b · Render detail + share panel
   ======================================================================= */

async function renderRender(renderId) {
  const r = await act(api(`/api/renders/${renderId}`));
  view.innerHTML = `
    <div class="pagehead">
      <div>
        <h1>${esc(r.title)}</h1>
        <p><a class="link" href="#/library">← Library</a> · ${esc(r.project)} · ${clock(r.duration_sec)}${
          r.size_mb ? ` · ${r.size_mb} MB` : ""} · ${ago(r.created_at)}</p>
      </div>
      <div class="actions"><button class="danger" id="delRender">Remove from library</button></div>
    </div>
    <div class="grid2" style="align-items:start">
      <div>
        ${r.video_exists
          ? `<video controls src="/api/renders/${renderId}/video"></video>`
          : `<div class="card bad"><div class="errbox">The video file is missing on disk. It was probably cleaned out of its run directory.</div></div>`}
        <div id="chapters"></div>
        <h2 class="section">Files</h2>
        <div class="card">
          <div class="row">
            <a class="btn sm ${r.video_exists ? "" : "disabled"}" href="/api/renders/${renderId}/video" download>Download mp4</a>
            <a class="btn sm ${r.has_srt ? "" : "disabled"}" href="/api/renders/${renderId}/srt" download>Subtitles (.srt)</a>
            <a class="btn sm ${r.has_script ? "" : "disabled"}" href="/api/renders/${renderId}/script" download>Script (.md)</a>
          </div>
        </div>
      </div>
      <div id="sharePanel"></div>
    </div>`;

  document.getElementById("delRender").onclick = async () => {
    if (!confirm("Remove this video from the library? Its share links stop working immediately. Files stay on disk.")) return;
    await act(api(`/api/renders/${renderId}`, { method: "DELETE" }));
    location.hash = "#/library";
  };
  drawSharePanel(renderId, r);
  drawChapters(r.chapters || []);
}

// The same list the share page shows, so you can check it before sending a link.
function drawChapters(chapters) {
  const box = document.getElementById("chapters");
  if (!box) return;
  if (!chapters.length) {
    box.innerHTML = `<h2 class="section">Chapters</h2>
      <div class="card"><div class="muted">This render predates chapter recording —
        re-render it to get a chapter list on the share page.</div></div>`;
    return;
  }
  box.innerHTML = `<h2 class="section">In this demo</h2>
    <div class="card"><div class="chapterlist">
      ${chapters.map((c) => `
        <button class="chapterrow" data-seek="${c.start_sec}">
          <span class="at">${mark(c.start_sec)}</span>
          <span><span class="ct">${esc(c.title)}${
            c.task ? ` <span class="muted2">· task ${c.task}</span>` : ""}</span>
            ${c.summary ? `<span class="cs">${esc(c.summary)}</span>` : ""}</span>
        </button>`).join("")}
    </div></div>`;
  const video = view.querySelector("video");
  box.querySelectorAll("[data-seek]").forEach((b) => {
    b.onclick = () => {
      if (!video) return;
      video.currentTime = Number(b.dataset.seek);
      video.play().catch(() => {});
    };
  });
  if (video) {
    video.addEventListener("timeupdate", () => {
      const t = video.currentTime;
      let active = -1;
      chapters.forEach((c, i) => {
        if (t >= c.start_sec && (c.end_sec == null || t < c.end_sec)) active = i;
      });
      box.querySelectorAll("[data-seek]").forEach((b, i) => b.classList.toggle("on", i === active));
    });
  }
}

function drawSharePanel(renderId, r) {
  const box = document.getElementById("sharePanel");
  const live = (r.shares || []).filter((s) => s.state === "active");
  const dead = (r.shares || []).filter((s) => s.state !== "active");

  box.innerHTML = `
    <div class="sharepanel">
      <div class="row" style="margin-bottom:12px">
        <strong class="grow" style="font-size:13px">Share</strong>
        <button class="sm primary" id="newShare" ${r.video_exists ? "" : "disabled"}>Create link</button>
      </div>
      ${live.length ? live.map(shareBlock).join("") : `
        <div class="muted">Not shared yet. A link lets anyone watch without signing in —
          you can set an expiry, a password, and whether they may download.</div>`}
    </div>
    ${dead.length ? `<div class="card" style="margin-top:14px">
      <div style="font:600 11px var(--sans);color:var(--muted);margin-bottom:9px">Past links</div>
      ${dead.map((s) => `<div class="row" style="padding:6px 0">
        <span class="pill ${s.state === "expired" ? "warn" : ""}">${s.state}</span>
        <span class="muted grow">${s.views} view${s.views === 1 ? "" : "s"} · created ${ago(s.created_at)}</span>
      </div>`).join("")}
    </div>` : ""}`;

  document.getElementById("newShare").onclick = () => openShareSheet(renderId);
  box.querySelectorAll("[data-copy]").forEach((b) => {
    b.onclick = async () => {
      await navigator.clipboard.writeText(b.dataset.copy);
      const old = b.textContent;
      b.textContent = "Copied ✓";
      setTimeout(() => (b.textContent = old), 1500);
    };
  });
  box.querySelectorAll("[data-revoke]").forEach((b) => {
    b.onclick = async () => {
      if (!confirm("Revoke this link? Anyone holding it loses access immediately.")) return;
      await act(api(`/api/shares/${b.dataset.revoke}`, { method: "DELETE" }));
      toast("Link revoked.", "ok");
      renderRender(renderId);
    };
  });
}

function shareBlock(s) {
  const url = `${location.origin}/s/${s.token}`;
  return `
    <div class="linkrow"><span class="url">${esc(url)}</span>
      <button class="xs" data-copy="${esc(url)}">Copy</button></div>
    <div class="sharemeta">
      <div class="m"><div class="k">Views</div><div class="v">${s.views}</div></div>
      <div class="m"><div class="k">Viewers</div><div class="v">${s.viewers}</div></div>
      <div class="m"><div class="k">Expires</div><div class="v">${until(s.expires_at)}</div></div>
      <div class="m"><div class="k">Password</div><div class="v">${s.has_password ? "Yes" : "No"}</div></div>
      <div class="m"><div class="k">Download</div><div class="v">${s.allow_download ? "Allowed" : "Blocked"}</div></div>
    </div>
    <div class="row" style="margin-top:12px">
      <span class="muted2 grow">${s.last_viewed_at ? `Last opened ${ago(s.last_viewed_at)}` : "Not opened yet"}</span>
      <button class="xs" data-revoke="${s.token}">Revoke</button>
    </div>`;
}

/* ---------- 3c · create-link sheet ---------- */

function openShareSheet(renderId) {
  openSheet(`
    <div class="shead">
      <h3>Create a share link</h3>
      <p class="muted">Anyone with the link can watch — there is no sign-in. Treat it like a password.</p>
    </div>
    <div class="sbody">
      <label class="field"><span>Expires</span>
        <select id="shExpiry" style="width:100%">
          <option value="7">In 7 days</option>
          <option value="30">In 30 days</option>
          <option value="90">In 90 days</option>
          <option value="">Never</option>
        </select></label>
      <label class="field"><span>Password (optional)</span>
        <input type="password" id="shPassword" placeholder="Leave empty for no password" style="width:100%"></label>
      <label class="switch"><input type="checkbox" id="shDownload" checked> Allow downloading the mp4</label>
    </div>
    <div class="sfoot">
      <button data-close>Cancel</button>
      <button class="primary" id="shCreate">Create link</button>
    </div>`);

  document.getElementById("shCreate").onclick = (e) => busyClick(e.currentTarget, async () => {
    const days = document.getElementById("shExpiry").value;
    const res = await act(post(`/api/renders/${renderId}/share`, {
      expires_in_days: days ? Number(days) : null,
      password: document.getElementById("shPassword").value || null,
      allow_download: document.getElementById("shDownload").checked,
    }));
    closeSheet();
    await navigator.clipboard.writeText(res.url).catch(() => {});
    toast("Link created and copied to your clipboard.", "ok");
    renderRender(renderId);
  }, { busyLabel: "Creating…" });
}

/* ---------- sheet plumbing ---------- */

function openSheet(html, { wide = false } = {}) {
  closeSheet();
  const back = document.createElement("div");
  back.className = "backdrop";
  back.id = "sheet";
  back.innerHTML = `<div class="sheet" ${wide ? 'style="max-width:720px"' : ""}>${html}</div>`;
  back.onclick = (e) => { if (e.target === back) closeSheet(); };
  document.body.appendChild(back);
  back.querySelectorAll("[data-close]").forEach((b) => (b.onclick = closeSheet));
  document.addEventListener("keydown", escClose);
}
function closeSheet() {
  const el = document.getElementById("sheet");
  if (el) el.remove();
  document.removeEventListener("keydown", escClose);
}
function escClose(e) { if (e.key === "Escape") closeSheet(); }

/* =======================================================================
   2h · Setup assistant
   ======================================================================= */

async function renderSetupAssistant(project) {
  clearInterval(pollTimer);
  view.innerHTML = `
    <div class="pagehead">
      <div><h1>Setup assistant</h1>
        <p>${esc(project)} · a short conversation that builds this app's context and routes.
          I look at your app to propose real routes and detect its login page — I never touch credentials.</p></div>
      <div class="actions">
        <a class="btn" href="#/p/${enc(project)}/profile">Edit profile YAML</a>
        <button id="onbReset">Reset chat</button>
      </div>
    </div>
    <div class="setupgrid">
      <div>
        <div class="card"><div id="chat" class="chat"><div class="muted">Loading…</div></div></div>
        <div class="row" style="margin-top:10px;align-items:flex-end">
          <textarea id="onbInput" rows="2" placeholder="Describe your app, or answer a question…" class="grow"></textarea>
          <button class="primary" id="onbSend">Send</button>
        </div>
        <div class="muted2" style="margin-top:6px">⌘/Ctrl + Enter sends</div>
      </div>
      <div class="card">
        <div class="row"><strong class="grow" style="font-size:13px">Draft profile</strong>
          <span class="pill" id="onbReady">draft</span></div>
        <div style="margin-top:14px;display:flex;flex-direction:column;gap:12px">
          <div><div style="font:600 11px var(--sans);color:var(--muted);margin-bottom:5px">Project context</div>
            <div class="draftbox" id="onbContext">—</div></div>
          <div><div style="font:600 11px var(--sans);color:var(--muted);margin-bottom:5px">Sign-in path</div>
            <div class="draftbox" id="onbSignIn">—</div></div>
          <div><div style="font:600 11px var(--sans);color:var(--muted);margin-bottom:5px">Routes (feature_map)</div>
            <div class="draftbox" id="onbRoutes">—</div></div>
        </div>
        <button class="primary" id="onbApply" style="width:100%;justify-content:center;margin-top:14px">Apply to profile</button>
        <div class="muted2" style="margin-top:7px">Applying merges the draft into the profile. Want a change? Just ask in the chat.</div>
      </div>
    </div>`;

  document.getElementById("onbReset").onclick = (e) => busyClick(e.currentTarget, async () => {
    await act(post(`/api/projects/${enc(project)}/onboard/reset`, {}));
    await loadOnboard(project);
  }, { busyLabel: "Resetting…" });
  document.getElementById("onbSend").onclick = (e) => sendOnboard(project, e.currentTarget);
  document.getElementById("onbInput").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) {
      ev.preventDefault();
      document.getElementById("onbSend").click();
    }
  });
  document.getElementById("onbApply").onclick = (e) => busyClick(e.currentTarget, async () => {
    const res = await act(post(`/api/projects/${enc(project)}/onboard/apply`, {}));
    toast(`Applied to ${res.saved}. You can now generate demos with this context.`, "ok");
  }, { busyLabel: "Applying…" });

  await loadOnboard(project);
}

async function loadOnboard(project) {
  const state = await act(api(`/api/projects/${enc(project)}/onboard`));
  renderChat(project, state.messages || []);
  renderDraft(state.draft || {});
}

function renderChat(project, messages) {
  const box = document.getElementById("chat");
  if (!box) return;
  if (!messages.length) {
    box.innerHTML = `<div class="msg system">No conversation yet.</div>
      <div style="align-self:center;margin-top:8px">
        <button class="primary" id="onbStart">Start — take a look at my app</button></div>`;
    document.getElementById("onbStart").onclick = (e) => sendOnboard(project, e.currentTarget, "");
    return;
  }
  box.innerHTML = messages.map((m) => {
    const role = ["user", "assistant", "system"].includes(m.role) ? m.role : "assistant";
    const qs = (m.questions || []).length
      ? `<div class="q">${m.questions.map((q) => "• " + esc(q)).join("<br>")}</div>` : "";
    return `<div class="msg ${role}">${esc(m.text)}${qs}</div>`;
  }).join("");
  box.scrollTop = box.scrollHeight;
}

function renderDraft(draft) {
  const ctx = document.getElementById("onbContext");
  if (!ctx) return;
  ctx.textContent = (draft.context || "").trim() || "—";
  document.getElementById("onbSignIn").textContent = draft.sign_in_path || "—";
  const fm = draft.feature_map || {};
  const keys = Object.keys(fm);
  document.getElementById("onbRoutes").innerHTML = keys.length
    ? keys.map((k) => `<div><strong>${esc(k)}</strong> → <span class="mono">${esc(fm[k])}</span></div>`).join("")
    : "—";
  const ready = document.getElementById("onbReady");
  ready.textContent = draft.ready ? "ready ✓" : "draft";
  ready.className = `pill ${draft.ready ? "ok" : "warn"}`;
}

async function sendOnboard(project, btn, explicitText) {
  const input = document.getElementById("onbInput");
  const text = explicitText !== undefined ? explicitText : (input ? input.value.trim() : "");
  await busyClick(btn, async () => {
    if (input) input.value = "";
    const box = document.getElementById("chat");
    if (box) {
      if (text) box.insertAdjacentHTML("beforeend", `<div class="msg user">${esc(text)}</div>`);
      box.insertAdjacentHTML("beforeend", `<div class="msg assistant">🔎 looking at your app &amp; thinking…</div>`);
      box.scrollTop = box.scrollHeight;
    }
    const { job } = await act(post(`/api/projects/${enc(project)}/onboard/message`, { text }));
    const done = await waitForJob(job);
    if (done.status !== "succeeded") toast(done.error || "The assistant hit an error — see Jobs.");
    await loadOnboard(project);
  }, { busyLabel: "…" });
}

function waitForJob(jobId) {
  return new Promise((resolve) => {
    const done = new Set(["succeeded", "failed", "cancelled"]);
    const t = setInterval(async () => {
      let j;
      try { j = await api(`/api/jobs/${jobId}`); } catch { return; }
      if (done.has(j.status)) { clearInterval(t); resolve(j); }
    }, 1500);
  });
}

/* =======================================================================
   Profile editor
   ======================================================================= */

async function renderProfileEditor(project) {
  const data = await act(api(`/api/projects/${enc(project)}/profile`));
  view.innerHTML = `
    <div class="pagehead">
      <div><h1>Profile · ${esc(project)}</h1>
        <p><a class="link" href="#/">← Projects</a> · validated against the profile schema on save</p></div>
      <div class="actions"><button class="primary" id="profSave">Save profile</button></div>
    </div>
    <div class="card">
      <textarea id="profYaml" style="min-height:520px;font-family:var(--mono);font-size:12.5px"></textarea>
      <div class="errbox" id="profError" style="display:none;margin-top:10px"></div>
    </div>`;
  document.getElementById("profYaml").value = data.yaml;
  document.getElementById("profSave").onclick = async (e) => {
    const errBox = document.getElementById("profError");
    errBox.style.display = "none";
    try {
      await post(`/api/projects/${enc(project)}/profile`,
        { yaml: document.getElementById("profYaml").value }, "PUT");
      e.target.textContent = "Saved ✓";
      setTimeout(() => (e.target.textContent = "Save profile"), 1500);
    } catch (err) {
      errBox.textContent = err.message.slice(0, 600);
      errBox.style.display = "";
    }
  };
}

/* ---------- boot ---------- */

loadMeta().then(route);
