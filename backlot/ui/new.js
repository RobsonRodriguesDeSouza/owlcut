import { el, getJSON } from "/ui/lib.js";
import { mountThemeToggle, postJSON } from "/ui/create-lib.js";

mountThemeToggle(document.getElementById("backLink"));

const form = document.getElementById("form");
const errorEl = document.getElementById("error");
const submitBtn = document.getElementById("submit");

const SELECTS = {
  pipeline: { key: "pipelines", def: "animated-explainer" },
  duration: { key: "durations", def: "45" },
  aspect: { key: "aspects", def: "16:9" },
  language: { key: "languages", def: "en-US" },
  voice: { key: "voices", def: "edge:en-US-AndrewMultilingualNeural" },
  music: { key: "music", def: "original" },
  tone: { key: "tones", def: "educational" },
  captions: { key: "captions", def: "phrases" },
  engine: { key: "engines", def: "auto" },
  approvals: { key: "approvals", def: "gates" },
};
const SUMMARY = [
  ["pipeline", "Type"], ["duration", "Length"], ["aspect", "Format"], ["voice", "Voice"],
  ["music", "Music"], ["approvals", "Approvals"],
];

let opts = null;

function fill(select, items, def) {
  select.innerHTML = "";
  for (const it of items) {
    select.append(el("option", { value: String(it.value) }, it.label));
  }
  if (items.some((it) => String(it.value) === def)) select.value = def;
}

function updateSummary() {
  const box = document.getElementById("summary");
  box.innerHTML = "";
  for (const [name, label] of SUMMARY) {
    const sel = form.elements[name];
    const text = sel.options[sel.selectedIndex]?.textContent || "—";
    box.append(el("div", {}, el("span", {}, label), el("span", {}, text.split(" (")[0].split(" — ")[0])));
  }
  const p = opts.pipelines.find((x) => x.value === form.elements.pipeline.value);
  document.getElementById("pipelineHelp").textContent = p ? p.description : "";
}

async function init() {
  opts = await getJSON("/api/options");
  for (const [name, cfg] of Object.entries(SELECTS)) fill(form.elements[name], opts[cfg.key], cfg.def);
  if (!opts.claude_available) {
    document.getElementById("claudeMissing").style.display = "block";
    submitBtn.disabled = true;
  }
  form.addEventListener("change", updateSummary);
  updateSummary();
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorEl.textContent = "";
  const data = Object.fromEntries(new FormData(form).entries());
  data.duration = Number(data.duration);
  if (!data.topic || data.topic.trim().length < 3) {
    errorEl.textContent = "Describe the video topic.";
    form.elements.topic.focus();
    return;
  }
  submitBtn.disabled = true;
  submitBtn.textContent = "Iniciando…";
  try {
    const { id } = await postJSON("/api/jobs", data);
    location.href = `/job/${id}`;
  } catch (err) {
    errorEl.textContent = `Couldn't start: ${err.message}`;
    submitBtn.disabled = false;
    submitBtn.textContent = "▶ Make video";
  }
});

init().catch((err) => { errorEl.textContent = `Couldn't load the options: ${err.message}`; });
