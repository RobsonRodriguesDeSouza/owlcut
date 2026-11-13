import { el, getJSON, subscribe, thumbURL } from "/ui/lib.js";
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
    tile("/clips", "scissors", "Clips from a YouTube video", "Paste a link and get the best moments as vertical clips with captions."),
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

function card(p) {
  const title = p.title || p.project_id;
  const poster = el("div", { class: "lib-poster" });
  if (p.poster) {
    poster.append(el("img", { src: thumbURL(p.project_id, p.poster, 640), loading: "lazy", alt: "" }));
  } else {
    poster.append(el("span", { class: "lp-txt" }, "No image yet"));
  }
  if (p.awaiting_human) {
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

const JOB_STATUS = { running: "Working", waiting: "Needs you", done: "Done", error: "Error", cancelled: "Paused", interrupted: "Interrupted" };

async function renderJobs() {
  const box = document.getElementById("jobs");
  let jobs = [];
  try { jobs = await getJSON("/api/jobs"); } catch { return; }
  const dayAgo = Date.now() / 1000 - 86400;
  const recent = jobs.filter((j) => ["running", "waiting"].includes(j.status) || (j.updated_at || 0) > dayAgo).slice(0, 6);
  box.innerHTML = "";
  for (const j of recent) {
    const href = j.kind === "clips" ? `/clips/${j.id}` : `/job/${j.id}`;
    box.append(el("a", { class: "job-pill", href },
      el("span", { class: `status ${j.status}` }, el("span", { class: "dot" }), JOB_STATUS[j.status] || j.status),
      el("span", {}, j.title || j.id)));
  }
}

async function render() {
  renderJobs();
  const projects = await getJSON("/api/projects");
  document.getElementById("count").textContent = projects.length === 1 ? "1 project" : `${projects.length} projects`;
  grid.innerHTML = "";
  document.getElementById("empty").style.display = projects.length ? "none" : "block";
  for (const p of projects) grid.append(card(p));
}

startTiles();
render().catch(console.error);
if (!staticSuffix) subscribe("/api/library/events", () => render().catch(console.error));
