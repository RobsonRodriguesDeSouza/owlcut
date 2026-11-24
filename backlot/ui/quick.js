// /clips/quick/<id> — progress and results of a local (no-Claude) clips job.
import { el, getJSON } from "/ui/lib.js";
import { postJSON } from "/ui/create-lib.js";

const $ = (id) => document.getElementById(id);
const qid = decodeURIComponent(location.pathname.split("/clips/quick/")[1] || "").replace(/\/$/, "");
let doc = null;
let clockTimer = null;

const STEPS = [
  ["source", "Getting the video"],
  ["transcribe", "Transcribing"],
  ["pick", "Picking clips"],
  ["render", "Rendering"],
  ["done", "Ready"],
];
const ORDER = { source: 0, audio: 1, transcribe: 1, pick: 2, render: 3, done: 4 };
const LABEL = {
  length: { short: "15–30 s", medium: "30–60 s", long: "60–90 s" },
  framing: { auto: "auto framing", fill: "fill screen", fit: "blurred background" },
  captions: { highlight: "word highlight", clean: "clean captions", none: "no captions" },
};

function ts(s) {
  s = Math.max(0, Math.round(+s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}
function mb(n) { return n ? `${(n / 1048576).toFixed(1)} MB` : ""; }

function renderHeader() {
  $("title").textContent = doc.title || qid;
  document.title = `OwlCut — ${doc.title || "Quick clips"}`;
  const o = doc.options || {}, s = doc.source || {};
  const meta = $("meta");
  meta.innerHTML = "";
  meta.append(el("span", { class: "chip accent" }, "Local"));
  if (s.name || s.url) meta.append(el("span", { class: "chip" }, s.name || "YouTube"));
  if (s.duration) meta.append(el("span", { class: "chip" }, `original ${ts(s.duration)}`));
  meta.append(el("span", { class: "chip" }, o.mode === "all" ? "whole video" : `best ${o.count}`));
  meta.append(el("span", { class: "chip" }, LABEL.length[o.length] || o.length));
  meta.append(el("span", { class: "chip" }, LABEL.framing[o.framing] || o.framing));
  meta.append(el("span", { class: "chip" }, LABEL.captions[o.captions] || o.captions));
}

function renderStatus() {
  const box = $("status");
  const st = doc.status;
  const p = doc.progress || {};
  const running = st === "running";
  box.classList.toggle("done", st === "done");
  box.classList.toggle("failed", st === "error" || st === "cancelled");
  $("spin").style.display = running ? "" : "none";
  $("stTitle").textContent = running
    ? (p.step === "render" ? `Rendering — ${p.done || 0} of ${p.total} ready` : p.step === "transcribe" ? "Transcribing the speech" : "Working…")
    : st === "done" ? `${doc.clips.length} ${doc.clips.length === 1 ? "clip is" : "clips are"} ready`
      : st === "cancelled" ? "Cancelled" : "Stopped with an error";
  $("stSub").textContent = running
    ? "Everything runs on your Mac — no Claude usage. You can leave this page; it keeps going."
    : st === "done"
      ? ((doc.options || {}).mode !== "all" && doc.clips.length < (doc.options || {}).count
        ? `Only ${doc.clips.length} of the ${doc.options.count} requested fit without overlapping at this length — try shorter clips for more. Watch them below.`
        : "Watch them below and download the ones you like.")
      : st === "cancelled" ? "Nothing else will be processed. Clips already rendered stay below."
        : doc.error || "Something went wrong.";

  const cur = running ? (ORDER[p.step] ?? 0) : st === "done" ? 4 : (ORDER[p.step] ?? 0);
  const ol = $("steps");
  ol.innerHTML = "";
  STEPS.forEach(([key, label], i) => {
    const text = key === "render" && p.total ? `${label} ${Math.min(p.done || 0, p.total)}/${p.total}` : label;
    const cls = i < cur || (st === "done") ? "done" : i === cur && running ? "active" : "";
    ol.append(el("li", { class: cls }, el("span", { class: "pdot" }), text));
  });
  // overall bar: source 0-10, transcribe 10-55, pick 55-60, render 60-100
  let pct = 0;
  if (st === "done") pct = 100;
  else if (p.step === "source") pct = p.total ? (p.done / p.total) * 10 : 3;
  else if (p.step === "audio") pct = 10;
  else if (p.step === "transcribe") pct = 10 + (p.total ? (p.done / p.total) * 45 : 0);
  else if (p.step === "pick") pct = 57;
  else if (p.step === "render") pct = 60 + (p.total ? (p.done / p.total) * 40 : 0);
  $("bar").style.width = `${Math.round(running ? Math.max(pct, 3) : pct)}%`;
  $("barWrap").setAttribute("aria-valuenow", String(Math.round(pct)));
  $("msg").textContent = running ? p.message || "" : "";
  $("cancelBtn").hidden = !running;
  $("deleteBtn").hidden = running;

  clearInterval(clockTimer);
  const tick = () => {
    const end = running ? Date.now() / 1000 : doc.updated_at;
    $("clock").textContent = ts(end - doc.created_at);
  };
  tick();
  if (running) clockTimer = setInterval(tick, 1000);
}

// Where these clips live in Documents/OwlCutVideos (saved after every render and on every zip download).
let savedFor = null;
function paintSaved(info, msg, cls = "") {
  const row = $("savedRow");
  row.hidden = false;
  row.className = `quick-saved ${cls}`;
  $("savedText").textContent = msg || (info && info.folder ? `Saved to ${info.shown}` : "Not in Documents/OwlCutVideos yet.");
  $("savedText").title = (info && info.folder) || "";
  $("savedOpen").hidden = !(info && info.folder);
  $("savedSave").hidden = !!(info && info.folder) || doc.status === "running";
}
async function loadSaved(force = false) {
  if (!force && savedFor === qid + doc.status) return;
  savedFor = qid + doc.status;
  try { paintSaved(await getJSON(`/api/quickclips/${encodeURIComponent(qid)}/saved`)); } catch { /* offline */ }
}
async function saveNow() {
  $("savedSave").disabled = true;
  paintSaved(null, "Saving to OwlCutVideos…");
  $("savedSave").hidden = true;
  try {
    const r = await fetch(`/api/quickclips/${encodeURIComponent(qid)}/save`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    paintSaved(data, null, "ok");
  } catch (e) {
    paintSaved(null, e.message || "Couldn't save.", "err");
  } finally { $("savedSave").disabled = false; }
}

function renderResults() {
  const clips = doc.clips || [];
  $("results").hidden = !clips.length;
  if (!clips.length) return;
  $("count").textContent = clips.length === 1 ? "1 clip" : `${clips.length} clips`;
  $("zipLink").href = `/api/quickclips/${encodeURIComponent(qid)}/zip`;
  $("zipLink").hidden = doc.status === "running";
  $("zipLink").onclick = () => setTimeout(() => loadSaved(true), 1500);   // the zip download also saves
  $("savedSave").onclick = saveNow;
  $("savedOpen").onclick = () => fetch(`/api/quickclips/${encodeURIComponent(qid)}/reveal`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
    .then((r) => (r.ok ? null : loadSaved(true)));
  loadSaved();
  const grid = $("grid");
  const sig = clips.map((c) => c.id + c.rendered_at).join("|");
  if (grid.dataset.sig === sig) return;   // keep players while nothing changed
  grid.dataset.sig = sig;
  grid.innerHTML = "";
  clips.forEach((c, i) => {
    const vid = el("video", { src: c.video_url, poster: c.thumb_url, controls: "", preload: "none", playsinline: "" });
    grid.append(el("article", { class: "render-card" },
      el("div", { class: "render-frame", style: "aspect-ratio:9/16" }, vid),
      el("div", { class: "render-meta quick-meta" },
        el("b", {}, `#${i + 1} ${c.title || c.id}`),
        el("span", { class: "mono" }, `${ts(c.start)} → ${ts(c.end)} · ${Math.round(c.duration)} s`),
        c.score != null ? el("span", { class: "chip", title: "Heuristic score: pace, energy, few pauses, hook, clean ending" }, `score ${c.score}`) : null,
        el("a", { class: "btn small", href: c.download_url, download: "" }, `Download ${mb(c.size)}`),
      ),
      c.transcript ? el("details", { class: "more quick-tx" }, el("summary", {}, "Transcript"), el("p", { class: "transcript" }, c.transcript)) : null,
    ));
  });
}

async function refresh() {
  try {
    doc = await getJSON(`/api/quickclips/${encodeURIComponent(qid)}`);
  } catch {
    $("title").textContent = "Not found";
    $("stTitle").textContent = "This quick clips job doesn't exist (it may have been deleted).";
    $("spin").style.display = "none";
    return false;
  }
  renderHeader();
  renderStatus();
  renderResults();
  return true;
}

$("cancelBtn").addEventListener("click", async () => {
  $("cancelBtn").disabled = true;
  $("cancelBtn").textContent = "Cancelling…";
  try { await postJSON(`/api/quickclips/${encodeURIComponent(qid)}/cancel`, {}); } catch (e) { alert(e.message); }
  $("cancelBtn").disabled = false;
  $("cancelBtn").textContent = "Cancel";
  refresh();
});
$("deleteBtn").addEventListener("click", async () => {
  if (!confirm("Delete these clips and the copy of the video? This can't be undone.")) return;
  try {
    await postJSON(`/api/quickclips/${encodeURIComponent(qid)}/delete`, {});
    location.href = "/clips?engine=local";
  } catch (e) { alert(e.message); }
});

async function loop() {
  const ok = await refresh();
  if (!ok) return;
  if (doc.status === "running") setTimeout(loop, document.hidden ? 8000 : 2000);
}
loop();
