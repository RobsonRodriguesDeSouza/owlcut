import { el, getJSON } from "/ui/lib.js";
import { postJSON } from "/ui/create-lib.js";
import { ICONS } from "/ui/shell.js";

const $ = (id) => document.getElementById(id);
const form = $("form");
let opts = null;
let probed = null;      // last successful probe
let probing = null;     // url being probed
let qopts = null;       // local (quick clips) options

// the engine picker sits above (outside) the <form>, so query the document
const engine = () => (document.querySelector('input[name="engine"]:checked') || {}).value || "agent";
const localMode = () => (form.querySelector('input[name="qmode"]:checked') || {}).value || "best";

function setEngine(mode) {
  const local = mode === "local";
  document.querySelectorAll(".local-only").forEach((n) => { n.hidden = !local; });
  document.querySelectorAll(".agent-only").forEach((n) => { n.hidden = local; });
  $("countField").hidden = local && localMode() === "all";
  $("submit").textContent = local ? "Cut the clips locally" : "Find the best clips";
  if (local) $("claudeMissing").style.display = "none";
  try { localStorage.setItem("owlcut.clips.engine", mode); } catch { /* private mode */ }
  renderSummary();
}

$("urlIcon").innerHTML = ICONS.youtube;

// ---- local file upload (chunked, resumable) --------------------------------
const CHUNK = 16 * 1024 * 1024;
let upload = null;   // {file, id, received, status: uploading|processing|ready|error, info, abort}

const sourceMode = () => (form.querySelector('input[name="source"]:checked') || {}).value || "youtube";

function fmtBytes(n) {
  if (!Number.isFinite(n)) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i >= 3 ? 2 : i ? 1 : 0)} ${u[i]}`;
}

function paintUpload() {
  const box = $("upload");
  if (!upload) { box.hidden = true; $("dropzone").hidden = false; return; }
  box.hidden = false;
  $("dropzone").hidden = true;
  const pct = upload.file.size ? Math.floor((upload.received / upload.file.size) * 100) : 0;
  $("uBar").style.width = `${upload.status === "ready" ? 100 : pct}%`;
  $("uBarWrap").setAttribute("aria-valuenow", String(upload.status === "ready" ? 100 : pct));
  box.classList.toggle("done", upload.status === "ready");
  box.classList.toggle("failed", upload.status === "error");
  $("uName").textContent = upload.file.name;
  const info = upload.info;
  $("uMeta").textContent = info
    ? `${fmtBytes(upload.file.size)} · ${info.width}×${info.height} · ${info.has_audio ? "with audio" : "NO audio track"}`
    : fmtBytes(upload.file.size);
  if (info) $("uDur").textContent = fmt(info.duration);
  let txt = "";
  if (upload.status === "uploading") {
    const rate = upload.rate ? ` · ${fmtBytes(upload.rate)}/s` : "";
    const eta = upload.rate ? ` · ~${fmt((upload.file.size - upload.received) / upload.rate)} left` : "";
    txt = `${pct}% · ${fmtBytes(upload.received)} of ${fmtBytes(upload.file.size)}${rate}${eta}`;
  } else if (upload.status === "processing") txt = "Checking the video…";
  else if (upload.status === "ready") txt = "Ready ✓";
  else if (upload.status === "error") txt = "Upload stopped";
  $("uTxt").textContent = txt;
  $("uCancel").textContent = upload.status === "error" ? "Retry" : "Remove";
}

async function putChunk(id, offset, blob, signal) {
  const res = await fetch(`/api/uploads/${id}?offset=${offset}`, {
    method: "PUT", body: blob, signal,
    headers: { "Content-Type": "application/octet-stream", "X-OwlCut-Upload": "1" },
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) { const e = new Error((data && data.detail) || `HTTP ${res.status}`); e.status = res.status; throw e; }
  return data;
}

async function runUpload(u) {
  u.status = "uploading";
  u.abort = new AbortController();
  $("uploadError").textContent = "";
  paintUpload();
  try {
    if (!u.id) {
      const r = await postJSON("/api/uploads", { name: u.file.name, size: u.file.size });
      u.id = r.id;
      u.received = 0;
    } else {
      const st = await getJSON(`/api/uploads/${u.id}`);  // resume where the server stopped
      u.received = st.received;
    }
    let t0 = performance.now(), b0 = u.received;
    while (u.received < u.file.size) {
      if (upload !== u) return;
      const end = Math.min(u.received + CHUNK, u.file.size);
      let tries = 0;
      for (;;) {
        try {
          const r = await putChunk(u.id, u.received, u.file.slice(u.received, end), u.abort.signal);
          u.received = r.received;
          break;
        } catch (e) {
          if (e.name === "AbortError") throw e;
          if (e.status === 409) { u.received = (await getJSON(`/api/uploads/${u.id}`)).received; break; }
          if (e.status && e.status < 500) throw e;
          if (++tries >= 5) throw e;
          await new Promise((r) => setTimeout(r, 1000 * tries));
        }
      }
      const dt = (performance.now() - t0) / 1000;
      if (dt > 1) { u.rate = (u.received - b0) / dt; if (dt > 8) { t0 = performance.now(); b0 = u.received; } }
      paintUpload();
    }
    u.status = "processing";
    paintUpload();
    const done = await postJSON(`/api/uploads/${u.id}/finish`, {});
    if (upload !== u) return;
    u.info = done.info;
    u.status = "ready";
    $("focusFields").hidden = false;
    $("chaptersField").hidden = true;
  } catch (e) {
    if (e.name === "AbortError" || upload !== u) return;
    u.status = "error";
    $("uploadError").textContent = `${e.message}${e.status && e.status < 500 ? "" : " — click Retry to continue from where it stopped."}`;
    if (e.status === 400 || e.status === 404) u.id = null;
  } finally {
    paintUpload();
    renderSummary();
  }
}

function pickFile(file) {
  if (!file) return;
  if (upload) discardUpload();
  upload = { file, id: null, received: 0, status: "uploading", info: null };
  const v = $("uThumb");
  if (v.dataset.url) URL.revokeObjectURL(v.dataset.url);
  v.dataset.url = URL.createObjectURL(file);
  v.src = v.dataset.url;
  v.addEventListener("loadedmetadata", () => { try { v.currentTime = Math.min(5, v.duration / 3); } catch { /* ignore */ } }, { once: true });
  $("uDur").textContent = "";
  runUpload(upload);
  renderSummary();
}

function discardUpload() {
  const u = upload;
  upload = null;
  if (u) {
    if (u.abort) u.abort.abort();
    if (u.id) postJSON(`/api/uploads/${u.id}/cancel`, {}).catch(() => {});
  }
  $("file").value = "";
  $("uploadError").textContent = "";
  paintUpload();
  renderSummary();
}

function setSource(mode) {
  $("filePane").hidden = mode !== "file";
  $("youtubePane").hidden = mode === "file";
  $("focusFields").hidden = mode === "file" ? !(upload && upload.status === "ready") : !probed;
  if (mode === "file") $("chaptersField").hidden = true;
  else $("chaptersField").hidden = !(probed && probed.chapters && probed.chapters.length);
  renderSummary();
}

$("file").addEventListener("change", (e) => pickFile(e.target.files[0]));
$("uCancel").addEventListener("click", () => {
  if (upload && upload.status === "error") runUpload(upload);
  else discardUpload();
});
const dz = $("dropzone");
dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
dz.addEventListener("dragleave", () => dz.classList.remove("over"));
dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("over"); pickFile(e.dataTransfer.files[0]); });
window.addEventListener("beforeunload", (e) => {
  if (upload && (upload.status === "uploading" || upload.status === "processing")) { e.preventDefault(); e.returnValue = ""; }
});

function fmt(s) {
  if (s == null || !Number.isFinite(+s)) return "";
  s = Math.round(+s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}

function seg(container, name, items, type = "radio", checked = []) {
  container.innerHTML = "";
  for (const it of items) {
    const input = el("input", { type, name, value: String(it.value) });
    if (checked.includes(String(it.value))) input.checked = true;
    container.append(el("label", {}, input, it.label, it.hint ? el("small", {}, it.hint) : null));
  }
}

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function styleGrid(container, name, items, value) {
  container.innerHTML = "";
  for (const it of items) {
    const input = el("input", { type: "radio", name, value: it.value });
    if (it.value === value) input.checked = true;
    const frame = el("span", { class: "style-frame" });
    if (it.preview) {
      const v = el("video", { src: it.preview, muted: "", loop: "", playsinline: "", preload: "metadata", "aria-hidden": "true" });
      v.muted = true;
      if (!reduceMotion) { v.autoplay = true; v.addEventListener("canplay", () => v.play().catch(() => {}), { once: true }); }
      frame.append(v);
    } else {
      frame.append(el("span", { class: "style-none" }, "—"));
    }
    container.append(el("label", { class: "style-card" }, input, frame,
      el("span", { class: "style-txt" }, el("b", {}, it.label), el("small", {}, it.desc))));
  }
}

function fillSelect(sel, items, value) {
  sel.innerHTML = "";
  for (const it of items) sel.append(el("option", { value: it.value }, it.label));
  if (value != null) sel.value = value;
}

function values() {
  const fd = new FormData(form);
  return {
    kind: "clips",
    source: sourceMode(),
    url: sourceMode() === "file" ? "" : (fd.get("url") || "").trim(),
    upload_id: sourceMode() === "file" && upload && upload.status === "ready" ? upload.id : undefined,
    focus_start: (fd.get("focus_start") || "").trim(),
    focus_end: (fd.get("focus_end") || "").trim(),
    count: Number(fd.get("count") || 5),
    length: fd.get("length") || "medium",
    platforms: fd.getAll("platforms"),
    language: fd.get("language"),
    approvals: fd.get("approvals"),
    caption: fd.get("caption") || "highlight",
    intro: fd.get("intro") || "hook",
    outro: fd.get("outro") || "none",
    cta_text: (fd.get("cta_text") || "").trim(),
    accent: fd.get("accent") || "#ff2d55",
    notes: (fd.get("notes") || "").trim(),
  };
}

function fd(name) { return (form.querySelector(`input[name="${name}"]:checked`) || {}).value; }

function renderSummary() {
  if (!opts) return;
  const v = values();
  const lab = (list, val) => (list.find((x) => String(x.value) === String(val)) || {}).label || "—";
  const plats = v.platforms.map((p) => lab(opts.platforms, p)).join(", ") || "none";
  const local = engine() === "local";
  const qlab = (list, val) => ((qopts && qopts[list]) || []).find((x) => x.value === val)?.label?.split(" (")[0] || "—";
  const rows = [
    ["Engine", local ? "Local" : "AI agent"],
    ["Video", v.source === "file"
      ? (upload ? `${upload.file.name.slice(0, 40)}${upload.file.name.length > 40 ? "…" : ""}` : "—")
      : (probed ? `${probed.title.slice(0, 40)}${probed.title.length > 40 ? "…" : ""}` : "—")],
    ["Range", v.focus_start ? `${v.focus_start}${v.focus_end ? " → " + v.focus_end : " onwards"}` : "whole video"],
    ["Clips", local && localMode() === "all" ? "whole video" : String(v.count)],
    ["Length", lab(opts.lengths, v.length).replace(/^.*· /, "")],
    ...(local
      ? [["Framing", qlab("framings", fd("framing"))], ["Captions", qlab("captions", fd("qcap"))], ["Format", "9:16"]]
      : [["Platforms", plats], ["Captions", lab(opts.styles.captions, v.caption)], ["Intro", lab(opts.styles.intros, v.intro)]]),
  ];
  const box = $("summary");
  box.innerHTML = "";
  for (const [k, val] of rows) box.append(el("div", {}, el("span", {}, k), el("span", {}, val)));
}

async function doProbe() {
  const url = $("url").value.trim();
  $("probeError").textContent = "";
  if (!url) return;
  if (probing === url || (probed && probed.input === url)) return;
  probing = url;
  $("probeBtn").disabled = true;
  $("probeBtn").textContent = "Reading…";
  try {
    const info = await postJSON("/api/clips/probe", { url });
    if ($("url").value.trim() !== url) return;  // user changed it meanwhile
    probed = { ...info, input: url };
    $("pThumb").src = info.thumbnail || "";
    $("pDur").textContent = info.duration_text || "";
    $("pTitle").textContent = info.title || info.video_id;
    $("pChannel").textContent = info.channel || "";
    const tags = $("pTags");
    tags.innerHTML = "";
    const cap = info.captions || {};
    tags.append(el("span", { class: "chip" }, cap.manual && cap.manual.length ? `Captions: ${cap.manual.slice(0, 4).join(", ")}` : (cap.auto_count ? "Auto-generated captions" : "No captions — local transcription")));
    if (info.chapters && info.chapters.length) tags.append(el("span", { class: "chip" }, `${info.chapters.length} chapters`));
    if (info.start != null) tags.append(el("span", { class: "chip accent" }, `Starts at ${fmt(info.start)}`));
    $("probe").hidden = false;
    $("focusFields").hidden = false;
    if (info.start != null && !$("focus_start").value) $("focus_start").value = fmt(info.start);
    const ch = $("chapters");
    ch.innerHTML = "";
    for (const c of info.chapters || []) {
      ch.append(el("button", {
        type: "button", class: "chapter",
        onclick: () => { $("focus_start").value = fmt(c.start); $("focus_end").value = fmt(c.end); renderSummary(); },
      }, el("time", {}, fmt(c.start)), c.title));
    }
    $("chaptersField").hidden = !(info.chapters && info.chapters.length);
  } catch (e) {
    probed = null;
    $("probe").hidden = true;
    $("probeError").textContent = e.message;
  } finally {
    probing = null;
    $("probeBtn").disabled = false;
    $("probeBtn").textContent = "Read video";
    renderSummary();
  }
}

async function init() {
  const [o, base] = await Promise.all([getJSON("/api/clips/options"), getJSON("/api/options").catch(() => ({}))]);
  opts = o;
  if (base && base.claude_available === false) $("claudeMissing").style.display = "block";
  if (base && base.agent_model) {
    const pretty = base.agent_model.replace(/^claude-/, "").replace(/-(\d+)-(\d+)$/, " $1.$2").replace(/^\w/, (c) => c.toUpperCase());
    $("agentModel").textContent = `Agent model: ${base.agent_model === "default" ? "Claude Code default" : pretty}. Rendering runs on your Mac; the agent's steps count toward your Claude plan usage.`;
    $("agentModel").hidden = false;
  }
  seg($("countSeg"), "count", o.counts.map((n) => ({ value: n, label: `${n} clips` })), "radio", ["5"]);
  seg($("lengthSeg"), "length", o.lengths.map((l) => {
    const [name, range] = l.label.split(" · ");
    return { value: l.value, label: name, hint: range };
  }), "radio", ["medium"]);
  seg($("platformSeg"), "platforms", o.platforms.map((p) => ({ value: p.value, label: p.label, hint: p.aspect })), "checkbox", ["tiktok", "reels", "shorts"]);
  styleGrid($("captionGrid"), "caption", o.styles.captions, "highlight");
  styleGrid($("introGrid"), "intro", o.styles.intros, "hook");
  seg($("outroSeg"), "outro", o.styles.outros.map((x) => ({ value: x.value, label: x.label })), "radio", ["none"]);
  const sw = $("accentSeg");
  for (const a of o.styles.accents) {
    const input = el("input", { type: "radio", name: "accent", value: a.value, "aria-label": a.label });
    if (a.value === "#ff2d55") input.checked = true;
    sw.append(el("label", { class: "swatch", title: a.label, style: `--sw:${a.value}` }, input));
  }
  form.addEventListener("change", (e) => {
    if (e.target.name === "outro") $("ctaField").hidden = e.target.value !== "cta";
    if (e.target.name === "source") setSource(e.target.value);
  });
  try {
    qopts = await getJSON("/api/quickclips/options");
    seg($("modeSeg"), "qmode", [
      { value: "best", label: "Best moments", hint: "the N strongest" },
      { value: "all", label: "Whole video", hint: "split into parts" },
    ], "radio", ["best"]);
    seg($("framingSeg"), "framing", qopts.framings.map((f) => {
      const [name, hint] = f.label.split(" (");
      return { value: f.value, label: name, hint: hint ? hint.replace(/\)$/, "") : "" };
    }), "radio", ["auto"]);
    seg($("qcapSeg"), "qcap", qopts.captions.map((c) => ({ value: c.value, label: c.label })), "radio", ["highlight"]);
    for (const a of qopts.accents) {
      const input = el("input", { type: "radio", name: "qaccent", value: a, "aria-label": a });
      if (a === qopts.accents[0]) input.checked = true;
      $("qaccentSeg").append(el("label", { class: "swatch", title: a, style: `--sw:${a}` }, input));
    }
    const help = () => {
      $("modeHelp").textContent = localMode() === "all"
        ? `The whole video becomes consecutive clips (up to ${qopts.max_all}). A 1-hour video at 30–60 s makes about 80 clips.`
        : "Picks the strongest stretches by speaking pace, loudness and few pauses, always on whole sentences.";
      $("countField").hidden = engine() === "local" && localMode() === "all";
    };
    form.addEventListener("change", (e) => { if (e.target.name === "qmode") help(); });
    help();
  } catch { /* local engine unavailable: keep the agent only */ }
  document.querySelector(".engine-pick").addEventListener("change", (e) => { if (e.target.name === "engine") setEngine(e.target.value); });
  fillSelect($("language"), o.languages, "original");
  fillSelect($("approvals"), o.approvals, "gates");

  const params = new URLSearchParams(location.search);
  let startEngine = params.get("engine");
  if (!startEngine) { try { startEngine = localStorage.getItem("owlcut.clips.engine"); } catch { /* ignore */ } }
  if (startEngine === "local" && qopts) {
    document.querySelector('input[name="engine"][value="local"]').checked = true;
    setEngine("local");
  }
  if (params.get("source") === "file") { form.querySelector('input[name="source"][value="file"]').checked = true; setSource("file"); }
  if (params.get("url")) { $("url").value = params.get("url"); doProbe(); }
  renderSummary();
}

$("probeBtn").addEventListener("click", doProbe);
$("url").addEventListener("paste", () => setTimeout(doProbe, 0));
$("url").addEventListener("change", doProbe);
$("url").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); doProbe(); } });
form.addEventListener("input", renderSummary);
form.addEventListener("change", renderSummary);

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const v = values();
  $("error").textContent = "";
  if (v.source === "file") {
    if (!upload) { $("error").textContent = "Pick the video file first."; return; }
    if (upload.status !== "ready") {
      $("error").textContent = upload.status === "error" ? "The upload stopped — click Retry." : "Wait for the upload to reach 100%.";
      return;
    }
  } else if (!v.url) { $("error").textContent = "Paste the video link."; $("url").focus(); return; }
  const btn = $("submit");
  if (engine() === "local") {
    btn.disabled = true;
    btn.textContent = "Starting…";
    try {
      const res = await postJSON("/api/quickclips", {
        upload_id: v.upload_id, url: v.url, source_title: probed ? probed.title : "",
        length: v.length, mode: localMode(), count: v.count, focus_start: v.focus_start, focus_end: v.focus_end,
        framing: fd("framing"), captions: fd("qcap"), accent: fd("qaccent"),
      });
      if (upload) upload.status = "claimed";
      location.href = `/clips/quick/${res.id}`;
    } catch (err) {
      $("error").textContent = err.message;
      btn.disabled = false;
      btn.textContent = "Cut the clips locally";
    }
    return;
  }
  if (!v.platforms.length) { $("error").textContent = "Pick at least one platform."; return; }
  btn.disabled = true;
  btn.textContent = "Sending to the agent…";
  try {
    const res = await postJSON("/api/jobs", v);
    if (upload) upload.status = "claimed";  // no beforeunload prompt, no cancel
    location.href = `/clips/${res.id}`;
  } catch (err) {
    $("error").textContent = err.message;
    btn.disabled = false;
    btn.textContent = "Find the best clips";
  }
});

init().catch((e) => { $("error").textContent = `Couldn't load the options: ${e.message}`; });
