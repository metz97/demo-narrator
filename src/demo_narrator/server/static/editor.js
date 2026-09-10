/* Flow editor (M2): step cards with inline editing, guidance, test-this-beat,
   regenerate-with-diff, and version history. Loaded before app.js. */

const ACTIONS = ["goto", "click", "fill", "press", "select", "hover", "scroll_to", "highlight", "wait_for", "expect", "dwell"];
const LOCATOR_ACTIONS = new Set(["click", "fill", "select", "hover", "scroll_to", "highlight", "wait_for", "expect", "press"]);
const STRATEGIES = ["testid", "role", "label", "text", "placeholder", "css"];

let ed = null; // { project, name, flow, dirty, savedYaml }

async function renderFlowEditor(project, flowName) {
  const data = await api(`/api/projects/${encodeURIComponent(project)}/flows/${encodeURIComponent(flowName)}`);
  ed = { project, name: data.name, flow: data.flow, dirty: false, savedYaml: data.yaml };
  view.innerHTML = `
    <div class="pagehead">
      <div>
        <h1 id="edTitle"></h1>
        <p><a class="link" href="#/p/${encodeURIComponent(project)}/run/2">← Flows</a>
          · <span class="mono">${esc(data.file)}</span></p>
      </div>
      <div class="actions">
        <button id="edYamlBtn">YAML</button>
        <button class="primary" id="edSave" disabled>Save</button>
      </div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="row">
        <input type="text" id="edNote" placeholder="version note (optional)" style="width:230px">
        <select id="edVersions"><option value="">history…</option></select>
        <button class="sm" id="edRestore" disabled>Restore</button>
      </div>
      <div class="errbox" id="edError" style="margin-top:10px;display:none"></div>
      <pre id="edYaml" class="log" style="display:none;margin-top:10px"></pre>
    </div>
    <div id="edBeats"></div>
    <h2 class="section">Jobs</h2>
    <div id="jobs"><div class="muted">No jobs yet.</div></div>`;

  document.getElementById("edSave").onclick = saveFlow;
  document.getElementById("edYamlBtn").onclick = () => {
    const el = document.getElementById("edYaml");
    el.textContent = ed.savedYaml + "\n# (read-only: last saved state — edit via the cards or the file itself)";
    el.style.display = el.style.display === "none" ? "" : "none";
  };
  const versionsSel = document.getElementById("edVersions");
  versionsSel.onchange = () => { document.getElementById("edRestore").disabled = !versionsSel.value; };
  document.getElementById("edRestore").onclick = restoreVersion;

  loadVersions();
  paintEditor();
  startJobPolling(project);
}

function markDirty() {
  ed.dirty = true;
  document.getElementById("edSave").disabled = false;
}

function paintEditor() {
  document.getElementById("edTitle").textContent = ed.flow.title || ed.name;
  const box = document.getElementById("edBeats");
  box.innerHTML = "";
  ed.flow.beats.forEach((beat, bi) => box.appendChild(beatCard(beat, bi)));
}

/* ---------- beat ---------- */

function beatCard(beat, bi) {
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `
    <div class="row">
      <div class="grow">
        <div class="mono-sub">beat ${bi}${beat.task ? ` · task ${beat.task}` : ""}</div>
        <input type="text" class="beatTitle" value="${esc(beat.title || "")}" placeholder="feature title"
               style="width:70%;margin-top:6px;font-weight:700">
      </div>
      <button class="sm" data-testbeat>▶ Test this beat</button>
      ${beat.task ? `<button class="sm" data-regen>⟳ Regenerate</button>` : ""}
    </div>
    ${beat.task ? `<details style="margin-top:10px"><summary class="muted">Guidance for the generator</summary>
      <textarea class="beatGuidance" placeholder="Where the feature lives, exact buttons, which record to use…"></textarea>
      <button class="sm" data-savehint style="margin-top:6px">save guidance</button></details>` : ""}
    <div class="steps"></div>
    <button class="sm" data-addstep style="margin-top:10px">+ add step</button>
    <div data-preview></div>
    <div data-diff></div>`;

  card.querySelector(".beatTitle").oninput = (e) => { beat.title = e.target.value; markDirty(); };
  card.querySelector("[data-testbeat]").onclick = () => testBeat(bi, card);
  card.querySelector("[data-addstep]").onclick = () => {
    beat.steps.push({ action: "dwell", say: "", min_duration_sec: 2.0 });
    markDirty(); paintEditor();
  };
  const regenBtn = card.querySelector("[data-regen]");
  if (regenBtn) regenBtn.onclick = () => regenerateBeat(beat, bi, card);
  if (beat.task) {
    const ta = card.querySelector(".beatGuidance");
    api(`/api/projects/${encodeURIComponent(ed.project)}/hints/${beat.task}`)
      .then((h) => { ta.value = h.text; });
    card.querySelector("[data-savehint]").onclick = async (e) => {
      await api(`/api/projects/${encodeURIComponent(ed.project)}/hints/${beat.task}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: ta.value }),
      });
      e.target.textContent = "saved ✓";
      setTimeout(() => (e.target.textContent = "save guidance"), 1500);
    };
  }

  const stepsBox = card.querySelector(".steps");
  beat.steps.forEach((step, si) => stepsBox.appendChild(stepCard(beat, step, si)));
  return card;
}

/* ---------- step ---------- */

function stepCard(beat, step, si) {
  const el = document.createElement("div");
  el.className = "step";
  const loc = step.locator || {};
  const strategy = STRATEGIES.find((s) => loc[s] != null) || "testid";
  el.innerHTML = `
    <div class="row">
      <span class="pill">${si}</span>
      <select data-f="action">${ACTIONS.map((a) => `<option ${a === step.action ? "selected" : ""}>${a}</option>`).join("")}</select>
      <span data-fields class="row grow"></span>
      <button class="xs" data-up title="move up">↑</button>
      <button class="xs" data-down title="move down">↓</button>
      <button class="xs" data-del title="delete">✕</button>
    </div>
    <textarea data-f="say" placeholder="narration (leave empty for a silent step)">${esc(step.say || "")}</textarea>
    <div class="row muted" style="margin-top:6px">
      <label>settle ms <input type="number" data-f="settle_ms" value="${step.settle_ms ?? ""}" style="width:80px"></label>
      <label>min duration s <input type="number" step="0.5" data-f="min_duration_sec" value="${step.min_duration_sec ?? ""}" style="width:80px"></label>
    </div>`;

  const fields = el.querySelector("[data-fields]");
  const paintFields = () => {
    fields.innerHTML = "";
    if (step.action === "goto") {
      fields.innerHTML = `<input type="text" data-f="path" placeholder="/route/path" value="${esc(step.path || "")}" style="width:280px">`;
    } else if (LOCATOR_ACTIONS.has(step.action)) {
      fields.innerHTML = `
        <select data-l="strategy">${STRATEGIES.map((s) => `<option ${s === strategy ? "selected" : ""}>${s}</option>`).join("")}</select>
        <input type="text" data-l="value" placeholder="locator value" value="${esc(loc[strategy] ?? "")}" style="width:170px">
        <input type="text" data-l="name" placeholder="name (role only)" value="${esc(loc.name ?? "")}" style="width:130px">
        <input type="number" data-l="nth" placeholder="nth" value="${loc.nth ?? ""}" style="width:64px">
        ${step.action === "fill" || step.action === "select" ? `<input type="text" data-f="value" placeholder="text to type" value="${esc(step.value ?? "")}" style="width:140px">` : ""}
        ${step.action === "press" ? `<input type="text" data-f="keys" placeholder="keys e.g. Enter" value="${esc(step.keys ?? "")}" style="width:110px">` : ""}
        ${step.action === "wait_for" || step.action === "expect" ? `<select data-f="state">${["visible", "hidden", "attached"].map((s) => `<option ${s === (step.state || "visible") ? "selected" : ""}>${s}</option>`).join("")}</select>` : ""}`;
    }
    bindFieldHandlers();
  };

  const bindFieldHandlers = () => {
    el.querySelectorAll("[data-f]").forEach((input) => {
      input.oninput = () => {
        const key = input.dataset.f;
        let v = input.value;
        if (input.type === "number") v = v === "" ? undefined : Number(v);
        if (key === "action") { step.action = v; if (!LOCATOR_ACTIONS.has(v)) delete step.locator; paintFields(); }
        else if (v === "" || v === undefined) delete step[key];
        else step[key] = v;
        markDirty();
      };
    });
    el.querySelectorAll("[data-l]").forEach((input) => {
      input.oninput = () => {
        const cur = step.locator || {};
        const stratSel = el.querySelector('[data-l="strategy"]');
        const valInput = el.querySelector('[data-l="value"]');
        const nameInput = el.querySelector('[data-l="name"]');
        const nthInput = el.querySelector('[data-l="nth"]');
        const next = {};
        if (valInput.value !== "") next[stratSel.value] = valInput.value;
        if (stratSel.value === "role" && nameInput.value !== "") next.name = nameInput.value;
        if (nthInput.value !== "") next.nth = Number(nthInput.value);
        step.locator = Object.keys(next).length ? next : undefined;
        if (!step.locator) delete step.locator;
        markDirty();
      };
    });
  };

  el.querySelector("[data-del]").onclick = () => { beat.steps.splice(si, 1); markDirty(); paintEditor(); };
  el.querySelector("[data-up]").onclick = () => {
    if (si > 0) { [beat.steps[si - 1], beat.steps[si]] = [beat.steps[si], beat.steps[si - 1]]; markDirty(); paintEditor(); }
  };
  el.querySelector("[data-down]").onclick = () => {
    if (si < beat.steps.length - 1) { [beat.steps[si + 1], beat.steps[si]] = [beat.steps[si], beat.steps[si + 1]]; markDirty(); paintEditor(); }
  };
  paintFields();
  return el;
}

/* ---------- save / versions ---------- */

async function saveFlow() {
  const errBox = document.getElementById("edError");
  errBox.style.display = "none";
  try {
    const note = document.getElementById("edNote").value.trim() || null;
    const res = await post(
      `/api/projects/${encodeURIComponent(ed.project)}/flows/${encodeURIComponent(ed.name)}`,
      { flow: ed.flow, note },
      "PUT",
    );
    ed.dirty = false;
    document.getElementById("edSave").disabled = true;
    document.getElementById("edSave").textContent = `Saved v${res.version} ✓`;
    setTimeout(() => (document.getElementById("edSave").textContent = "Save"), 2000);
    const fresh = await api(`/api/projects/${encodeURIComponent(ed.project)}/flows/${encodeURIComponent(ed.name)}`);
    ed.savedYaml = fresh.yaml;
    loadVersions();
  } catch (e) {
    errBox.textContent = e.message.slice(0, 600);
    errBox.style.display = "";
  }
}

async function loadVersions() {
  const versions = await api(`/api/projects/${encodeURIComponent(ed.project)}/flows/${encodeURIComponent(ed.name)}/versions`);
  const sel = document.getElementById("edVersions");
  sel.innerHTML = `<option value="">history (${versions.length})…</option>` +
    versions.map((v) => `<option value="${v.seq}">v${v.seq} · ${esc(v.note || v.created_at)}</option>`).join("");
}

async function restoreVersion() {
  const seq = document.getElementById("edVersions").value;
  if (!seq) return;
  const v = await api(`/api/projects/${encodeURIComponent(ed.project)}/flows/${encodeURIComponent(ed.name)}/versions/${seq}`);
  ed.flow = v.flow;
  markDirty();
  paintEditor();
}

/* ---------- test beat ---------- */

async function testBeat(bi, card) {
  const res = await post(
    `/api/projects/${encodeURIComponent(ed.project)}/flows/${encodeURIComponent(ed.name)}/test-beat`,
    { flow: ed.flow, beat_index: bi },
  );
  const slot = card.querySelector("[data-preview]");
  slot.innerHTML = `<div class="muted" style="margin-top:10px">⏳ recording preview (job ${res.job})…</div>`;
  startJobPolling(ed.project);
  const wait = setInterval(async () => {
    const job = await api(`/api/jobs/${res.job}`);
    if (job.status === "succeeded") {
      clearInterval(wait);
      slot.innerHTML = `<video controls src="/api/jobs/${res.job}/video"></video>`;
    } else if (job.status === "failed") {
      clearInterval(wait);
      slot.innerHTML = `<div class="muted" style="color:var(--danger);margin-top:10px">preview failed: ${esc(job.error || "")}</div>`;
    }
  }, 2500);
}

/* ---------- regenerate + diff ---------- */

async function regenerateBeat(beat, bi, card) {
  const guidance = card.querySelector(".beatGuidance")?.value?.trim() || null;
  const res = await post(`/api/projects/${encodeURIComponent(ed.project)}/generate`, {
    tasks: [{ id: beat.task, guidance }],
  });
  const jobId = res.jobs[0];
  const slot = card.querySelector("[data-diff]");
  slot.innerHTML = `<div class="muted" style="margin-top:10px">⏳ regenerating (job ${jobId}) — this takes a few minutes…</div>`;
  startJobPolling(ed.project);
  const wait = setInterval(async () => {
    const job = await api(`/api/jobs/${jobId}`);
    if (job.status === "succeeded") {
      clearInterval(wait);
      const candidateName = job.result.flow;
      const cand = await api(`/api/projects/${encodeURIComponent(ed.project)}/flows/${encodeURIComponent(candidateName)}`);
      const newSteps = cand.flow.beats[0].steps;
      slot.innerHTML = `
        <div class="card" style="margin-top:10px;background:var(--panel2)">
          <strong>Regenerated ${newSteps.length} steps</strong> — replace this feature's current ${beat.steps.length}?
          <div class="row" style="align-items:flex-start;margin-top:8px">
            <div class="grow"><div class="muted">current</div>${stepSummaryList(beat.steps)}</div>
            <div class="grow"><div class="muted">new</div>${stepSummaryList(newSteps)}</div>
          </div>
          <div class="row" style="margin-top:8px">
            <button data-accept>Accept new steps</button>
            <button class="sm" data-dismiss>Dismiss</button>
          </div>
        </div>`;
      slot.querySelector("[data-accept]").onclick = () => {
        beat.steps = newSteps;
        markDirty(); paintEditor();
      };
      slot.querySelector("[data-dismiss]").onclick = () => { slot.innerHTML = ""; };
    } else if (job.status === "failed") {
      clearInterval(wait);
      slot.innerHTML = `<div class="muted" style="color:var(--danger);margin-top:10px">regeneration failed: ${esc(job.error || "")}</div>`;
    }
  }, 3000);
}

function stepSummaryList(steps) {
  return `<ol class="muted" style="padding-left:18px;font-size:13px">${steps.map((s) => {
    const loc = s.locator ? esc(JSON.stringify(s.locator)) : esc(s.path || "");
    return `<li><strong>${esc(s.action)}</strong> ${loc}${s.say ? `<br><em>"${esc(s.say)}"</em>` : ""}</li>`;
  }).join("")}</ol>`;
}
