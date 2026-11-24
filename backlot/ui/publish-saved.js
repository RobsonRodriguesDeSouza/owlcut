// /publish — "Saved in OwlCutVideos": every production in the output folder; pick a video,
// write the post text per network, copy it, download the video and mark it as posted.

import { el, getJSON } from "/ui/lib.js";

const $ = (id) => document.getElementById(id);
const KIND = { clips: "Clips", ranking: "Ranking", video: "AI video" };
const HOW = {
  tiktok: "TikTok: send the video to your phone (AirDrop), tap +, pick it, paste the caption.",
  reels: "Instagram: send the video to your phone (AirDrop), + → Reel, pick it, paste the caption.",
  shorts: "YouTube: on studio.youtube.com → Create → Upload (a vertical video under 60 s becomes a Short), paste title and description.",
};
let toast = () => {};
let current = null;   // post state of the open video
let platform = null;

const enc = encodeURIComponent;
const mb = (n) => `${(n / 1e6).toFixed(1)} MB`;

async function send(method, url, body) {
  const res = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`);
  return data;
}

function counter(len, max) {
  return el("span", { class: `count${len > max ? " over" : len > max * 0.9 ? " near" : ""}` }, `${len}/${max}`);
}

function postText(e) {
  return [e.caption.trim(), e.hashtags.join(" ")].filter(Boolean).join("\n\n");
}

async function copy(text, what) {
  try { await navigator.clipboard.writeText(text); toast(`${what} copied`); }
  catch { toast("Couldn't copy — select the text and copy it by hand."); }
}

// ------------------------------------------------------------------ list
export async function renderSaved(toastFn) {
  toast = toastFn || toast;
  $("savedRefresh").onclick = () => renderSaved();
  const data = await getJSON("/api/saved");
  const list = $("saved");
  list.innerHTML = "";
  const prods = data.productions || [];
  const total = prods.reduce((n, p) => n + p.videos.length, 0);
  $("savedCount").textContent = prods.length ? `${prods.length} productions · ${total} videos` : "";
  $("savedRoot").textContent = `Folder: ${data.root}`;
  $("savedEmpty").hidden = prods.length > 0;
  for (const p of prods) {
    const grid = el("div", { class: "saved-grid" });
    for (const v of p.videos) grid.append(videoCard(v));
    const posted = p.videos.filter((v) => v.posted.length).length;
    list.append(el("section", { class: "saved-prod" },
      el("div", { class: "saved-prod-head" },
        el("span", { class: `chip kind-${p.kind}` }, KIND[p.kind] || p.category),
        el("h3", {}, p.title),
        p.date ? el("span", { class: "when" }, p.date) : null,
        el("span", { class: "when" }, `${posted}/${p.videos.length} posted`),
        el("button", { class: "btn small", type: "button", style: "margin-left:auto",
          onclick: () => send("POST", "/api/saved/reveal", { path: p.videos[0].path }).catch((e) => toast(e.message)) }, "Show in Finder")),
      grid));
  }
}

function videoCard(v) {
  const badges = el("div", { class: "saved-badges" },
    ...v.posted.map((p) => el("span", { class: "chip posted-chip" }, `✓ ${{ tiktok: "TikTok", reels: "Reels", shorts: "Shorts" }[p] || p}`)));
  return el("button", { type: "button", class: `saved-card${v.posted.length ? " done" : ""}`, onclick: () => openPost(v.path),
    title: v.name },
    el("div", { class: "saved-thumb" }, el("img", { src: `/api/saved/thumb?path=${enc(v.path)}`, alt: "", loading: "lazy",
      onerror: (e) => { e.target.replaceWith(el("span", { class: "lp-txt" }, "No preview")); } })),
    el("div", { class: "saved-meta" }, el("b", {}, v.label), el("span", { class: "mono" }, mb(v.size)), badges));
}

// ------------------------------------------------------------------ post dialog
let tiktok = null;    // connection status (Settings → Social accounts)

async function openPost(path) {
  try {
    [current, tiktok] = await Promise.all([
      getJSON(`/api/saved/post?path=${enc(path)}`),
      getJSON("/api/social/tiktok").catch(() => null),
    ]);
  } catch (e) { toast(e.message); return; }
  const dlg = $("postDlg");
  $("pdTitle").textContent = current.label;
  $("pdSub").textContent = `${current.name} · ${mb(current.size)}`;
  const vid = $("pdVideo");
  vid.poster = `/api/saved/thumb?path=${enc(path)}`;
  vid.src = `/api/saved/video?path=${enc(path)}`;
  const saved = (() => { try { return localStorage.getItem("backlot.pub.platform"); } catch { return null; } })();
  platform = current.platforms.includes(saved) ? saved : current.platforms[0];
  const tabs = $("pdTabs");
  tabs.innerHTML = "";
  for (const p of current.platforms) {
    const r = el("input", { type: "radio", name: "pdPlatform", value: p });
    r.checked = p === platform;
    r.addEventListener("change", () => {
      flush();
      platform = p;
      try { localStorage.setItem("backlot.pub.platform", p); } catch { /* storage blocked */ }
      paintPost();
    });
    const done = current.posts[p].posted ? " ✓" : "";
    tabs.append(el("label", {}, r, current.labels[p] + done));
  }
  $("pdClose").onclick = () => dlg.close();
  dlg.onclose = () => { flush(); vid.pause(); vid.removeAttribute("src"); vid.load(); renderSaved().catch(() => {}); };
  paintPost();
  if (!dlg.open) dlg.showModal();
}

let saveTimer = null;
async function save(patch, quiet = true, p = platform) {
  try {
    current.posts[p] = await send("PUT", "/api/saved/post", { path: current.path, platform: p, ...patch });
    if (!quiet) toast("Saved");
    return true;
  } catch (e) { toast(e.message); return false; }
}

function paintPost() {
  const e = current.posts[platform];
  const lim = current.limits[platform];
  const fields = $("pdFields");
  fields.innerHTML = "";
  let titleIn = null;
  if (lim.title) {
    titleIn = el("input", { type: "text", maxlength: String(lim.title), value: e.title || "", id: "pdT" });
    const c = el("span", {}, counter(titleIn.value.length, lim.title));
    titleIn.addEventListener("input", () => { c.replaceChildren(counter(titleIn.value.length, lim.title)); queue({ title: titleIn.value }); });
    fields.append(el("div", { class: "field" }, el("label", { for: "pdT" }, "Title ", c), titleIn));
  }
  const cap = el("textarea", { id: "pdC", rows: "5" });
  cap.value = e.caption || "";
  const cc = el("span", {}, counter(cap.value.length, lim.caption));
  cap.addEventListener("input", () => { cc.replaceChildren(counter(cap.value.length, lim.caption)); queue({ caption: cap.value }); });
  fields.append(el("div", { class: "field" }, el("label", { for: "pdC" }, platform === "shorts" ? "Description " : "Caption ", cc), cap));
  const tags = el("input", { id: "pdH", type: "text", value: (e.hashtags || []).join(" "), placeholder: "#gta6 #gaming" });
  tags.addEventListener("change", () => save({ hashtags: tags.value }).then((ok) => { if (ok) tags.value = current.posts[platform].hashtags.join(" "); }));
  fields.append(el("div", { class: "field" }, el("label", { for: "pdH" }, "Hashtags ", el("span", { class: "count" }, `up to ${lim.hashtags}`)), tags));

  const posted = el("input", { type: "checkbox" });
  posted.checked = !!e.posted;
  posted.addEventListener("change", async () => {
    if (await save({ posted: posted.checked })) {
      toast(posted.checked ? `Marked as posted on ${current.labels[platform]}` : "Unmarked");
      const lbl = [...$("pdTabs").querySelectorAll("label")].find((l) => l.querySelector("input").value === platform);
      if (lbl) lbl.lastChild.textContent = current.labels[platform] + (posted.checked ? " ✓" : "");
    } else posted.checked = !posted.checked;
  });
  const now = () => ({ caption: cap.value, hashtags: tags.value.split(/[\s,]+/).filter(Boolean).map((t) => (t.startsWith("#") ? t : `#${t}`)) });
  const act = $("pdActions");
  act.innerHTML = "";
  act.append(...[
    el("button", { type: "button", class: "btn primary small", onclick: () => copy(postText(now()), "Text") },
      platform === "shorts" ? "Copy description + hashtags" : "Copy caption + hashtags"),
    titleIn ? el("button", { type: "button", class: "btn small", onclick: () => copy(titleIn.value, "Title") }, "Copy title") : null,
    el("a", { class: "btn small", href: `/api/saved/video?path=${enc(current.path)}&download=1`, download: current.name }, "Download video"),
    el("button", { type: "button", class: "btn small", onclick: () => send("POST", "/api/saved/reveal", { path: current.path }).catch((x) => toast(x.message)) }, "Show in Finder"),
    el("label", { class: `use posted${e.posted ? " on" : ""}` }, posted, el("span", {}, "Posted")),
  ].filter(Boolean));
  posted.addEventListener("change", () => posted.parentElement.classList.toggle("on", posted.checked));
  $("pdHow").textContent = HOW[platform] || "";
  if (platform === "tiktok") paintTikTok(act, now);
}

// typing saves after a short pause; switching network or closing saves right away
let pending = null;
function queue(patch) {
  clearTimeout(saveTimer);
  pending = { p: platform, patch: { ...(pending && pending.p === platform ? pending.patch : {}), ...patch } };
  saveTimer = setTimeout(flush, 600);
}
function flush() {
  clearTimeout(saveTimer);
  if (!pending) return;
  const { p, patch } = pending;
  pending = null;
  save(patch, true, p);
}

// ------------------------------------------------------------------ send to TikTok (inbox draft)
const TK_STATUS = {
  PROCESSING_UPLOAD: "TikTok is receiving the video…",
  PROCESSING_DOWNLOAD: "TikTok is receiving the video…",
  SEND_TO_USER_INBOX: "In your TikTok inbox ✓ — open TikTok on your phone, tap the notification, paste the caption and post.",
  PUBLISH_COMPLETE: "Posted on TikTok ✓",
  FAILED: "TikTok couldn't process the video",
};

function paintTikTok(act, now) {
  const box = el("div", { class: "tk-send" });
  act.after(box);
  if (!tiktok || !tiktok.connected) {
    box.append(el("span", {}, "Send it straight to your TikTok inbox: "),
      el("a", { href: "/settings#social" }, tiktok && tiktok.configured ? "connect TikTok in Settings" : "set up TikTok in Settings"));
    return;
  }
  const e = current.posts.tiktok;
  const who = (tiktok.account && tiktok.account.display_name) || "your account";
  const line = el("span", { class: "tk-line" }, e.sent_at
    ? `Sent to ${who}'s inbox on ${new Date(e.sent_at * 1000).toLocaleString()}.`
    : `Sends the video to ${who}'s TikTok inbox as a draft. The caption is copied for you to paste.`);
  const btn = el("button", { type: "button", class: "btn small tk-btn" }, e.sent_at ? "Send again" : "Send to TikTok");
  btn.addEventListener("click", async () => {
    const rel = current.path;
    btn.disabled = true;
    line.textContent = "Uploading to TikTok…";
    copy(postText(now()), "Caption");
    try {
      const r = await fetch("/api/saved/tiktok", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: rel }) });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
      current.posts.tiktok.sent_at = Date.now() / 1000;
      for (let i = 0; i < 40; i++) {                 // ~2 min
        await new Promise((ok) => setTimeout(ok, 3000));
        let s;
        try { s = await getJSON(`/api/social/tiktok/publish/${enc(data.publish_id)}`); } catch { continue; }
        line.textContent = (TK_STATUS[s.status] || s.status) + (s.fail_reason ? ` (${s.fail_reason})` : "");
        if (["SEND_TO_USER_INBOX", "PUBLISH_COMPLETE", "FAILED"].includes(s.status)) break;
      }
    } catch (err) {
      line.textContent = err.message;
      line.classList.add("err");
    } finally { btn.disabled = false; btn.textContent = "Send again"; }
  });
  box.append(btn, line);
}
