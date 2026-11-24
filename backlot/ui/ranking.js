// Ranking videos — list + editor. Everything renders locally (no agent).
import { el, getJSON, fmtAgo } from "/ui/lib.js";
import { postJSON } from "/ui/create-lib.js";
import { ICONS } from "/ui/shell.js";

const $ = (id) => document.getElementById(id);
const rid = (location.pathname.match(/^\/ranking\/([a-z0-9-]+)\/?$/) || [])[1] || null;

// ---------------------------------------------------------------- helpers

function fmt(s, decimals = true) {
  if (s == null || !Number.isFinite(+s)) return "";
  s = Math.max(0, +s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const ss = decimals ? sec.toFixed(1).padStart(4, "0") : String(Math.floor(sec)).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

function parseTime(text) {
  text = String(text || "").trim().replace(",", ".");
  if (!text) return null;
  const parts = text.split(":");
  if (parts.length > 3 || parts.some((p) => p === "" || isNaN(+p) || +p < 0)) return NaN;
  return parts.reduce((t, p) => t * 60 + +p, 0);
}

// ---------------------------------------------------------------- list view

async function showList() {
  $("listView").hidden = false;
  const list = $("list");
  const rows = await getJSON("/api/rankings").catch(() => []);
  list.innerHTML = "";
  if (!rows.length) {
    list.append(el("p", { class: "rk-empty" }, "No rankings yet. Give the first one a title above."));
  }
  for (const r of rows) {
    const status = r.status === "rendering" ? "Rendering…" : r.has_video ? "Video ready" : r.status === "error" ? "Render failed" : "Draft";
    list.append(el("a", { class: "rk-card", href: `/ranking/${r.id}` },
      r.thumbnail ? el("img", { class: "rk-card-thumb", src: r.thumbnail, alt: "", loading: "lazy" }) : el("span", { class: "rk-card-thumb" }),
      el("span", { class: "rk-card-body" },
        el("b", {}, r.title || "Untitled ranking"),
        el("small", {}, `${r.entries} video${r.entries === 1 ? "" : "s"} · ${status} · ${fmtAgo(r.updated_at)}`))));
  }
  $("newForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("newError").textContent = "";
    try {
      const doc = await postJSON("/api/rankings", { title: $("newTitle").value });
      location.href = `/ranking/${doc.id}`;
    } catch (err) { $("newError").textContent = err.message; }
  });
}

// ---------------------------------------------------------------- editor

let doc = null;
let opts = null;
let active = -1;          // slot index with the open player
let player = null;
let playerReady = false;
let stopTimer = null;
let saveTimer = null;
let pollTimer = null;

const locked = () => doc && doc.status === "rendering";

function loadYT() {
  if (window.YT && window.YT.Player) return Promise.resolve();
  if (loadYT.p) return loadYT.p;
  loadYT.p = new Promise((resolve) => {
    window.onYouTubeIframeAPIReady = resolve;
    document.head.append(el("script", { src: "https://www.youtube.com/iframe_api" }));
  });
  return loadYT.p;
}

function scheduleSave() {
  $("saved").textContent = "Saving…";
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, 600);
}

async function save() {
  clearTimeout(saveTimer);
  try {
    const fresh = await postJSON(`/api/rankings/${rid}`, {
      title: doc.title, accent: doc.accent, order: doc.order, entries: doc.entries,
    });
    doc.problems = fresh.problems;
    doc.output = fresh.output;
    doc.status = fresh.status;
    $("saved").textContent = "Saved";
    renderSide();
  } catch (err) {
    $("saved").textContent = `Not saved: ${err.message}`;
  }
}

function setAccent() {
  document.documentElement.style.setProperty("--rk-accent", doc.accent);
}

function renderHeader() {
  $("eTitle").textContent = doc.title || "Untitled ranking";
  document.title = `OwlCut — ${doc.title || "Ranking"}`;
  $("title").value = doc.title || "";
  const orderSeg = $("orderSeg");
  orderSeg.innerHTML = "";
  for (const o of opts.orders) {
    const input = el("input", { type: "radio", name: "order", value: o.value });
    input.checked = o.value === doc.order;
    orderSeg.append(el("label", {}, input, o.label.replace("#6", `#${filled().length || 6}`)));
  }
  const sw = $("accentSeg");
  sw.innerHTML = "";
  for (const a of opts.accents) {
    const input = el("input", { type: "radio", name: "accent", value: a, "aria-label": a });
    input.checked = a === doc.accent;
    sw.append(el("label", { class: "swatch", title: a, style: `--sw:${a}` }, input));
  }
  setAccent();
}

const filled = () => doc.entries.filter((e) => e.video_id);

function cutLength(e) {
  return e.start != null && e.end != null ? e.end - e.start : null;
}

function renderSlots() {
  const ol = $("slots");
  ol.innerHTML = "";
  doc.entries.forEach((e, i) => ol.append(slotEl(e, i)));
  $("addSlot").hidden = doc.entries.length >= opts.max_entries;
  $("addSlot").disabled = locked();
}

function slotEl(e, i) {
  const li = el("li", { class: `rk-slot${i === active ? " active" : ""}`, "data-i": i });
  const up = el("button", { type: "button", title: "Move up", "aria-label": `Move #${i + 1} up` }, "▲");
  const down = el("button", { type: "button", title: "Move down", "aria-label": `Move #${i + 1} down` }, "▼");
  up.disabled = i === 0 || locked();
  down.disabled = i === doc.entries.length - 1 || locked();
  up.onclick = () => move(i, -1);
  down.onclick = () => move(i, 1);
  li.append(el("div", { class: "rk-num" }, el("b", {}, `#${i + 1}`), el("div", { class: "rk-move" }, up, down)));

  const body = el("div", { class: "rk-body" });
  const url = el("input", { type: "url", inputmode: "url", autocomplete: "off", value: e.url || "",
    placeholder: "https://www.youtube.com/watch?v=…", "aria-label": `YouTube link for #${i + 1}` });
  url.disabled = locked();
  const load = el("button", { class: "btn", type: "button" }, e.video_id ? "Reload" : "Load");
  load.disabled = locked();
  const err = el("div", { class: "rk-slot-error", role: "alert" });
  const doLoad = () => probe(i, url.value, load, err);
  load.onclick = doLoad;
  url.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); doLoad(); } });
  url.addEventListener("paste", () => setTimeout(doLoad, 0));
  const icon = el("span", { class: "url-ic", "aria-hidden": "true" });
  icon.innerHTML = ICONS.youtube;
  body.append(el("div", { class: "url-row" }, el("div", { class: "url-input" }, icon, url), load), err);

  if (e.video_id) {
    const remove = el("button", { class: "rk-icon", type: "button", title: "Clear this slot", "aria-label": `Clear #${i + 1}` }, "✕");
    remove.disabled = locked();
    remove.onclick = () => clearSlot(i);
    body.append(el("div", { class: "rk-meta" },
      el("img", { src: e.thumbnail || `https://i.ytimg.com/vi/${e.video_id}/mqdefault.jpg`, alt: "", onerror: (ev) => { ev.target.style.visibility = "hidden"; } }),
      el("div", {}, el("b", {}, e.video_title || e.video_id), el("small", {}, [e.channel, e.duration ? fmt(e.duration, false) : ""].filter(Boolean).join(" · "))),
      remove));

    const name = el("input", { type: "text", maxlength: "48", value: e.name || "", placeholder: "Shown under the #number" });
    name.disabled = locked();
    name.addEventListener("input", () => { e.name = name.value; renderSide(); scheduleSave(); });
    name.addEventListener("focus", () => { previewSlot = i; renderPhone(); });
    body.append(el("div", { class: "field" }, el("label", {}, `Name for #${i + 1}`), name));

    if (i === active) body.append(el("div", { class: "rk-player" }, el("div", { id: "ytPlayer" })));

    const cut = el("div", { class: "rk-cut" });
    cut.append(timeField(e, i, "start", "Start"), timeField(e, i, "end", "End"));
    const info = el("div", { class: "rk-cut-info" });
    const len = cutLength(e);
    if (len != null) {
      const bad = len < 1 || len > opts.max_cut;
      info.append(el("span", { class: bad ? "bad" : "ok" }, `Cut: ${len.toFixed(1)}s${bad ? ` (1–${opts.max_cut}s)` : ""}`));
    } else {
      info.append(el("span", {}, "Play the video and mark the start and end."));
    }
    const watch = el("button", { class: "btn small", type: "button" }, i === active ? "▶ Play the cut" : "Open player");
    watch.disabled = locked();
    watch.onclick = () => (i === active ? playCut(e) : openPlayer(i));
    info.append(watch);
    cut.append(info);
    body.append(cut);
  }
  li.append(body);
  return li;
}

function timeField(e, i, key, label) {
  const input = el("input", { type: "text", inputmode: "decimal", value: fmt(e[key]), placeholder: key === "start" ? "0:00.0" : "0:15.0",
    "aria-label": `${label} of #${i + 1}` });
  input.disabled = locked();
  input.addEventListener("change", () => {
    const v = parseTime(input.value);
    if (Number.isNaN(v)) { input.value = fmt(e[key]); return; }
    e[key] = v == null ? null : Math.round(v * 100) / 100;
    if (key === "start" && e.start != null && (e.end == null || e.end <= e.start)) e.end = Math.min(e.start + 10, e.duration || e.start + 10);
    renderSlots(); renderSide(); scheduleSave();
  });
  const mark = el("button", { class: "btn small", type: "button", title: "Use the player's current time" }, "Mark now");
  mark.disabled = locked() || i !== active;
  mark.onclick = () => {
    if (!player || !playerReady) return;
    const t = Math.round(player.getCurrentTime() * 10) / 10;
    e[key] = t;
    if (key === "start" && (e.end == null || e.end <= t)) e.end = Math.min(t + 10, e.duration || t + 10);
    if (key === "end" && (e.start == null || e.start >= t)) e.start = Math.max(0, t - 10);
    renderSlots(); renderSide(); scheduleSave();
  };
  return el("div", { class: "field" }, el("label", {}, label), el("div", { class: "rk-time" }, input, mark));
}

async function probe(i, url, btn, err) {
  url = url.trim();
  err.textContent = "";
  if (!url) return;
  const e = doc.entries[i];
  if (e.url === url && e.video_id && btn.textContent !== "Reload") return;
  btn.disabled = true;
  btn.textContent = "Reading…";
  try {
    const p = await postJSON("/api/clips/probe", { url });
    const changed = e.video_id !== p.video_id;
    Object.assign(e, {
      url: p.url, video_id: p.video_id, video_title: p.title, channel: p.channel,
      thumbnail: p.thumbnail, duration: p.duration,
    });
    if (changed) {
      e.start = p.start != null ? p.start : 0;
      e.end = Math.min(e.start + 10, p.duration || e.start + 10);
      if (!e.name) e.name = (p.title || "").slice(0, 48);
    }
    previewSlot = i;
    await save();
    renderHeader();
    openPlayer(i);
  } catch (ex) {
    err.textContent = ex.message;
    btn.disabled = false;
    btn.textContent = "Load";
  }
}

function destroyPlayer() {
  clearInterval(stopTimer);
  if (player) { try { player.destroy(); } catch { /* gone */ } }
  player = null;
  playerReady = false;
}

async function openPlayer(i) {
  destroyPlayer();
  active = i;
  previewSlot = i;
  renderSlots();
  renderPhone();
  const e = doc.entries[i];
  if (!e || !e.video_id) return;
  await loadYT();
  if (active !== i || !$("ytPlayer")) return;
  player = new YT.Player("ytPlayer", {
    videoId: e.video_id,
    playerVars: { start: Math.floor(e.start || 0), rel: 0, modestbranding: 1, playsinline: 1 },
    events: {
      onReady: () => { playerReady = true; $("slots").querySelectorAll(".rk-time .btn").forEach((b) => (b.disabled = locked())); },
      onError: (ev) => {
        const box = document.querySelector(`.rk-slot[data-i="${i}"] .rk-slot-error`);
        if (box) box.textContent = [101, 150].includes(ev.data)
          ? "This video can't be played embedded. Type the start and end times by hand — the render still works."
          : "The player failed to load this video.";
      },
    },
  });
  $("slots").children[i]?.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function playCut(e) {
  if (!player || !playerReady || e.start == null) return;
  clearInterval(stopTimer);
  player.seekTo(e.start, true);
  player.playVideo();
  stopTimer = setInterval(() => {
    if (!player || !playerReady) return clearInterval(stopTimer);
    if (e.end != null && player.getCurrentTime() >= e.end) { player.pauseVideo(); clearInterval(stopTimer); }
  }, 100);
}

function move(i, dir) {
  const j = i + dir;
  if (j < 0 || j >= doc.entries.length) return;
  [doc.entries[i], doc.entries[j]] = [doc.entries[j], doc.entries[i]];
  destroyPlayer();
  active = -1;
  previewSlot = j;
  renderSlots(); renderSide(); scheduleSave();
}

function clearSlot(i) {
  if (doc.entries.length > 6) doc.entries.splice(i, 1);
  else doc.entries[i] = { url: "", video_id: "", name: "", video_title: "", channel: "", thumbnail: "", duration: 0, start: null, end: null };
  if (active === i) { destroyPlayer(); active = -1; }
  renderSlots(); renderHeader(); renderSide(); scheduleSave();
}

// ---------------------------------------------------------------- side panel

let previewSlot = 0;

function renderPhone() {
  $("phoneHead").textContent = doc.title || "Your header title";
  const e = doc.entries[previewSlot] && doc.entries[previewSlot].video_id ? doc.entries[previewSlot] : filled()[0];
  const idx = e ? doc.entries.indexOf(e) : 0;
  const thumb = e ? (e.thumbnail || `https://i.ytimg.com/vi/${e.video_id}/mqdefault.jpg`) : "";
  $("phone").style.backgroundImage = thumb ? `url("${thumb}")` : "";
  $("phoneVideo").style.backgroundImage = thumb ? `url("${thumb}")` : "";
  $("phoneRank").textContent = `#${idx + 1}`;
  $("phoneName").textContent = e ? e.name || "" : "Name of the item";
}

function renderSide() {
  renderPhone();
  const f = filled();
  const total = f.reduce((s, e) => s + Math.max(0, cutLength(e) || 0), 0);
  const order = opts.orders.find((o) => o.value === doc.order);
  $("summary").innerHTML = "";
  for (const [k, v] of [
    ["Videos", `${f.length}`],
    ["Total length", total ? fmt(total, false) : "—"],
    ["Order", order ? order.label.replace("#6", `#${f.length || 6}`) : ""],
    ["Format", "9:16 · 1080×1920"],
  ]) $("summary").append(el("div", {}, el("span", {}, k), el("span", {}, v)));

  const problems = doc.problems || [];
  $("problems").innerHTML = "";
  for (const p of problems) $("problems").append(el("li", {}, p));

  const rendering = locked();
  $("render").disabled = rendering || problems.length > 0 || !opts.ffmpeg;
  $("render").textContent = rendering ? "Rendering…" : doc.output ? "Render again" : "Render video";
  if (!opts.ffmpeg) $("renderError").textContent = "ffmpeg was not found on this machine.";
  else if (doc.status === "error" && doc.error) $("renderError").textContent = doc.error;
  else $("renderError").textContent = "";

  const pr = doc.progress;
  $("progress").hidden = !rendering;
  if (rendering && pr) {
    $("bar").style.width = `${Math.round(((pr.done || 0) / Math.max(1, pr.total || 1)) * 100)}%`;
    $("progressMsg").textContent = pr.message || "Working…";
  }

  $("output").hidden = !doc.output || rendering;
  if (doc.output) {
    const v = $("outVideo");
    if (v.dataset.src !== doc.output.url) { v.src = doc.output.url; v.dataset.src = doc.output.url; }
    $("download").href = `/api/rankings/${rid}/video?download=1`;
    $("download").textContent = `Download MP4 · ${(doc.output.size / 1e6).toFixed(1)} MB`;
  }
}

async function poll() {
  clearTimeout(pollTimer);
  try {
    const fresh = await getJSON(`/api/rankings/${rid}`);
    const was = doc.status;
    Object.assign(doc, { status: fresh.status, progress: fresh.progress, error: fresh.error, output: fresh.output, problems: fresh.problems });
    if (was === "rendering" && fresh.status !== "rendering") { renderSlots(); }
  } catch { /* keep polling */ }
  renderSide();
  if (doc.status === "rendering") pollTimer = setTimeout(poll, 1500);
}

async function startRender() {
  $("renderError").textContent = "";
  await save();
  try {
    const fresh = await postJSON(`/api/rankings/${rid}/render`, {});
    Object.assign(doc, fresh);
    destroyPlayer();
    active = -1;
    renderSlots(); renderSide();
    pollTimer = setTimeout(poll, 1000);
  } catch (err) {
    $("renderError").textContent = err.message;
  }
}

async function showEditor() {
  $("editView").hidden = false;
  try {
    [opts, doc] = await Promise.all([getJSON("/api/rankings/options"), getJSON(`/api/rankings/${rid}`)]);
  } catch {
    $("editView").innerHTML = '<p class="rk-empty">This ranking was not found. <a href="/ranking">Back to all rankings</a></p>';
    return;
  }
  previewSlot = Math.max(0, doc.entries.findIndex((e) => e.video_id));
  renderHeader();
  renderSlots();
  renderSide();

  $("title").addEventListener("input", () => {
    doc.title = $("title").value;
    $("eTitle").textContent = doc.title || "Untitled ranking";
    renderPhone(); scheduleSave();
  });
  $("orderSeg").addEventListener("change", (ev) => { doc.order = ev.target.value; renderSide(); scheduleSave(); });
  $("accentSeg").addEventListener("change", (ev) => { doc.accent = ev.target.value; setAccent(); scheduleSave(); });
  $("addSlot").onclick = () => {
    if (doc.entries.length >= opts.max_entries) return;
    doc.entries.push({ url: "", video_id: "", name: "", video_title: "", channel: "", thumbnail: "", duration: 0, start: null, end: null });
    renderSlots(); renderHeader(); scheduleSave();
  };
  $("render").onclick = startRender;
  if (doc.status === "rendering") poll();
}

if (rid) showEditor(); else showList();
