// /settings — watermark: round logo + @handle, position, size, opacity, where it applies.
import { el, getJSON } from "/ui/lib.js";

const $ = (id) => document.getElementById(id);
let cfg = null;
let opts = null;
let kind = "clips";
let frameT = 0.35;
let previewTimer = null;
let saving = false;

async function send(method, url, body, headers = {}) {
  const res = await fetch(url, { method, headers, body });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}
const putJSON = (url, obj) => send("PUT", url, JSON.stringify(obj), { "Content-Type": "application/json" });

function radios(container, name, items, value) {
  container.innerHTML = "";
  for (const [val, label] of items) {
    const input = el("input", { type: "radio", name, value: val });
    if (val === value) input.checked = true;
    container.append(el("label", {}, input, label));
  }
}
const picked = (name) => (document.querySelector(`input[name="${name}"]:checked`) || {}).value;

function paintLogo() {
  const has = cfg.has_logo;
  $("logoImg").hidden = !has;
  $("logoEmpty").hidden = has;
  $("logoDrop").classList.toggle("has", has);
  $("logoRemove").hidden = !has;
  $("logoPick").textContent = has ? "Replace image" : "Choose image";
  const ring = $("ring").checked ? 1 : 0;
  if (has) $("logoImg").src = `/api/branding/logo.png?size=264&ring=${ring}&v=${cfg.logo_version}`;
}

function paintSide() {
  const has = cfg.has_side_logo;
  $("sideImg").hidden = !has;
  $("sideEmpty").hidden = has;
  $("sideDrop").classList.toggle("has", has);
  $("sideRemove").hidden = !has;
  $("sidePick").textContent = has ? "Replace image" : "Choose image";
  if (has) $("sideImg").src = `/api/branding/side-logo.png?size=264&shape=${picked("side_shape")}&v=${cfg.side_logo_version}`;
  $("sideFields").classList.toggle("off", !has || !$("sideEnabled").checked);
}

function paintPos() {
  for (const id of ["posX", "posY", "sideX", "sideY"]) $(`${id}Val`).textContent = `${$(id).value}%`;
  const whole = picked("pos_area") === "frame";
  $("posHelp").textContent = whole
    ? "Drag and watch the preview. The logo can go anywhere on the screen — including the blurred band above and below a wide video. There, cropping the video would remove it."
    : "Drag and watch the preview. Measured on the video picture, so the logo always stays on it and cropping can't remove it.";
}

function paintPreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => {
    const img = $("preview");
    img.classList.add("loading");
    img.onload = () => img.classList.remove("loading");
    const draft = collect();
    $("nextFrame").hidden = !($("sample").value || "").startsWith("quick:");
    const sample = encodeURIComponent($("sample").value || "demo-wide");
    img.src = `/api/branding/preview.png?kind=${kind}&w=600&sample=${sample}&t=${frameT.toFixed(2)}` +
      `&v=${cfg.logo_version}-${cfg.side_logo_version}&draft=${encodeURIComponent(JSON.stringify(draft))}`;
    $("previewFrame").classList.toggle("wide", kind === "video");
    const on = draft.apply[kind];
    const empty = !cfg.has_logo && !draft.handle.replace(/[@\s]/g, "") && !(cfg.has_side_logo && draft.side_enabled);
    $("previewNote").textContent = empty ? "Add a logo, your @ or a side watermark to see it here."
      : on ? "" : `Off for ${opts.kinds[kind]} — tick it under “Apply to”.`;
  }, 120);
}

function state(text, cls = "") {
  $("saveState").textContent = text;
  $("saveState").className = `save-state ${cls}`;
}

function collect() {
  const apply = {};
  document.querySelectorAll('#applyList input').forEach((i) => { apply[i.value] = i.checked; });
  return {
    handle: $("handle").value, pos_x: +$("posX").value, pos_y: +$("posY").value, pos_area: picked("pos_area"), size: picked("size"),
    opacity: +$("opacity").value, ring: $("ring").checked, apply,
    side_enabled: $("sideEnabled").checked, side_x: +$("sideX").value, side_y: +$("sideY").value,
    side_size: picked("side_size"), side_shape: picked("side_shape"), side_opacity: +$("sideOpacity").value,
    fade_enabled: $("fadeEnabled").checked, fade_duration: +picked("fade_duration"),
  };
}

// ---- draft vs saved ------------------------------------------------------
const norm = (h) => { const x = String(h || "").replace(/\s+/g, "").replace(/^@+/, "").slice(0, 30); return x ? `@${x}` : ""; };
function savedView() {
  return JSON.stringify({ handle: cfg.handle, pos_x: cfg.pos_x, pos_y: cfg.pos_y, pos_area: cfg.pos_area, size: cfg.size,
    opacity: cfg.opacity, ring: cfg.ring, apply: cfg.apply,
    side_enabled: cfg.side_enabled, side_x: cfg.side_x, side_y: cfg.side_y,
    side_size: cfg.side_size, side_shape: cfg.side_shape, side_opacity: cfg.side_opacity,
    fade_enabled: cfg.fade_enabled, fade_duration: cfg.fade_duration });
}
function draftView() {
  const d = collect();
  return JSON.stringify({ ...d, handle: norm(d.handle) });
}
const isDirty = () => cfg && draftView() !== savedView();

function changed() {
  const dirty = isDirty();
  $("saveBtn").disabled = !dirty || saving;
  $("resetBtn").hidden = !dirty;
  if (dirty) state("Unsaved changes", "dirty");
  else if ($("saveState").classList.contains("dirty")) state("");
  paintLogo();
  paintSide();
  paintFade();
  paintPreview();
}

function paintFade() {
  const row = document.querySelector(".fade-row");
  row.classList.toggle("off", !$("fadeEnabled").checked);
  const d = +picked("fade_duration") || 0.6;   // demo cycle scales with the chosen speed
  row.style.setProperty("--fade-cycle", `${(d * 2 / 0.44).toFixed(2)}s`);
}

function fillForm() {
  $("handle").value = cfg.handle;
  $("ring").checked = cfg.ring;
  $("opacity").value = cfg.opacity;
  $("opacityVal").textContent = `${cfg.opacity}%`;
  for (const [id, key] of [["posX", "pos_x"], ["posY", "pos_y"], ["sideX", "side_x"], ["sideY", "side_y"]]) $(id).value = cfg[key];
  paintPos();
  document.querySelectorAll('input[name="size"]').forEach((i) => { i.checked = i.value === cfg.size; });
  document.querySelectorAll("#applyList input").forEach((i) => { i.checked = !!cfg.apply[i.value]; });
  $("fadeEnabled").checked = cfg.fade_enabled;
  document.querySelectorAll('input[name="fade_duration"]').forEach((i) => { i.checked = +i.value === cfg.fade_duration; });
  $("sideEnabled").checked = cfg.side_enabled;
  $("sideOpacity").value = cfg.side_opacity;
  $("sideOpacityVal").textContent = `${cfg.side_opacity}%`;
  for (const k of ["side_size", "side_shape", "pos_area"]) {
    document.querySelectorAll(`input[name="${k}"]`).forEach((i) => { i.checked = i.value === cfg[k]; });
  }
}

async function saveNow() {
  if (!isDirty() || saving) return;
  saving = true;
  $("saveBtn").disabled = true;
  $("saveBtn").textContent = "Saving…";
  try {
    cfg = (await putJSON("/api/branding", collect())).settings;
    fillForm();
    state("Settings saved ✓", "ok");
  } catch (e) {
    state(e.message, "err");
  } finally {
    saving = false;
    $("saveBtn").textContent = "Save settings";
    changed();
    if (!isDirty() && !$("saveState").classList.contains("err")) state("Settings saved ✓", "ok");
  }
}

// main (round) logo and side watermark share the upload/remove flow
const SLOTS = {
  main: { api: "/api/branding/logo", drop: "logoDrop", file: "logoFile", error: "logoError", has: "has_logo", ver: "logo_version", label: "Logo" },
  side: { api: "/api/branding/side-logo", drop: "sideDrop", file: "sideFile", error: "sideError", has: "has_side_logo", ver: "side_logo_version", label: "Side watermark" },
};
function takeFiles(fresh) {
  cfg = { ...cfg, has_logo: fresh.has_logo, logo_version: fresh.logo_version,
    has_side_logo: fresh.has_side_logo, side_logo_version: fresh.side_logo_version };
}

async function uploadLogo(file, slot = "main") {
  const s = SLOTS[slot];
  $(s.error).textContent = "";
  if (!file) return;
  if (!/^image\/(png|jpeg|webp)$/.test(file.type)) { $(s.error).textContent = "Use a PNG, JPG or WebP image."; return; }
  if (file.size > 15 * 1048576) { $(s.error).textContent = "The image is larger than 15 MB."; return; }
  $(s.drop).classList.add("busy");
  state(`Uploading ${s.label.toLowerCase()}…`);
  try {
    takeFiles((await send("PUT", s.api, file, { "X-OwlCut-Upload": "1", "Content-Type": "application/octet-stream" })).settings);
    state(isDirty() ? `${s.label} saved — other changes still unsaved` : `${s.label} saved ✓`, isDirty() ? "dirty" : "ok");
    changed();
  } catch (e) {
    $(s.error).textContent = e.message;
    state("");
  } finally {
    $(s.drop).classList.remove("busy");
    $(s.file).value = "";
  }
}

async function removeLogo(slot) {
  const s = SLOTS[slot];
  try {
    takeFiles((await send("POST", `${s.api}/remove`, "{}", { "Content-Type": "application/json" })).settings);
    state(`${s.label} removed`, "ok");
    changed();
  } catch (e) { state(e.message, "err"); }
}

async function init() {
  const data = await getJSON("/api/branding");
  cfg = data.settings;
  opts = data.options;

  radios($("sizeSeg"), "size", opts.sizes.map((s) => [s, s[0].toUpperCase() + s.slice(1)]), cfg.size);
  radios($("kindSeg"), "pkind", Object.entries(opts.kinds), kind);
  radios($("areaSeg"), "pos_area", Object.entries(opts.areas), cfg.pos_area);
  radios($("fadeSeg"), "fade_duration", opts.fades.map((f) => [String(f.value), `${f.label} · ${f.value} s`]), String(cfg.fade_duration));
  radios($("sideShapeSeg"), "side_shape", Object.entries(opts.side_shapes), cfg.side_shape);
  radios($("sideSizeSeg"), "side_size", opts.side_sizes.map((s) => [s, s[0].toUpperCase() + s.slice(1)]), cfg.side_size);
  for (const [k, label] of Object.entries(opts.kinds)) {
    const input = el("input", { type: "checkbox", value: k });
    input.checked = !!cfg.apply[k];
    $("applyList").append(el("label", {}, input, label));
  }
  fillForm();
  changed();

  $("form").addEventListener("submit", (e) => { e.preventDefault(); saveNow(); });
  $("form").addEventListener("change", (e) => {
    if (e.target.id === "logoFile") return uploadLogo(e.target.files[0], "main");
    if (e.target.name === "pos_area") paintPos();
    if (e.target.id === "sideFile") return uploadLogo(e.target.files[0], "side");
    changed();
  });
  $("handle").addEventListener("input", changed);
  $("handle").addEventListener("blur", () => { $("handle").value = norm($("handle").value); });
  for (const id of ["posX", "posY", "sideX", "sideY"]) {
    $(id).addEventListener("input", () => { paintPos(); changed(); });   // live: the preview follows the drag
  }
  document.querySelectorAll(".pos-presets").forEach((box) => box.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-x]");
    if (!b) return;
    const p = box.dataset.for;
    $(`${p}X`).value = b.dataset.x;
    $(`${p}Y`).value = b.dataset.y;
    paintPos();
    changed();
  }));
  $("opacity").addEventListener("input", () => { $("opacityVal").textContent = `${$("opacity").value}%`; changed(); });
  $("sideOpacity").addEventListener("input", () => { $("sideOpacityVal").textContent = `${$("sideOpacity").value}%`; changed(); });
  $("resetBtn").addEventListener("click", () => { fillForm(); changed(); state(""); });
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") { e.preventDefault(); saveNow(); }
  });
  window.addEventListener("beforeunload", (e) => { if (isDirty()) { e.preventDefault(); e.returnValue = ""; } });
  $("kindSeg").addEventListener("change", (e) => { e.stopPropagation(); kind = picked("pkind"); paintPreview(); });
  try {
    const samples = await getJSON("/api/branding/samples");
    let mine = null;
    for (const smp of samples) {
      const opt = el("option", { value: smp.id }, smp.label);
      if (smp.id.startsWith("quick:")) {
        if (!mine) { mine = el("optgroup", { label: "Your clips" }); $("sample").append(mine); }
        mine.append(opt);
      } else $("sample").append(opt);
    }
  } catch { $("sample").append(el("option", { value: "demo-wide" }, "Example · wide video")); }
  try { const s = localStorage.getItem("owlcut.settings.sample"); if (s && [...$("sample").options].some((o) => o.value === s)) $("sample").value = s; } catch { /* ignore */ }
  $("sample").addEventListener("change", () => {
    try { localStorage.setItem("owlcut.settings.sample", $("sample").value); } catch { /* ignore */ }
    paintPreview();
  });
  $("nextFrame").addEventListener("click", () => { frameT = (frameT + 0.27) % 1; paintPreview(); });
  paintPreview();
  $("logoRemove").addEventListener("click", () => removeLogo("main"));
  $("sideRemove").addEventListener("click", () => removeLogo("side"));

  for (const [slot, s] of Object.entries(SLOTS)) {
    const drop = $(s.drop);
    ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => uploadLogo(e.dataTransfer.files[0], slot));
  }
}

init().catch((e) => state(e.message, "err"));
