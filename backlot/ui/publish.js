// /publish — list of clips kits, and the per-network publishing kit of one job.

import { el, getJSON, mediaURL, thumbURL } from "/ui/lib.js";
import { postJSON } from "/ui/create-lib.js";

const $ = (id) => document.getElementById(id);
const jobId = decodeURIComponent(location.pathname.split("/publish/")[1] || "").replace(/\/$/, "");
let data = null;
let platform = null;

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("on");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.remove("on"), 1800);
}

function ago(epoch) {
  if (!epoch) return "";
  const d = Date.now() / 1000 - epoch;
  if (d < 3600) return `${Math.max(1, Math.round(d / 60))} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  return `${Math.round(d / 86400)} d ago`;
}

// ---------------------------------------------------------------- list mode
async function renderList() {
  $("listMode").hidden = false;
  const kits = await getJSON("/api/publish");
  const ready = kits.filter((k) => k.clips > 0);
  const grid = $("kits");
  grid.innerHTML = "";
  $("listEmpty").hidden = ready.length > 0;
  for (const k of ready) {
    const poster = el("div", { class: "lib-poster" });
    if (k.poster) poster.append(el("img", { src: thumbURL(k.project_id, k.poster, 640), alt: "", loading: "lazy" }));
    else poster.append(el("span", { class: "lp-txt" }, "No cover"));
    const pct = k.posts ? Math.round((k.posted / k.posts) * 100) : 0;
    grid.append(el("a", { class: "lib-card", href: `/publish/${encodeURIComponent(k.id)}`, style: "text-decoration:none;color:inherit" },
      poster,
      el("div", { class: "lib-body" },
        el("h3", {}, k.title || k.id),
        el("div", { class: "lb-meta" },
          el("span", { class: "chip" }, k.clips === 1 ? "1 clip" : `${k.clips} clips`),
          el("span", { class: "chip" }, `${k.posted}/${k.posts} posted`),
          el("span", { class: "when" }, ago(k.updated_at))),
        el("div", { class: "mini-rail", "aria-hidden": "true" }, el("i", { class: "d", style: `flex:${pct || 0.001}` }), el("i", { style: `flex:${100 - pct || 0.001}` })),
      )));
  }
}

// ---------------------------------------------------------------- kit mode
function counter(len, max) {
  return el("span", { class: `count${len > max ? " over" : len > max * 0.9 ? " near" : ""}` }, `${len}/${max}`);
}

async function save(clip, patch, quiet = false) {
  try {
    const saved = await postJSON(`/api/publish/${encodeURIComponent(jobId)}/${encodeURIComponent(clip.id)}/${platform}`, patch);
    Object.assign(clip.platforms[platform], saved, { edited: true });
    if (!quiet) toast("Saved");
    return true;
  } catch (e) {
    toast(e.message);
    return false;
  }
}

function postText(entry) {
  const parts = [(entry.caption || "").trim()];
  if (entry.hashtags && entry.hashtags.length) parts.push(entry.hashtags.join(" "));
  return parts.filter(Boolean).join("\n\n");
}

async function copy(text, what) {
  try { await navigator.clipboard.writeText(text); toast(`${what} copied`); }
  catch { toast("Couldn't copy — select the text and copy it by hand."); }
}

function clipRow(clip) {
  const e = clip.platforms[platform];
  const lim = data.limits[platform];
  const pid = data.job.project_id;
  const id = `${clip.id}-${platform}`;

  const vid = el("video", { src: mediaURL(pid, e.video), controls: "", preload: "metadata", playsinline: "" });
  if (clip.thumbnail) vid.setAttribute("poster", mediaURL(pid, clip.thumbnail));
  const media = el("div", { class: "pub-media" }, el("div", { class: "render-frame" }, vid));

  const fields = el("div", { class: "pub-fields" });
  if (lim.title) {
    const t = el("input", { id: `t-${id}`, type: "text", maxlength: String(lim.title), value: e.title || "" });
    const c = counter((e.title || "").length, lim.title);
    t.addEventListener("input", () => fields.querySelector(`[data-c="t"]`).replaceChildren(counter(t.value.length, lim.title)));
    t.addEventListener("change", () => save(clip, { title: t.value }));
    fields.append(el("div", { class: "field" },
      el("label", { for: `t-${id}` }, "Title ", el("span", { "data-c": "t" }, c)), t));
  }
  const cap = el("textarea", { id: `c-${id}`, rows: "5" });
  cap.value = e.caption || "";
  cap.addEventListener("input", () => fields.querySelector(`[data-c="c"]`).replaceChildren(counter(cap.value.length, lim.caption)));
  cap.addEventListener("change", () => save(clip, { caption: cap.value }));
  fields.append(el("div", { class: "field" },
    el("label", { for: `c-${id}` }, "Caption ", el("span", { "data-c": "c" }, counter(cap.value.length, lim.caption))), cap));

  const tags = el("input", { id: `h-${id}`, type: "text", value: (e.hashtags || []).join(" "), placeholder: "#topic #subject" });
  tags.addEventListener("change", async () => {
    if (await save(clip, { hashtags: tags.value })) tags.value = clip.platforms[platform].hashtags.join(" ");
  });
  fields.append(el("div", { class: "field" },
    el("label", { for: `h-${id}` }, "Hashtags ", el("span", { class: "count" }, `up to ${lim.hashtags}`)), tags));

  const posted = el("input", { type: "checkbox" });
  posted.checked = !!e.posted;
  posted.addEventListener("change", async () => {
    if (await save(clip, { posted: posted.checked }, true)) {
      toast(posted.checked ? "Marked as posted" : "Unmarked");
      row.classList.toggle("done", posted.checked);
      renderProgress();
    }
  });
  const actions = el("div", { class: "pub-actions" },
    el("button", { type: "button", class: "btn primary small", onclick: () => copy(postText({ caption: cap.value, hashtags: tags.value.split(/\s+/).filter(Boolean) }), "Text") }, "Copy caption + hashtags"),
    lim.title ? el("button", { type: "button", class: "btn small", onclick: () => copy(fields.querySelector("input[type=text]").value, "Title") }, "Copy title") : null,
    el("a", { class: "btn small", href: mediaURL(pid, e.video), download: `${String(clip.rank).padStart(2, "0")}-${clip.id}.mp4` }, "Download video"),
    clip.thumbnail ? el("a", { class: "btn small", href: mediaURL(pid, clip.thumbnail), download: `${clip.id}-cover.jpg` }, "Download cover") : null,
    el("label", { class: "use posted" }, posted, el("span", {}, "Posted")),
  );

  const head = el("div", { class: "pub-head" },
    el("span", { class: "rank mono" }, `#${clip.rank}`),
    el("h3", {}, clip.title || clip.id),
    clip.duration ? el("span", { class: "chip" }, `${Math.round(clip.duration)} s`) : null,
    !e.aspect_ok ? el("span", { class: "chip", title: "No render in this network's format; using another one." }, "fallback format") : null,
    !e.has_draft && !e.edited ? el("span", { class: "chip" }, "no agent text") : null,
  );

  const row = el("article", { class: `pub-row${e.posted ? " done" : ""}` }, media, el("div", { class: "pub-main" }, head, fields, actions));
  return row;
}

function renderProgress() {
  const all = data.clips.map((c) => c.platforms[platform]);
  const done = all.filter((e) => e.posted).length;
  $("progText").textContent = `${done} of ${all.length} posted on ${data.labels[platform]}`;
  $("progBar").style.width = all.length ? `${(done / all.length) * 100}%` : "0";
}

function renderKit() {
  const list = $("pubList");
  list.innerHTML = "";
  for (const c of data.clips) list.append(clipRow(c));
  $("draftNote").hidden = data.clips.some((c) => c.platforms[platform].has_draft || c.platforms[platform].edited);
  renderProgress();
}

async function loadKit() {
  $("kitMode").hidden = false;
  try {
    data = await getJSON(`/api/publish/${encodeURIComponent(jobId)}`);
  } catch {
    $("title").textContent = "Kit not found";
    $("sub").textContent = "This link doesn't match a clips production.";
    $("kitMode").hidden = true;
    return;
  }
  const j = data.job;
  $("title").textContent = j.title || j.id;
  document.title = `OwlCut — Publish · ${j.title || ""}`;
  $("sub").textContent = data.clips.length
    ? "Review the text, copy it and post. Your edits are saved here."
    : "No rendered clips yet. Come back once the agent finishes rendering.";
  const meta = $("meta");
  meta.innerHTML = "";
  meta.append(el("span", { class: "chip" }, data.clips.length === 1 ? "1 clip" : `${data.clips.length} clips`));
  if (data.source && data.source.channel) meta.append(el("span", { class: "chip" }, data.source.channel));
  const act = $("actions");
  act.innerHTML = "";
  act.append(
    el("a", { class: "btn small", href: `/clips/${encodeURIComponent(j.id)}` }, "Review clips"),
    data.clips.length ? el("a", { class: "btn primary small", href: `/api/publish/${encodeURIComponent(j.id)}/kit.zip` }, "Download kit (.zip)") : null,
  );
  const tabs = $("tabs");
  tabs.innerHTML = "";
  const saved = (() => { try { return localStorage.getItem("backlot.pub.platform"); } catch { return null; } })();
  platform = data.platforms.includes(saved) ? saved : data.platforms[0];
  for (const p of data.platforms) {
    const r = el("input", { type: "radio", name: "platform", value: p });
    r.checked = p === platform;
    r.addEventListener("change", () => {
      platform = p;
      try { localStorage.setItem("backlot.pub.platform", p); } catch { /* storage blocked */ }
      renderKit();
    });
    tabs.append(el("label", {}, r, data.labels[p]));
  }
  if (data.clips.length) renderKit();
  else $("kitMode").hidden = true;
}

if (jobId) loadKit(); else renderList().catch((e) => { $("sub").textContent = e.message; });
