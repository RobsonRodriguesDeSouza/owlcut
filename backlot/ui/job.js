import { STAGE_ICONS, el, fmtMoney, getJSON, mediaURL } from "/ui/lib.js";
import { mountThemeToggle, postJSON, renderMarkdown, statusBadge } from "/ui/create-lib.js";

mountThemeToggle(document.getElementById("backLink"));

const jobId = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop());
const $ = (id) => document.getElementById(id);
let job = null;
let lastLogLen = -1;
let lastMsg = null;
let lastStatus = null;
let currentRender = null;
let busy = false;

const ICON = { user: "›", assistant: "◆", tool: "·", status: "◈" };

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
}

function renderHeader() {
  $("title").textContent = (job.title || job.id).toUpperCase();
  document.title = `OwlCut — ${job.title || job.id}`;
  const meta = $("meta");
  meta.innerHTML = "";
  meta.append(statusBadge(job.status));
  meta.append(el("span", { class: "chip" }, job.form?.pipeline || "—"));
  if (job.turns) meta.append(el("span", { class: "chip" }, `${job.turns} turn${job.turns > 1 ? "s" : ""}`));
  if (job.cost_usd) meta.append(el("span", {
    class: "chip",
    title: "API-equivalent estimate reported by Claude Code. On a subscription plan (Pro/Max) it is not billed: it uses the plan's usage limit. It only becomes a real charge if Claude Code is using an ANTHROPIC_API_KEY.",
  }, `estimated usage ≈ ${fmtMoney(job.cost_usd)}`));
  const board = $("boardLink");
  if (job.kind === "clips") {
    board.href = `/clips/${encodeURIComponent(job.id)}`;
    board.textContent = "Review clips";
    board.classList.add("primary");
    board.style.display = "";
  } else if (job.project_exists) {
    board.href = `/p/${job.project_id}`;
    board.style.display = "";
  }
}

const STAGE_LABEL = {
  research: ["Research", "Gathers facts and references on the topic"],
  idea: ["Idea", "Defines the video's concept"],
  proposal: ["Proposal", "Concepts, approach and cost estimate"],
  script: ["Script", "Narration text, scene by scene"],
  character_design: ["Characters", "How the characters look"],
  rig_plan: ["Rig / animation", "How the characters move"],
  scene_plan: ["Scene plan", "Look and timing of each scene"],
  assets: ["Assets", "Voice, music, images and animation"],
  edit: ["Edit", "Cuts, captions and mix"],
  compose: ["Render", "Assembles and renders the video"],
  publish: ["Delivery", "Final review and export"],
};
let prevStageStatus = {};

function stageClass(st) {
  return st.status === "completed" ? "done"
    : st.status === "in_progress" ? "active"
    : st.status === "awaiting_human" ? "await"
    : st.status === "failed" ? "failed" : "";
}

function stageSub(st, cls) {
  if (cls === "await") return "◈ waiting for your approval";
  if (cls === "failed") return st.error ? String(st.error).slice(0, 60) : "failed";
  if (cls === "active") {
    if (st.stalled) return `no activity for ${st.stalled_minutes} min`;
    const done = st.partial_progress && st.partial_progress.completed_scene_ids;
    if (Array.isArray(done)) return `${done.length} scene${done.length === 1 ? "" : "s"} done`;
    return st.guessed ? "in progress…" : "in progress";
  }
  if (cls === "done" && st.timestamp) {
    const t = new Date(st.timestamp).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
    return `✓ ${t}${st.gated && st.human_approved ? " · aprovado" : ""}`;
  }
  return "";
}

function renderRail() {
  const stages = (job.stages || []).filter((s) => !s.undeclared).map((s) => ({ ...s }));
  // Before the agent writes a checkpoint for the stage it is working on,
  // highlight the next pending one so the rail never looks idle while running.
  if (job.status === "running" && !stages.some((s) => ["in_progress", "awaiting_human"].includes(s.status))) {
    const next = stages.find((s) => s.status !== "completed");
    if (next) { next.status = "in_progress"; next.guessed = true; }
  }
  const rail = $("rail");
  rail.innerHTML = "";
  let n = 1;
  for (const st of stages) {
    const cls = stageClass(st);
    const icon = STAGE_ICONS[st.status] || String(n);
    n += 1;
    const [label, desc] = STAGE_LABEL[st.name] || [st.name, ""];
    const fresh = prevStageStatus[st.name] && prevStageStatus[st.name] !== st.status;
    rail.append(el("li", { class: `vstage ${cls}${fresh ? " fresh" : ""}`, "aria-current": cls === "active" || cls === "await" ? "step" : null },
      el("span", { class: "vline" }),
      el("span", { class: "node" }, icon),
      el("div", { class: "vtxt" },
        el("div", { class: "name" }, label, st.gated ? el("span", { class: "gate", title: "Stage with human approval" }, "approval") : null),
        el("div", { class: "desc" }, desc),
        stageSub(st, cls) ? el("div", { class: "sub" }, stageSub(st, cls)) : null,
      ),
    ));
  }
  prevStageStatus = Object.fromEntries(stages.map((s) => [s.name, s.status]));
  const done = stages.filter((s) => s.status === "completed").length;
  $("railCount").textContent = `${done} de ${stages.length}`;
  $("railBar").style.width = stages.length ? `${(done / stages.length) * 100}%` : "0";
}

function renderMessage() {
  const s = job.status;
  const key = `${s}|${job.last_message}|${job.error || ""}`;
  if (key === lastMsg) return;
  lastMsg = key;

  const body = $("msgBody");
  const card = $("msgCard");
  body.innerHTML = "";
  card.classList.toggle("attention", s === "waiting");
  $("reply").style.display = ["waiting", "cancelled", "interrupted", "error", "done"].includes(s) ? "" : "none";
  $("runningActions").style.display = s === "running" ? "" : "none";

  const approve = $("approveBtn");
  approve.style.display = s === "done" ? "none" : "";
  $("changesBtn").textContent = s === "done" ? "✎ Request a change" : (s === "waiting" ? "✎ Request changes" : "↻ Send and continue");
  approve.textContent = s === "waiting" ? "✓ Approve and continue" : "▶ Continue";

  const titles = {
    running: "Agent working",
    waiting: "Your turn: the agent is waiting for your answer",
    done: "Production finished",
    error: "Something went wrong",
    cancelled: "Agent paused",
    interrupted: "Turn interrupted",
  };
  $("msgTitle").textContent = titles[s] || "Agent";

  if (s === "running") {
    body.append(el("div", { class: "thinking" }, el("span", { class: "spinner" }),
      "The agent is producing. Stages appear live in the activity panel and on the storyboard."));
    if (job.last_message) {
      const prev = el("div", { class: "md", style: "margin-top:18px;opacity:.6" });
      prev.innerHTML = renderMarkdown(job.last_message);
      body.append(prev);
    }
    return;
  }
  const md = el("div", { class: "md" });
  md.innerHTML = renderMarkdown(job.last_message || "_(no message)_");
  body.append(md);
  if (s === "error" && job.error) {
    body.append(el("pre", { class: "md", style: "font-size:12px;white-space:pre-wrap;color:var(--red)" }, job.error));
  }
}

function renderActivity() {
  const log = job.log || [];
  if (log.length === lastLogLen) return;
  lastLogLen = log.length;
  const list = $("activity");
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  list.innerHTML = "";
  for (const e of log) {
    let text = e.label || e.text || "";
    if (e.kind === "assistant" || e.kind === "user") text = text.length > 220 ? text.slice(0, 220) + "…" : text;
    if (e.kind === "status") text = { waiting: "waiting for you", done: "done", error: "error", cancelled: "paused" }[e.text] || e.text;
    list.append(el("div", { class: `act ${e.kind}` },
      el("time", {}, fmtTime(e.ts)), el("span", { class: "ic" }, ICON[e.kind] || "·"), el("span", { class: "tx" }, text)));
  }
  if (atBottom || lastLogLen === log.length) list.scrollTop = list.scrollHeight;
}

function renderVideo() {
  const renders = job.renders || [];
  const card = $("videoCard");
  if (!renders.length) { card.style.display = "none"; return; }
  card.style.display = "";
  const sel = $("renderSelect");
  const names = [...sel.options].map((o) => o.value).join("|");
  if (names !== renders.join("|")) {
    sel.innerHTML = "";
    for (const r of renders) sel.append(el("option", { value: r }, r));
    const preferred = renders.includes("final.mp4") ? "final.mp4" : renders[0];
    sel.value = currentRender && renders.includes(currentRender) ? currentRender : preferred;
    setRender(sel.value);
  }
}

function setRender(name) {
  if (name === currentRender) return;
  currentRender = name;
  const url = mediaURL(job.project_id, `renders/${name}`);
  $("video").src = url;
  $("downloadLink").href = url;
  $("downloadLink").setAttribute("download", `${job.project_id}-${name}`);
}

$("renderSelect").addEventListener("change", (e) => setRender(e.target.value));

async function refresh() {
  try {
    job = await getJSON(`/api/jobs/${encodeURIComponent(jobId)}`);
  } catch (err) {
    $("msgBody").textContent = `Production not found (${err.message}).`;
    return;
  }
  renderHeader();
  renderRail();
  renderMessage();
  renderActivity();
  renderVideo();
  if (lastStatus && lastStatus !== job.status && job.status !== "running" && document.hidden) {
    try { new Notification("OwlCut", { body: "The agent is waiting for your answer." }); } catch { /* notifications unavailable */ }
  }
  lastStatus = job.status;
}

async function send(text) {
  if (busy) return;
  busy = true;
  $("replyError").textContent = "";
  for (const b of ["approveBtn", "changesBtn"]) $(b).disabled = true;
  try {
    await postJSON(`/api/jobs/${encodeURIComponent(jobId)}/reply`, { text });
    $("replyText").value = "";
    lastMsg = null;
    await refresh();
  } catch (err) {
    $("replyError").textContent = err.message;
  } finally {
    busy = false;
    for (const b of ["approveBtn", "changesBtn"]) $(b).disabled = false;
  }
}

$("approveBtn").addEventListener("click", () => {
  const extra = $("replyText").value.trim();
  if (job.status === "waiting") {
    send(extra ? `Approved. Note: ${extra}` : "Approved. Go ahead to the next stage.");
  } else {
    send(extra || "Continue de onde parou.");
  }
});

$("changesBtn").addEventListener("click", () => {
  const text = $("replyText").value.trim();
  if (!text) {
    $("replyError").textContent = "Describe the change you want.";
    $("replyText").focus();
    return;
  }
  send(text);
});

$("cancelBtn").addEventListener("click", async () => {
  if (!confirm("Pause the agent now? You can continue later by sending a message.")) return;
  try { await postJSON(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {}); } catch (err) { alert(err.message); }
  refresh();
});

if ("Notification" in window && Notification.permission === "default") {
  document.addEventListener("click", () => Notification.requestPermission().catch(() => {}), { once: true });
}

refresh();
setInterval(refresh, 2500);
