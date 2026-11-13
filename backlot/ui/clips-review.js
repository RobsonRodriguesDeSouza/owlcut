// /clips/<job> — review, trim and approve the clips the agent proposed.
// Reads the clips contract (projects/<id>/clips/clips.json) through
// /api/clips/<job>; approvals go back to the agent as a structured reply.

import { el, getJSON, mediaURL } from "/ui/lib.js";
import { postJSON, renderMarkdown, statusBadge } from "/ui/create-lib.js";

const $ = (id) => document.getElementById(id);
const jobId = decodeURIComponent(location.pathname.split("/clips/")[1] || "").replace(/\/$/, "");
const video = $("video");

let data = null;            // last API payload
let edits = {};             // clip id -> {start, end, selected}
let active = null;          // clip id playing as a segment
let segEnd = null;          // stop time for segment playback
let filter = "all";
let sending = false;
let lastSig = "";

const STAGE_LABEL = { idea: "Brief", script: "Transcript and candidates", scene_plan: "Framing", assets: "Captions and assets", edit: "Edit", compose: "Render", publish: "Publish" };

// ---------- formatting ----------
function ts(s, precise = false) {
  if (s == null || !Number.isFinite(+s)) return "—";
  const neg = s < 0; s = Math.abs(+s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const secTxt = precise ? sec.toFixed(1).padStart(4, "0") : String(Math.floor(sec)).padStart(2, "0");
  return (neg ? "-" : "") + (h ? `${h}:${String(m).padStart(2, "0")}:${secTxt}` : `${m}:${secTxt}`);
}
const secs = (n) => `${(Math.round(n * 10) / 10).toLocaleString("en-US")} s`;

// ---------- state helpers ----------
function clips() { return (data && data.clips && data.clips.clips) || []; }
function project() { return data && data.job && data.job.project_id; }
function busy() { return !data || data.job.status === "running" || sending; }

function cur(c) {
  const e = edits[c.id] || {};
  return {
    start: e.start ?? c.start,
    end: e.end ?? c.end,
    selected: e.selected ?? ["selected", "rendered"].includes(c.status),
  };
}
function changed(c) {
  const v = cur(c);
  return Math.abs(v.start - c.start) > 0.05 || Math.abs(v.end - c.end) > 0.05;
}
function setEdit(c, patch) {
  const v = cur(c);
  const next = { ...v, ...patch };
  const dur = (data.clips.source && data.clips.source.duration_seconds) || video.duration || Infinity;
  next.start = Math.max(0, Math.min(next.start, next.end - 1));
  next.end = Math.min(dur, Math.max(next.end, next.start + 1));
  edits[c.id] = next;
  renderList();
  renderTimeline();
  renderActionBar();
}

// If the agent marked nothing as selected, preselect its top N (N = requested count).
function seedSelection() {
  const list = clips();
  if (!list.length || list.some((c) => ["selected", "rendered"].includes(c.status))) return;
  const n = Number((data.job.form || {}).count) || 5;
  list.filter((c) => c.status !== "rejected").slice(0, n).forEach((c) => {
    if (!edits[c.id]) edits[c.id] = { start: c.start, end: c.end, selected: true };
  });
}

// ---------- header + agent message ----------
function renderHeader() {
  const j = data.job;
  const src = (data.clips && data.clips.source) || j.source || {};
  $("title").textContent = j.title || j.id;
  document.title = `OwlCut — ${j.title || "Clips"}`;
  const meta = $("meta");
  meta.innerHTML = "";
  meta.append(statusBadge(j.status));
  if (src.channel) meta.append(el("span", { class: "chip" }, src.channel));
  const dur = src.duration_seconds ?? src.duration;
  if (dur) meta.append(el("span", { class: "chip" }, `original ${ts(dur)}`));
  if (j.form && j.form.count) meta.append(el("span", { class: "chip" }, `requested: ${j.form.count} clips`));
  $("activityLink").href = `/job/${encodeURIComponent(j.id)}`;
  if (src.url) { $("sourceLink").href = src.url; $("sourceLink").hidden = false; }

  const msg = (j.last_message || "").trim();
  $("agentCard").hidden = !msg;
  $("agentCard").classList.toggle("attention", j.status === "waiting");
  if (msg) {
    $("agentMsg").innerHTML = renderMarkdown(msg);
    $("agentStatus").innerHTML = "";
    $("agentStatus").append(statusBadge(j.status));
  }
}

function renderProgress() {
  const hasClips = clips().length > 0;
  $("progress").hidden = hasClips;
  $("review").hidden = !hasClips;
  if (hasClips) return;
  const j = data.job;
  const err = data.clips && data.clips.error;
  $("progressText").textContent = err
    || (j.status === "running" ? "The agent is downloading and transcribing the video…"
      : j.status === "waiting" ? "The agent stopped and is waiting for your answer — see the message above."
        : j.status === "error" ? "The production stopped with an error. Check the message and the agent activity."
          : "Waiting for the agent.");
  $("progress").querySelector(".spinner").style.display = j.status === "running" ? "" : "none";
  const ol = $("progressStages");
  ol.innerHTML = "";
  for (const st of (j.stages || []).filter((s) => !s.undeclared)) {
    const cls = st.status === "completed" ? "done" : st.status === "in_progress" ? "active" : st.status === "awaiting_human" ? "await" : "";
    ol.append(el("li", { class: cls }, el("span", { class: "pdot" }), STAGE_LABEL[st.name] || st.name));
  }
}

// ---------- player + timeline ----------
function ensureSource() {
  const rel = data.clips && data.clips.source && data.clips.source.video_path;
  $("playerEmpty").hidden = !!rel;
  video.hidden = !rel;
  if (!rel) return;
  const url = mediaURL(project(), rel);
  if (video.dataset.src !== url) { video.dataset.src = url; video.src = url; }
}

function totalDuration() {
  return (data.clips.source && data.clips.source.duration_seconds) || (Number.isFinite(video.duration) ? video.duration : 0)
    || Math.max(0, ...clips().map((c) => c.end));
}

function renderTimeline() {
  const tl = $("timeline");
  tl.querySelectorAll(".tl-clip").forEach((n) => n.remove());
  const total = totalDuration();
  if (!total) return;
  clips().forEach((c, i) => {
    const v = cur(c);
    const mark = el("button", {
      type: "button",
      class: `tl-clip${v.selected ? " sel" : ""}${active === c.id ? " on" : ""}`,
      style: `left:${(v.start / total) * 100}%;width:${Math.max(0.6, ((v.end - v.start) / total) * 100)}%`,
      title: `#${i + 1} ${c.title} · ${ts(v.start)}–${ts(v.end)}`,
      "aria-label": `Play clip ${i + 1}: ${c.title}`,
      onclick: () => playClip(c),
    });
    tl.append(mark);
  });
}

function playClip(c) {
  if (video.hidden) return;
  const v = cur(c);
  active = c.id;
  segEnd = v.end;
  video.currentTime = v.start;
  video.play().catch(() => {});
  $("nowPlaying").textContent = `Playing: ${c.title || c.id} (${ts(v.start)} → ${ts(v.end)})`;
  renderTimeline();
  document.querySelectorAll(".clip").forEach((n) => n.classList.toggle("playing", n.dataset.id === c.id));
}

video.addEventListener("timeupdate", () => {
  const total = totalDuration();
  if (total) $("playhead").style.left = `${(video.currentTime / total) * 100}%`;
  $("clock").textContent = ts(video.currentTime, true);
  if (segEnd != null && video.currentTime >= segEnd) {
    video.pause();
    segEnd = null;
  }
});
video.addEventListener("seeking", () => {
  // a manual seek outside the active clip ends segment mode
  const c = clips().find((x) => x.id === active);
  if (c) { const v = cur(c); if (video.currentTime < v.start - 0.3 || video.currentTime > v.end + 0.3) segEnd = null; }
});
video.addEventListener("loadedmetadata", renderTimeline);

// ---------- clip list ----------
function scoreBars(scores) {
  const names = { hook: "Hook", coherence: "Stands alone", value: "Value", energy: "Energy", platform_fit: "Platform fit" };
  const box = el("div", { class: "scores" });
  for (const [k, label] of Object.entries(names)) {
    const v = Number(scores[k]);
    if (!Number.isFinite(v)) continue;
    const pct = Math.max(0, Math.min(100, v <= 10 ? v * 10 : v));
    box.append(el("div", { class: "score-row" }, el("span", {}, label), el("i", {}, el("b", { style: `width:${pct}%` }))));
  }
  return box.childNodes.length ? box : null;
}

function nudger(c, field, label) {
  const v = cur(c);
  const step = (d) => () => setEdit(c, { [field]: Math.round((v[field] + d) * 10) / 10 });
  const here = () => {
    if (video.hidden) return;
    setEdit(c, { [field]: Math.round(video.currentTime * 10) / 10 });
  };
  return el("div", { class: "nudge" },
    el("span", { class: "nudge-label" }, label),
    el("button", { type: "button", class: "nb", onclick: step(-0.5), "aria-label": `${label}: half a second earlier` }, "−0.5"),
    el("span", { class: "nudge-val mono" }, ts(v[field], true)),
    el("button", { type: "button", class: "nb", onclick: step(0.5), "aria-label": `${label}: half a second later` }, "+0.5"),
    el("button", { type: "button", class: "nb here", onclick: here, title: "Use the player's current position" }, "here"),
  );
}

function clipCard(c, rank) {
  const v = cur(c);
  const dur = v.end - v.start;
  const toggle = el("input", { type: "checkbox", "aria-label": `Use clip ${rank}` });
  toggle.checked = v.selected;
  toggle.addEventListener("change", () => setEdit(c, { selected: toggle.checked }));

  const score = c.score != null ? Math.round(c.score) : null;
  const head = el("div", { class: "clip-head" },
    el("label", { class: "use" }, toggle, el("span", {}, "Use")),
    el("span", { class: "rank mono" }, `#${rank}`),
    score != null ? el("span", { class: `score${score >= 80 ? " hi" : score < 60 ? " lo" : ""}`, title: "Agent score (0–100)" }, String(score)) : null,
    el("h3", {}, c.title || c.id),
  );

  const body = el("div", { class: "clip-body" });
  if (c.hook) body.append(el("p", { class: "hook" }, `“${c.hook}”`));
  body.append(el("div", { class: "clip-time" },
    el("button", { type: "button", class: "btn small play", onclick: () => playClip(c) }, "▶ Play segment"),
    el("span", { class: "mono" }, `${ts(v.start, true)} → ${ts(v.end, true)}`),
    el("span", { class: "dur" }, secs(dur)),
    changed(c) ? el("button", { type: "button", class: "linkish", onclick: () => { delete edits[c.id].start; delete edits[c.id].end; setEdit(c, { start: c.start, end: c.end }); } }, "undo trim") : null,
  ));
  body.append(el("div", { class: "nudges" }, nudger(c, "start", "Start"), nudger(c, "end", "End")));
  if (c.reason) body.append(el("p", { class: "reason" }, c.reason));
  const tags = el("div", { class: "probe-tags" });
  if (c.crop) tags.append(el("span", { class: "chip" }, `Framing: ${c.crop}`));
  if (c.status === "rejected") tags.append(el("span", { class: "chip" }, "Rejected by the agent"));
  if (Object.keys(c.renders || {}).length) tags.append(el("span", { class: "chip ok" }, "Rendered"));
  if (changed(c)) tags.append(el("span", { class: "chip accent" }, "Trimmed by you"));
  if (tags.childNodes.length) body.append(tags);
  const bars = scoreBars(c.scores || {});
  if (c.transcript || bars) {
    const det = el("details", { class: "more" }, el("summary", {}, "Transcript and scores"));
    if (bars) det.append(bars);
    if (c.transcript) det.append(el("p", { class: "transcript" }, c.transcript));
    body.append(det);
  }

  return el("article", {
    class: `clip${v.selected ? " selected" : ""}${c.status === "rejected" ? " rejected" : ""}${active === c.id ? " playing" : ""}`,
    "data-id": c.id,
  }, head, body);
}

function renderList() {
  const box = $("clipList");
  const openIds = new Set([...box.querySelectorAll("details.more[open]")].map((d) => d.closest(".clip").dataset.id));
  box.innerHTML = "";
  const list = clips();
  list.forEach((c, i) => {
    const v = cur(c);
    if (filter === "selected" && !v.selected) return;
    if (filter === "rejected" && c.status !== "rejected") return;
    const card = clipCard(c, i + 1);
    if (openIds.has(c.id)) { const d = card.querySelector("details.more"); if (d) d.open = true; }
    box.append(card);
  });
  if (!box.childNodes.length) box.append(el("div", { class: "empty-state" }, el("b", {}, "Nothing here"), "No clips match this filter."));
}

// ---------- renders ----------
function renderRenders() {
  const items = [];
  clips().forEach((c, i) => {
    for (const [aspect, rel] of Object.entries(c.renders || {})) items.push({ c, i, aspect, rel });
  });
  $("renders").hidden = !items.length;
  if (!items.length) return;
  $("renderCount").textContent = items.length === 1 ? "1 video" : `${items.length} videos`;
  $("publishLink").href = `/publish/${encodeURIComponent(jobId)}`;
  const grid = $("renderGrid");
  const sig = items.map((x) => x.rel).join("|");
  if (grid.dataset.sig === sig) return;  // keep players (and their playback) when nothing changed
  grid.dataset.sig = sig;
  grid.innerHTML = "";
  for (const { c, i, aspect, rel } of items) {
    const ratio = aspect.replace("x", ":").split(":").map(Number);
    const url = mediaURL(project(), rel);
    const vid = el("video", { src: url, controls: "", preload: "metadata", playsinline: "" });
    if (c.thumbnail) vid.setAttribute("poster", mediaURL(project(), c.thumbnail));
    grid.append(el("article", { class: "render-card" },
      el("div", { class: "render-frame", style: ratio.length === 2 && ratio[0] && ratio[1] ? `aspect-ratio:${ratio[0]}/${ratio[1]}` : "" }, vid),
      el("div", { class: "render-meta" },
        el("b", {}, `#${i + 1} ${c.title || c.id}`),
        el("span", { class: "chip" }, aspect.replace("x", ":")),
        el("a", { class: "btn small", href: url, download: rel.split("/").pop() }, "Download"),
      ),
    ));
  }
}

// ---------- action bar ----------
function selection() {
  return clips().filter((c) => cur(c).selected);
}

function mode() {
  const list = clips();
  if (!list.length) return "none";
  const st = (data.clips.status || "").toLowerCase();
  const anyRender = list.some((c) => Object.keys(c.renders || {}).length);
  if (st === "published") return "published";
  if (anyRender || st === "awaiting_review") return "review";
  return "select";
}

function renderActionBar() {
  const m = mode();
  const bar = $("actionBar");
  bar.hidden = m === "none" || m === "published";
  document.body.classList.toggle("has-action-bar", !bar.hidden);
  if (bar.hidden) return;
  const sel = selection();
  const total = sel.reduce((a, c) => a + (cur(c).end - cur(c).start), 0);
  const edited = sel.filter(changed);
  $("selCount").textContent = sel.length === 1 ? "1 clip selected" : `${sel.length} clips selected`;
  $("selTotal").textContent = sel.length ? ` · ${secs(total)} in total` : "";
  const btn = $("approveBtn");
  if (m === "select") btn.textContent = `Approve and render ${sel.length || ""}`.trim();
  else btn.textContent = edited.length ? `Re-render ${edited.length} trimmed` : "Approve finished clips";
  const isBusy = busy();
  btn.disabled = isBusy || (m === "select" && !sel.length);
  $("sendMore").disabled = isBusy;
  $("actionNote").textContent = isBusy && !sending
    ? "The agent is working — the buttons unlock when it stops."
    : data.job.status === "waiting" ? "The agent is waiting for your decision." : "";
}

function approvalMessage() {
  const m = mode();
  const sel = selection();
  const dropped = clips().filter((c) => !cur(c).selected);
  const line = (c) => {
    const v = cur(c);
    const adj = changed(c) ? ` (trimmed by me; was ${c.start.toFixed(1)} → ${c.end.toFixed(1)})` : "";
    return `- ${c.id} — "${c.title}": ${v.start.toFixed(1)} → ${v.end.toFixed(1)} s${adj}`;
  };
  if (m === "select") {
    return [
      "Selection approved on the /clips page. Render exactly these clips, in this order (times in seconds of the source video):",
      ...sel.map(line),
      dropped.length ? `Drop: ${dropped.map((c) => c.id).join(", ")}.` : "",
      "Keep clips.json updated (clip statuses and overall status) as you go.",
    ].filter(Boolean).join("\n");
  }
  const edited = sel.filter(changed);
  if (edited.length) {
    return [
      "Re-render only these clips with the new times (seconds of the source video) and keep the others:",
      ...edited.map(line),
      "Update the renders and clips.json when done.",
    ].join("\n");
  }
  return "Clips approved on the /clips page. Go ahead to the publish stage.";
}

async function send(text) {
  if (!text.trim()) return;
  sending = true;
  renderActionBar();
  try {
    await postJSON(`/api/jobs/${encodeURIComponent(jobId)}/reply`, { text });
    $("moreText").value = "";
    $("moreBox").open = false;
    $("actionNote").textContent = "Sent. The agent is back to work.";
  } catch (e) {
    $("actionNote").textContent = `Couldn't send: ${e.message}`;
  } finally {
    sending = false;
    await refresh();
  }
}

$("approveBtn").addEventListener("click", () => send(approvalMessage()));
$("sendMore").addEventListener("click", () => send($("moreText").value));
document.querySelectorAll('input[name="filter"]').forEach((r) => r.addEventListener("change", () => { filter = r.value; renderList(); }));

// ---------- loop ----------
async function refresh() {
  try {
    data = await getJSON(`/api/clips/${encodeURIComponent(jobId)}`);
  } catch (e) {
    $("title").textContent = "Production not found";
    $("progress").hidden = false;
    $("progressText").textContent = "This link doesn't match any clips production.";
    $("progress").querySelector(".spinner").style.display = "none";
    return;
  }
  data.clips = data.clips || { clips: [], source: {} };
  seedSelection();
  renderHeader();
  renderProgress();
  if (clips().length) {
    ensureSource();
    const sig = JSON.stringify(clips().map((c) => [c.id, c.start, c.end, c.status, c.renders]));
    if (sig !== lastSig || !document.querySelector(".clip")) { lastSig = sig; renderList(); }
    renderTimeline();
  }
  renderRenders();
  renderActionBar();
}

async function loop() {
  await refresh();
  const delay = data && data.job && data.job.status === "running" ? 3000 : 8000;
  setTimeout(loop, document.hidden ? Math.max(delay, 15000) : delay);
}
loop();
