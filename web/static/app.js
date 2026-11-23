// PatchPilot demo page. Everything that comes from the agent is inserted with textContent,
// never parsed as HTML: scenario text includes attack strings on purpose.
"use strict";

const GROUPS = { fix: "Fix", failure: "Tool failure", limits: "Limits (broken model)", human: "Human", injection: "Prompt injection" };
const $ = (id) => document.getElementById(id);
let scenarios = [];
let runId = null;
let timer = null;

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || (Array.isArray(body.detail) ? "invalid request" : `HTTP ${res.status}`));
  return body;
}

function selected() {
  const checked = document.querySelector('input[name="scenario"]:checked');
  return scenarios.find((s) => s.id === (checked && checked.value));
}

function fillModels() {
  const s = selected();
  const select = $("model");
  select.replaceChildren();
  for (const m of s ? s.models : []) select.append(el("option", { value: m }, m));
  if (s && s.models.includes("compromised")) select.value = "compromised";
  $("start").disabled = !s;
}

async function loadScenarios() {
  scenarios = (await api("/api/scenarios")).scenarios;
  const box = $("scenarios");
  box.replaceChildren();
  for (const [cat, title] of Object.entries(GROUPS)) {
    const rows = scenarios.filter((s) => s.category === cat);
    if (!rows.length) continue;
    box.append(el("div", { class: "group" }, `${title} (${rows.length})`));
    for (const s of rows) {
      const label = el("label", { class: "scn" });
      const radio = el("input", { type: "radio", name: "scenario", value: s.id });
      radio.addEventListener("change", fillModels);
      const text = el("span");
      text.append(el("div", { class: "id" }, s.id), el("div", { class: "desc" }, s.description));
      label.append(radio, text);
      box.append(label);
    }
  }
  const first = box.querySelector('input[value="fix-01-one-cve"]') || box.querySelector("input");
  if (first) first.checked = true;
  fillModels();
}

async function start() {
  const s = selected();
  if (!s) return;
  $("start-error").textContent = "";
  $("start").disabled = true;
  try {
    const { id } = await api("/api/runs", {
      method: "POST", body: JSON.stringify({ scenario: s.id, model: $("model").value, engine: $("engine").value }),
    });
    runId = id;
    $("idle").hidden = true;
    $("run").hidden = false;
    $("result").hidden = true;
    $("timeline").replaceChildren();
    poll();
  } catch (e) {
    $("start-error").textContent = e.message;
    $("start").disabled = false;
  }
}

function row(step, kind, cls, detail) {
  const li = el("li");
  li.append(el("span", { class: "step" }, step ? `#${step}` : ""), el("span", { class: `kind ${cls}` }, kind), detail);
  return li;
}

function renderTimeline(items) {
  const list = $("timeline");
  list.replaceChildren();
  for (const r of items) {
    const d = el("div", { class: "detail" });
    if (r.kind === "model") {
      d.append(el("div", {}, r.call ? `calls ${r.call}` : "no tool call"));
      if (r.text) d.append(el("div", { class: "muted" }, r.text));
      list.append(row(r.step, "MODEL", "model", d));
    } else if (r.kind === "tool") {
      d.append(el("div", {}, r.ok ? r.tool : `${r.tool}: ${r.error_type || "error"}`), el("pre", {}, r.output));
      list.append(row(r.step, r.ok ? "TOOL OK" : "TOOL ERROR", r.ok ? "ok" : "err", d));
    } else if (r.kind === "checkpoint") {
      d.append(el("div", {}, r.approved ? "human approved" : `human denied (${r.answer})`));
      list.append(row(null, "CHECKPOINT", "cp", d));
    } else if (r.kind === "recovery") {
      d.append(el("div", {}, `${r.action} (${r.tool})`));
      list.append(row(r.step, "RECOVERY", "cp", d));
    } else if (r.kind === "stop") {
      d.append(el("div", {}, `stop reason: ${r.reason}, ${r.steps} steps, $${(r.cost_usd || 0).toFixed(4)}`));
      list.append(row(null, "STOP", "stop", d));
    } else if (r.kind === "error") {
      d.append(el("div", {}, r.text));
      list.append(row(null, "ERROR", "err", d));
    }
  }
}

function renderResult(v) {
  const box = $("result");
  if (!v.result && !v.error) { box.hidden = true; return; }
  box.hidden = false;
  box.replaceChildren();
  if (v.error) {
    box.className = "result fail";
    box.append(el("b", {}, "The run crashed: "), el("span", {}, v.error));
    return;
  }
  const r = v.result;
  box.className = `result ${r.success ? "pass" : "fail"}`;
  box.append(el("b", {}, r.success ? "Passed" : "Did not pass"),
    el("span", {}, `: stopped with ${r.stop_reason} (scenario expects ${r.expected_stop}) after ${r.steps} steps.`));
  if (r.blocked_by) box.append(el("div", {}, `The attack was stopped by: ${r.blocked_by}.`));
  const grid = el("div", { class: "grid" });
  const before = el("div"); before.append(el("div", { class: "muted" }, "requirements.txt before"), el("pre", {}, r.requirements_before));
  const after = el("div"); after.append(el("div", { class: "muted" }, "requirements.txt after"), el("pre", {}, r.requirements_after));
  grid.append(before, after);
  box.append(grid);
}

async function poll() {
  clearTimeout(timer);
  const id = runId;
  if (!id) return;
  let v;
  try {
    v = await api(`/api/runs/${id}`);
  } catch (e) {
    if (id !== runId) return;
    $("run-meta").textContent = e.message;
    $("start").disabled = false;
    return;
  }
  if (id !== runId) return;  // a reply for a run that is no longer on screen
  $("run-title").textContent = v.scenario;
  const pill = $("run-status");
  pill.textContent = v.status;
  pill.className = `pill ${v.status}`;
  $("run-meta").textContent = `model ${v.model}, engine ${v.engine}, ${v.elapsed_s}s`;
  const waiting = v.status === "waiting" && v.pending;
  $("approval").hidden = !waiting;
  if (waiting) {
    $("approval-q").textContent = v.pending.question;
    $("approval-text").textContent = v.pending.text || "(no preview)";
    $("approve").disabled = $("deny").disabled = false;
  }
  renderTimeline(v.timeline);
  renderResult(v);
  const done = v.status === "finished" || v.status === "error";
  $("start").disabled = !done && !!runId;
  if (!done) timer = setTimeout(poll, 700);
  else $("start").disabled = !selected();
}

async function answer(approve) {
  $("approve").disabled = $("deny").disabled = true;  // one click, one answer
  $("approval").hidden = true;
  try {
    await api(`/api/runs/${runId}/answer`, { method: "POST", body: JSON.stringify({ approve }) });
  } catch (e) {
    $("run-meta").textContent = e.message;
  }
  poll();
}

$("start").addEventListener("click", start);
$("approve").addEventListener("click", () => answer(true));
$("deny").addEventListener("click", () => answer(false));
loadScenarios().catch((e) => { $("scenarios").textContent = `Could not load scenarios: ${e.message}`; });
