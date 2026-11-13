import { el, getJSON } from "/ui/lib.js";
import { postJSON } from "/ui/create-lib.js";
import { ICONS } from "/ui/shell.js";

const $ = (id) => document.getElementById(id);
const form = $("form");
let opts = null;
let probed = null;      // last successful probe
let probing = null;     // url being probed

$("urlIcon").innerHTML = ICONS.youtube;

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
    url: (fd.get("url") || "").trim(),
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

function renderSummary() {
  if (!opts) return;
  const v = values();
  const lab = (list, val) => (list.find((x) => String(x.value) === String(val)) || {}).label || "—";
  const plats = v.platforms.map((p) => lab(opts.platforms, p)).join(", ") || "none";
  const rows = [
    ["Video", probed ? `${probed.title.slice(0, 40)}${probed.title.length > 40 ? "…" : ""}` : "—"],
    ["Range", v.focus_start ? `${v.focus_start}${v.focus_end ? " → " + v.focus_end : " onwards"}` : "whole video"],
    ["Clips", String(v.count)],
    ["Length", lab(opts.lengths, v.length).replace(/^.*· /, "")],
    ["Platforms", plats],
    ["Captions", lab(opts.styles.captions, v.caption)],
    ["Intro", lab(opts.styles.intros, v.intro)],
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
  form.addEventListener("change", (e) => { if (e.target.name === "outro") $("ctaField").hidden = e.target.value !== "cta"; });
  fillSelect($("language"), o.languages, "original");
  fillSelect($("approvals"), o.approvals, "gates");

  const params = new URLSearchParams(location.search);
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
  if (!v.url) { $("error").textContent = "Paste the video link."; $("url").focus(); return; }
  if (!v.platforms.length) { $("error").textContent = "Pick at least one platform."; return; }
  const btn = $("submit");
  btn.disabled = true;
  btn.textContent = "Sending to the agent…";
  try {
    const res = await postJSON("/api/jobs", v);
    location.href = `/clips/${res.id}`;
  } catch (err) {
    $("error").textContent = err.message;
    btn.disabled = false;
    btn.textContent = "Find the best clips";
  }
});

init().catch((e) => { $("error").textContent = `Couldn't load the options: ${e.message}`; });
