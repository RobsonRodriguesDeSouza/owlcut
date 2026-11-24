import { el, getJSON, subscribe, thumbURL } from "/ui/lib.js";
import { postJSON } from "/ui/create-lib.js";
import { ICONS } from "/ui/shell.js";

const grid = document.getElementById("grid");
const staticSuffix = new URLSearchParams(location.search).has("static") ? "?static=1" : "";

const PIPELINE_SHORT = {
  "animated-explainer": "Animated explainer",
  "avatar-spokesperson": "Avatar",
  "clip-factory": "Clips",
  "podcast-repurpose": "Podcast",
  "talking-head": "Talking head",
  "screen-demo": "Screen demo",
  "documentary-montage": "Documentary",
  "cinematic": "Cinematic",
  "animation": "Animation",
  "character-animation": "Character",
  "hybrid": "Hybrid",
  "localization-dub": "Dubbing",
};

const STAGE_LABEL = {
  research: "research", proposal: "proposal", idea: "idea", script: "script", scene_plan: "scenes",
  assets: "assets", edit: "edit", compose: "render", publish: "publish",
};

function html(node, markup) { node.innerHTML = markup; return node; }

function ago(epochSeconds) {
  if (!epochSeconds) return "";
  const diff = Date.now() / 1000 - epochSeconds;
  if (diff < 90) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)} h ago`;
  const d = Math.round(diff / 86400);
  return d === 1 ? "yesterday" : `${d} days ago`;
}

function startTiles() {
  const row = document.getElementById("startRow");
  const tile = (href, icon, title, text) => {
    const a = el("a", { class: "start-tile", href });
    return html(a, `<span class="ic">${ICONS[icon]}</span><span><b>${title}</b><span>${text}</span></span><span class="go">${ICONS.arrow}</span>`);
  };
  row.append(
    tile("/clips", "scissors", "Clips from a long video", "Paste a YouTube link or upload a file and get the best moments as vertical clips with captions."),
    tile("/new", "plus", "New video from a topic", "Script, narration, scenes and render, made by the agent."),
  );
}

function miniRail(states) {
  const rail = el("div", { class: "mini-rail", "aria-hidden": "true" });
  for (const s of states) {
    const cls = s.status === "completed" ? "d"
      : s.status === "in_progress" ? "a"
      : s.status === "awaiting_human" ? "w" : "";
    rail.append(el("i", { class: cls, title: `${STAGE_LABEL[s.name] || s.name}` }));
  }
  return rail;
}

// projects whose production was cancelled from the Library (their checkpoints
// may still read "awaiting approval", which would wrongly say "Needs you")
let cancelledProjects = new Set();

function card(p) {
  const title = p.title || p.project_id;
  const poster = el("div", { class: "lib-poster" });
  if (p.poster) {
    poster.append(el("img", { src: thumbURL(p.project_id, p.poster, 640), loading: "lazy", alt: "" }));
  } else {
    poster.append(el("span", { class: "lp-txt" }, "No image yet"));
  }
  if (cancelledProjects.has(p.project_id)) {
    poster.append(el("span", { class: "lp-live lp-cancelled" }, "Cancelled"));
  } else if (p.awaiting_human) {
    poster.append(el("span", { class: "lp-live" }, el("span", { class: "dot" }), "Needs you"));
  } else if (p.live && p.active_stage) {
    poster.append(el("span", { class: "lp-live" }, el("span", { class: "dot" }),
      `In production · ${STAGE_LABEL[p.active_stage] || p.active_stage}`));
  }

  const n = (v, one, many) => `${v} ${v === 1 ? one : many}`;
  const meta = el("div", { class: "lb-meta" },
    el("span", { class: "chip" }, PIPELINE_SHORT[p.pipeline_type] || (p.pipeline_type && p.pipeline_type !== "unknown" ? p.pipeline_type : "Other")),
    p.scene_count ? el("span", { class: "chip" }, n(p.scene_count, "scene", "scenes")) : null,
    p.render_count ? el("span", { class: "chip" }, n(p.render_count, "render", "renders")) : null,
    el("span", { class: "when" }, ago(p.last_activity)),
  );

  return el("a", {
    class: `lib-card${p.live ? " live-card" : ""}`,
    href: `/p/${p.project_id}${staticSuffix}`,
    style: "text-decoration:none;color:inherit",
  },
    poster,
    el("div", { class: "lib-body" },
      el("h3", {}, title),
      meta,
      p.stage_states.length ? miniRail(p.stage_states) : null,
    ),
  );
}

const JOB_STATUS = { running: "Working", waiting: "Needs you", done: "Done", error: "Error", cancelled: "Paused", interrupted: "Interrupted", closed: "Cancelled", failed: "Error" };
const OPEN = ["running", "waiting", "error", "cancelled", "interrupted"];
const STATUS_HINT = {
  running: "The agent is working on it right now.",
  waiting: "Waiting for your review or reply.",
  error: "Stopped with an error.",
  cancelled: "Paused — reply on its page to continue.",
  interrupted: "Interrupted by a server restart — reply on its page to continue.",
};
const confirming = new Set();   // job ids whose Cancel is armed
const closing = new Set();      // job ids with a close request in flight

const jobHref = (j) => (j.kind === "quick" ? `/clips/quick/${j.id}` : j.kind === "clips" ? `/clips/${j.id}` : `/job/${j.id}`);

// local (no-Claude) clips jobs, shaped like agent jobs for the cards
function quickAsJob(q) {
  const p = q.progress || {};
  const pct = p.step === "render" && p.total ? p.done / p.total : null;
  return {
    id: q.id, kind: "quick", title: q.title, status: q.status === "running" ? "running" : q.status === "error" ? "failed" : q.status === "done" ? "done" : "closed",
    updated_at: q.updated_at, cost_usd: 0, source_name: "Local",
    stage: p.message || "Working", stages_total: pct != null ? p.total : 0, stages_done: pct != null ? p.done : 0,
    summary: q.status === "done" ? `${q.clip_count} clips ready.` : "", error: q.error || "",
  };
}

function money(v) { return v ? `$${Number(v).toFixed(2)}` : ""; }

function prodCard(j) {
  const total = j.stages_total || 0, done = j.stages_done || 0;
  const pct = total ? Math.round((done / total) * 100) : 0;
  const stageTxt = j.stage
    ? `${j.stage}${j.stage_status === "awaiting_human" ? " · awaiting your approval" : j.stage_status === "failed" ? " · failed" : ""}`
    : "Starting";
  const meta = el("div", { class: "prod-meta" },
    el("span", { class: "chip" }, j.kind === "quick" ? "Quick clips" : j.kind === "clips" ? "Clips" : "Video"),
    j.source_name ? el("span", { class: "chip" }, j.source_name) : null,
    el("span", { class: "when" }, ago(j.updated_at)),
    money(j.cost_usd) ? el("span", { class: "when" }, `${money(j.cost_usd)} spent`) : null,
  );

  const actions = el("div", { class: "prod-actions" });
  if (closing.has(j.id)) {
    actions.append(el("span", { class: "prod-note" }, j.status === "running" ? "Stopping the agent…" : "Cancelling…"));
  } else if (confirming.has(j.id)) {
    const yes = el("button", { class: "btn small danger", type: "button" }, "Yes, cancel");
    const no = el("button", { class: "btn small", type: "button" }, "Keep");
    yes.addEventListener("click", () => closeJob(j));
    no.addEventListener("click", () => { confirming.delete(j.id); renderJobs(); });
    actions.append(el("span", { class: "prod-note" },
      j.status === "running" ? "Stop the agent and cancel? Files are kept." : "Cancel this production? Files are kept."), no, yes);
    queueMicrotask(() => no.focus());
  } else {
    const cancel = el("button", { class: "btn small ghost", type: "button", "aria-label": `Cancel ${j.title || j.id}` }, "Cancel");
    cancel.addEventListener("click", () => { confirming.add(j.id); renderJobs(); });
    actions.append(cancel, el("a", { class: "btn small primary", href: jobHref(j) }, j.status === "waiting" ? "Review" : "Open"));
  }

  return el("article", { class: `prod-card s-${j.status}` },
    el("div", { class: "prod-top" },
      el("span", { class: `status ${j.status}` }, el("span", { class: "dot" }), JOB_STATUS[j.status] || j.status),
      el("a", { class: "prod-title", href: jobHref(j) }, j.title || j.id),
    ),
    meta,
    el("div", { class: "prod-stage" },
      el("div", { class: "prod-stage-txt" },
        el("span", {}, stageTxt),
        total ? el("span", { class: "mono" }, `${done}/${total} ${j.kind === "quick" ? "clips" : "stages"}`) : null),
      el("div", { class: "bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(pct), "aria-label": "Stages completed" },
        el("span", { style: `width:${pct}%` })),
    ),
    el("p", { class: "prod-msg" }, j.status === "error" && j.error ? j.error.split("\n")[0] : (j.summary || STATUS_HINT[j.status] || "")),
    actions,
  );
}

async function closeJob(j) {
  confirming.delete(j.id);
  closing.add(j.id);
  renderJobs();
  try {
    await postJSON(j.kind === "quick" ? `/api/quickclips/${encodeURIComponent(j.id)}/cancel` : `/api/jobs/${encodeURIComponent(j.id)}/close`, {});
  } catch (e) {
    alert(`Couldn't cancel: ${e.message}`);
  }
  // a running agent takes a moment to stop; keep polling until it's gone
  for (let i = 0; i < 20; i++) {
    const jobs = await fetchAll().catch(() => null);
    const cur = jobs && jobs.find((x) => x.id === j.id);
    if (!cur || !OPEN.includes(cur.status)) break;
    await new Promise((r) => setTimeout(r, 1000));
  }
  closing.delete(j.id);
  renderJobs();
}

const reopening = new Set();

function renderCancelled(list) {
  const sec = document.getElementById("cancelled");
  sec.hidden = list.length === 0;
  document.getElementById("cancelCount").textContent = list.length === 1 ? "1 production" : `${list.length} productions`;
  const ul = document.getElementById("cancelList");
  ul.innerHTML = "";
  list.sort((a, b) => (b.closed_at || b.updated_at || 0) - (a.closed_at || a.updated_at || 0));
  for (const j of list) {
    const reached = j.stage
      ? `${j.kind === "quick" ? "" : "Stopped at "}${j.stage}${j.stages_total ? ` · ${j.stages_done}/${j.stages_total} ${j.kind === "quick" ? "clips" : "stages"}` : ""}`
      : "";
    const actions = el("div", { class: "cancel-actions" });
    if (j.kind !== "quick") {
      const re = el("button", { class: "btn small", type: "button", "aria-label": `Reopen ${j.title || j.id}` },
        reopening.has(j.id) ? "Reopening…" : "Reopen");
      re.disabled = reopening.has(j.id);
      re.addEventListener("click", () => reopenJob(j));
      actions.append(re);
    }
    actions.append(el("a", { class: "btn small ghost", href: jobHref(j) }, "Open"));
    const del = el("button", { class: "btn small danger", type: "button", "aria-label": `Delete ${j.title || j.id}` }, "Delete");
    del.addEventListener("click", () => openDelete([j]));
    actions.append(del);
    ul.append(el("li", { class: `cancel-row${deleting.has(j.id) ? " deleting" : ""}` },
      el("div", { class: "cancel-main" },
        el("a", { class: "cancel-title", href: jobHref(j) }, j.title || j.id),
        el("div", { class: "cancel-meta" },
          el("span", { class: "chip" }, j.kind === "quick" ? "Quick clips" : j.kind === "clips" ? "Clips" : "Video"),
          el("span", {}, `cancelled ${ago(j.closed_at || j.updated_at)}`),
          reached ? el("span", {}, reached) : null,
          money(j.cost_usd) ? el("span", {}, `${money(j.cost_usd)} spent`) : null),
      ),
      actions,
    ));
  }
}

// ---- delete cancelled productions ----------------------------------------
const deleting = new Set();
let pendingDelete = [];
let lastCancelled = [];
const delURL = (j) => j.kind === "quick"
  ? `/api/quickclips/${encodeURIComponent(j.id)}/delete` : `/api/jobs/${encodeURIComponent(j.id)}/delete`;
function size(n) {
  if (!n) return "0 MB";
  return n >= 1073741824 ? `${(n / 1073741824).toFixed(1)} GB` : `${Math.max(0.1, n / 1048576).toFixed(1)} MB`;
}

async function openDelete(list) {
  pendingDelete = list;
  const dlg = document.getElementById("delDlg");
  const one = list.length === 1;
  document.getElementById("ddTitle").textContent = one ? `Delete “${list[0].title || list[0].id}”?` : `Delete ${list.length} cancelled productions?`;
  document.getElementById("ddSub").textContent = "Checking how much space this frees…";
  document.getElementById("ddError").textContent = "";
  document.getElementById("ddExports").checked = false;
  document.getElementById("ddExportRow").hidden = true;
  const ul = document.getElementById("ddList");
  ul.innerHTML = "";
  const go = document.getElementById("ddGo");
  go.disabled = true;
  go.textContent = "Delete";
  dlg.showModal();
  const reports = await Promise.all(list.map((j) =>
    postJSON(delURL(j), { dry_run: true }).catch((e) => ({ id: j.id, error: e.message }))));
  const byId = Object.fromEntries(reports.map((r) => [r.id, r]));
  let plain = 0, withExp = 0, exports = 0;
  for (const j of list) {
    const r = byId[j.id] || {};
    plain += r.bytes || 0;
    withExp += r.with_exports_bytes || r.bytes || 0;
    if (r.export) exports += 1;
  }
  const paint = () => {
    const inc = document.getElementById("ddExports").checked;
    document.getElementById("ddSub").textContent = `Frees about ${size(inc ? withExp : plain)} on your Mac.` +
      (exports && !inc && withExp - plain > 1048576
        ? ` The finished videos are also in OwlCutVideos (same file, no extra space) — tick the box below to free ${size(withExp)}.` : "");
    ul.innerHTML = "";
    for (const j of list) {
      const r = byId[j.id] || {};
      const n = inc ? (r.with_exports_bytes || r.bytes) : r.bytes;
      ul.append(el("li", {}, el("span", {}, j.title || j.id), el("span", { class: "mono" }, r.error ? r.error : size(n))));
    }
  };
  if (exports) {
    document.getElementById("ddExportRow").querySelector("b").previousSibling.textContent =
      list.length === 1 ? "Also delete its folder in " : "Also delete their folders in ";
    document.getElementById("ddExportRow").hidden = false;
    document.getElementById("ddExportInfo").textContent = exports === list.length && exports === 1 ? "" : `(${exports} of ${list.length} have one)`;
    document.getElementById("ddExports").onchange = paint;
  }
  paint();
  go.disabled = false;
  go.textContent = one ? "Delete" : `Delete ${list.length}`;
}

async function runDelete() {
  const list = pendingDelete;
  const includeExports = document.getElementById("ddExports").checked;
  const go = document.getElementById("ddGo");
  go.disabled = true;
  go.textContent = "Deleting…";
  list.forEach((j) => deleting.add(j.id));
  renderCancelled(lastCancelled);
  const failed = [];
  for (const j of list) {
    try { await postJSON(delURL(j), { include_exports: includeExports }); }
    catch (e) { failed.push(`${j.title || j.id}: ${e.message}`); }
    deleting.delete(j.id);
  }
  if (failed.length) {
    document.getElementById("ddError").textContent = failed.join(" · ");
    go.disabled = false;
    go.textContent = "Try again";
    pendingDelete = list.filter((j) => failed.some((f) => f.startsWith(`${j.title || j.id}:`)));
  } else {
    document.getElementById("delDlg").close();
  }
  await render().catch(console.error);
}

async function reopenJob(j) {
  reopening.add(j.id);
  renderJobs();
  try {
    await postJSON(`/api/jobs/${encodeURIComponent(j.id)}/reopen`, {});
  } catch (e) {
    alert(`Couldn't reopen: ${e.message}`);
  }
  reopening.delete(j.id);
  renderJobs();
}

let lastJobs = [];
async function fetchAll() {
  const [jobs, quick] = await Promise.all([getJSON("/api/jobs"), getJSON("/api/quickclips").catch(() => [])]);
  const dayAgo = Date.now() / 1000 - 86400;
  // running local jobs, plus errors from the last day (older ones are just history)
  const q = quick.filter((x) => x.status === "running" || x.status === "cancelled" || ((x.updated_at || 0) > dayAgo)).map(quickAsJob);
  return [...jobs, ...q].sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
}
async function renderJobs() {
  let jobs = lastJobs;
  try { jobs = lastJobs = await fetchAll(); } catch { /* keep last */ }
  const open = jobs.filter((j) => OPEN.includes(j.status) || closing.has(j.id));
  const list = document.getElementById("prodList");
  document.getElementById("inprog").hidden = open.length === 0;
  document.getElementById("inprogCount").textContent = open.length === 1 ? "1 production" : `${open.length} productions`;
  list.innerHTML = "";
  for (const j of open) list.append(prodCard(j));

  lastCancelled = jobs.filter((j) => j.status === "closed" && !closing.has(j.id));
  renderCancelled(lastCancelled);
  const nextCancelled = new Set(jobs.filter((j) => j.status === "closed" && j.kind !== "quick").map((j) => j.project_id || j.id));
  const changed = nextCancelled.size !== cancelledProjects.size || [...nextCancelled].some((x) => !cancelledProjects.has(x));
  cancelledProjects = nextCancelled;
  if (changed && document.querySelector(".lib-card")) render().catch(console.error);   // refresh the badges

  // recently finished — quick links, last 24 h (cancelled ones have their own list)
  const box = document.getElementById("jobs");
  const dayAgo = Date.now() / 1000 - 86400;
  const recent = jobs.filter((j) => !OPEN.includes(j.status) && j.status !== "closed" && (j.updated_at || 0) > dayAgo).slice(0, 6);
  box.innerHTML = "";
  for (const j of recent) {
    box.append(el("a", { class: "job-pill", href: jobHref(j) },
      el("span", { class: `status ${j.status}` }, el("span", { class: "dot" }), JOB_STATUS[j.status] || j.status),
      el("span", {}, j.title || j.id)));
  }
}

async function render() {
  await renderJobs();
  const projects = await getJSON("/api/projects");
  document.getElementById("count").textContent = projects.length === 1 ? "1 project" : `${projects.length} projects`;
  grid.innerHTML = "";
  document.getElementById("empty").style.display = projects.length ? "none" : "block";
  for (const p of projects) grid.append(card(p));
}

startTiles();
render().catch(console.error);
document.getElementById("ddGo").addEventListener("click", runDelete);
document.getElementById("deleteAllBtn").addEventListener("click", () => { if (lastCancelled.length) openDelete(lastCancelled); });
if (!staticSuffix) {
  subscribe("/api/library/events", () => render().catch(console.error));
  // job status lives outside projects/, so poll it (paused while a Cancel is armed or the tab is hidden)
  setInterval(() => {
    if (!document.hidden && !confirming.size && !closing.size && !deleting.size && !document.getElementById("delDlg").open) renderJobs();
  }, 5000);
}
