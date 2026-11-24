// Settings — Social accounts · TikTok (keys, connect, disconnect). Separate from the
// watermark form: it saves on its own and never touches the watermark's Save bar.

import { getJSON } from "/ui/lib.js";

const $ = (id) => document.getElementById(id);
let st = null;

async function send(method, url, body) {
  const res = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`);
  return data;
}

function say(msg, cls = "") {
  const s = $("tkState");
  s.textContent = msg;
  s.className = `save-state ${cls}`;
}

function paint() {
  $("tkKey").value = st.client_key;
  $("tkSecret").value = "";
  $("tkSecret").placeholder = st.has_secret ? "•••••••• saved — paste a new one to replace it" : "Paste the Client secret";
  $("tkSecretHelp").textContent = st.has_secret ? "Saved on this Mac only (.backlot/social, readable by your user only)." : "";
  $("tkRedirect").value = st.redirect_uri;
  $("tkForget").hidden = !st.configured;
  const c = $("tkConnect");
  c.setAttribute("aria-disabled", st.configured ? "false" : "true");
  c.textContent = st.connected ? "Reconnect" : "Connect TikTok";
  c.classList.toggle("primary", !st.connected);
  const acc = $("tkAccount");
  acc.hidden = !st.connected;
  if (st.connected) {
    $("tkName").textContent = (st.account && st.account.display_name) || "TikTok account";
    const av = $("tkAvatar");
    if (st.account && st.account.avatar_url) { av.src = st.account.avatar_url; av.hidden = false; } else av.hidden = true;
    const when = st.connected_at ? new Date(st.connected_at * 1000).toLocaleDateString() : "";
    $("tkSince").textContent = `Connected${when ? ` on ${when}` : ""} · can send videos to your TikTok inbox`;
  }
}

async function load() {
  st = await getJSON("/api/social/tiktok");
  paint();
}

async function saveKeys() {
  const btn = $("tkSave");
  btn.disabled = true;
  say("Saving…");
  try {
    st = await send("PUT", "/api/social/tiktok/keys", {
      client_key: $("tkKey").value, client_secret: $("tkSecret").value, redirect_uri: $("tkRedirect").value,
    });
    paint();
    say(st.connected ? "Keys saved" : "Keys saved — now click Connect TikTok", "ok");
  } catch (e) { say(e.message, "err"); }
  finally { btn.disabled = false; }
}

async function init() {
  await load();
  $("tkSave").addEventListener("click", saveKeys);
  $("tkCopy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("tkRedirect").value); say("Redirect URI copied", "ok"); }
    catch { $("tkRedirect").select(); say("Select it and copy by hand"); }
  });
  $("tkConnect").addEventListener("click", (e) => {
    if ($("tkKey").value.trim() !== st.client_key || $("tkSecret").value.trim() || $("tkRedirect").value.trim() !== st.redirect_uri) {
      e.preventDefault();
      say("Save the keys first.", "err");
    }
  });
  $("tkDisconnect").addEventListener("click", async () => {
    try { st = await send("POST", "/api/social/tiktok/disconnect"); paint(); say("Disconnected", "ok"); }
    catch (e) { say(e.message, "err"); }
  });
  $("tkForget").addEventListener("click", async () => {
    if (!confirm("Remove the TikTok keys and disconnect?")) return;
    try { st = await send("POST", "/api/social/tiktok/forget"); paint(); say("Keys removed", "ok"); }
    catch (e) { say(e.message, "err"); }
  });

  // back from TikTok's login page
  const q = new URLSearchParams(location.search);
  if (q.has("tiktok") || q.has("tiktok_error")) {
    if (q.get("tiktok") === "connected") say("TikTok connected", "ok");
    else say(`TikTok: ${q.get("tiktok_error")}`, "err");
    $("social").scrollIntoView({ block: "start" });
    history.replaceState(null, "", "/settings#social");
  }
}

init().catch((e) => say(e.message, "err"));
