// OwlCut app shell — the bar every page shares: brand, tools, job status, theme.
// Pages load it as a module; it injects itself at the top of <body>.

const THEME_KEY = "backlot.theme";

const svg = (paths, extra = "") =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ${extra}>${paths}</svg>`;

export const ICONS = {
  library: svg('<rect x="3" y="4" width="18" height="14" rx="2.5"/><path d="M8 21h8"/><path d="m10 8.5 4.5 2.5-4.5 2.5z"/>'),
  plus: svg('<path d="M12 5v14M5 12h14"/>'),
  scissors: svg('<circle cx="6" cy="7" r="2.6"/><circle cx="6" cy="17" r="2.6"/><path d="M8.2 8.4 20 17M8.2 15.6 20 7"/>'),
  send: svg('<path d="M21 3 10 14"/><path d="m21 3-7 18-4-7-7-4z"/>'),
  sun: svg('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
  moon: svg('<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>'),
  arrow: svg('<path d="M5 12h14M13 6l6 6-6 6"/>'),
  youtube: svg('<rect x="2.5" y="5.5" width="19" height="13" rx="4"/><path d="m10.5 9.5 4 2.5-4 2.5z" fill="currentColor"/>'),
  play: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2 1.2v9.6L10.4 6z" fill="currentColor"/></svg>',
  // OwlCut mark: an owl face — ear tufts, two eyes, a beak.
  owl: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5.5 7.5 8h9L20 5.5V14a8 8 0 0 1-16 0z" fill="currentColor" opacity=".18"/><path d="M4 5.5 7.5 8h9L20 5.5V14a8 8 0 0 1-16 0z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><circle cx="8.8" cy="12.2" r="2.6" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="15.2" cy="12.2" r="2.6" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="8.8" cy="12.2" r="1" fill="currentColor"/><circle cx="15.2" cy="12.2" r="1" fill="currentColor"/><path d="m12 14.6-1 1.6h2z" fill="currentColor"/></svg>',
};

const NAV = [
  { href: "/", label: "Library", icon: "library", match: (p) => p === "/" || p.startsWith("/p/") },
  { href: "/new", label: "New video", short: "New", icon: "plus", match: (p) => p === "/new" || p.startsWith("/job/") },
  { href: "/clips", label: "Clips", icon: "scissors", match: (p) => p.startsWith("/clips") },
  { href: "/publish", label: "Publish", icon: "send", match: (p) => p.startsWith("/publish") },
];

export function getTheme() {
  const q = new URLSearchParams(location.search).get("theme");
  if (q === "light" || q === "dark") return q;
  try { return localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark"; } catch { return "dark"; }
}

export function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem(THEME_KEY, theme); } catch { /* storage blocked */ }
  window.dispatchEvent(new CustomEvent("backlot:theme", { detail: theme }));
}

function themeButton() {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "theme-toggle";
  const paint = () => {
    const light = getTheme() === "light";
    btn.innerHTML = light ? ICONS.moon : ICONS.sun;
    btn.setAttribute("aria-label", light ? "Use dark theme" : "Use light theme");
    btn.title = btn.getAttribute("aria-label");
  };
  btn.addEventListener("click", () => { setTheme(getTheme() === "light" ? "dark" : "light"); paint(); });
  paint();
  return btn;
}

function buildBar() {
  const path = location.pathname;
  const bar = document.createElement("header");
  bar.className = "appbar";
  const links = NAV.map((n) => {
    const cur = n.match(path) ? ' aria-current="page"' : "";
    const label = n.short
      ? `<span class="lbl-long">${n.label}</span><span class="lbl-short">${n.short}</span>`
      : n.label;
    return `<a href="${n.href}"${cur}>${ICONS[n.icon]}<span>${label}</span></a>`;
  }).join("");
  bar.innerHTML = `
    <div class="appbar-in">
      <a class="brand" href="/" aria-label="OwlCut home">
        <span class="brand-mark">${ICONS.owl}</span><span class="brand-name">OwlCut</span>
      </a>
      <nav class="nav" aria-label="Tools">${links}</nav>
      <span class="spacer"></span>
      <a class="appbar-status" id="appbarStatus" href="/"><span class="dot"></span><span class="lbl">No active productions</span></a>
    </div>`;
  bar.querySelector(".appbar-in").append(themeButton());
  return bar;
}

async function refreshStatus() {
  const el = document.getElementById("appbarStatus");
  if (!el) return;
  try {
    const res = await fetch("/api/jobs");
    if (!res.ok) return;
    const jobs = await res.json();
    const waiting = jobs.filter((j) => j.status === "waiting");
    const running = jobs.filter((j) => j.status === "running");
    const lbl = el.querySelector(".lbl");
    const jobHref = (j) => (j.kind === "clips" ? `/clips/${j.id}` : `/job/${j.id}`);
    el.classList.toggle("on", running.length > 0 && waiting.length === 0);
    el.classList.toggle("wait", waiting.length > 0);
    if (waiting.length) {
      lbl.textContent = waiting.length === 1 ? "1 production needs you" : `${waiting.length} productions need you`;
      el.href = jobHref(waiting[0]);
    } else if (running.length) {
      lbl.textContent = running.length === 1 ? "1 production running" : `${running.length} productions running`;
      el.href = jobHref(running[0]);
    } else {
      lbl.textContent = "No active productions";
      el.href = "/";
    }
  } catch { /* server restarting */ }
}

function mount() {
  // The module can be evaluated twice (page tag + a page script importing ICONS
  // under a different cache-busting URL); only the first one mounts.
  if (document.querySelector(".appbar")) return;
  document.documentElement.dataset.theme = getTheme();
  const main = document.querySelector("main, .wrap");
  if (main && !main.id) main.id = "conteudo";
  const skip = document.createElement("a");
  skip.className = "skip-link";
  skip.href = `#${main ? main.id : "conteudo"}`;
  skip.textContent = "Skip to content";
  document.body.prepend(skip, buildBar());
  refreshStatus();
  setInterval(refreshStatus, 8000);
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
else mount();
