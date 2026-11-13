"""Backlot jobs — start and steer productions from the browser.

A *job* is one production driven by a headless Claude Code session
(``claude -p``). The browser form becomes a structured brief; the agent
follows AGENT_GUIDE.md exactly as it would in a chat, and at every human
gate it ends its turn. The job page then shows the agent's message with
Approve / Request-changes controls, and a reply resumes the same session
(``claude -p --resume <session>``).

Job state lives in ``.backlot/jobs/<job_id>/`` (job.json + log.jsonl), never
inside ``projects/`` — the agent owns the project directory.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Optional

import yaml

from backlot.state import PROJECTS_DIR, REPO_ROOT, _load_pipeline_meta, load_board_state

JOBS_DIR = REPO_ROOT / ".backlot" / "jobs"
PIPELINE_DIR = REPO_ROOT / "pipeline_defs"
HIDDEN_PIPELINES = {"framework-smoke"}
ALLOWED_TOOLS = "Bash Read Write Edit Glob Grep WebSearch WebFetch Skill TodoWrite NotebookEdit"
LOG_TAIL = 400

# Statuses: running | waiting (agent ended its turn, needs a reply) | done | error | cancelled | interrupted
ACTIVE = {"running"}


# ---------------------------------------------------------------------------
# Claude CLI discovery
# ---------------------------------------------------------------------------

def find_claude() -> Optional[str]:
    env = os.environ.get("OPENMONTAGE_CLAUDE_BIN")
    if env and Path(env).is_file():
        return env
    found = shutil.which("claude")
    if found:
        return found
    candidates = sorted(
        glob.glob(str(Path.home() / ".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude")),
        key=lambda p: Path(p).stat().st_mtime,
        reverse=True,
    )
    for extra in (Path.home() / ".claude/local/claude", Path.home() / ".local/bin/claude"):
        if extra.is_file():
            candidates.append(str(extra))
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Form options
# ---------------------------------------------------------------------------

PIPELINE_LABELS = {
    "animated-explainer": "Animated explainer (topic → explainer video)",
    "animation": "Animation",
    "character-animation": "Character animation",
    "cinematic": "Cinematic / trailer",
    "documentary-montage": "Documentary montage",
    "hybrid": "Hybrid (footage + graphics)",
    "screen-demo": "Screen demo / tutorial",
    "talking-head": "Talking head",
    "avatar-spokesperson": "Avatar spokesperson",
    "clip-factory": "Clip factory (long video → clips)",
    "podcast-repurpose": "Podcast → video",
    "localization-dub": "Dubbing / localization",
}


PIPELINE_DESC_PT = {
    "animated-explainer": "An explainer made from scratch: the agent researches the topic, proposes concepts, writes the script, narrates and animates the scenes.",
    "animation": "Motion-graphics animation driven by a short script, no footage.",
    "character-animation": "Animation with recurring characters, poses and staged scenes.",
    "cinematic": "A cinematic piece (trailer, teaser, opener) with strong pacing and score.",
    "documentary-montage": "Documentary montage with archive imagery, narration and a timeline.",
    "hybrid": "Your footage mixed with graphics, text and animation on top.",
    "screen-demo": "A tutorial or demo from a screen recording, with zooms, highlights and narration.",
    "talking-head": "Editing a person talking to camera: cuts, captions and inserts.",
    "avatar-spokesperson": "An avatar presents the content, with a script and supporting scenes.",
    "clip-factory": "Turns a long video into several short clips for social media.",
    "podcast-repurpose": "Turns a podcast or long audio into videos and clips.",
    "localization-dub": "Dubs or subtitles an existing video in another language.",
}


def _pipelines() -> list[dict[str, str]]:
    out = []
    for path in sorted(PIPELINE_DIR.glob("*.yaml")):
        if path.stem in HIDDEN_PIPELINES:
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        desc = " ".join(str(data.get("description", "")).split())
        out.append({
            "value": path.stem,
            "label": PIPELINE_LABELS.get(path.stem, path.stem),
            "description": PIPELINE_DESC_PT.get(path.stem, desc[:220]),
        })
    out.sort(key=lambda p: (p["value"] != "animated-explainer", p["label"]))
    return out


def _voices() -> list[dict[str, str]]:
    voices = [{"value": "auto", "label": "Let the agent choose"}]
    try:
        import edge_tts  # noqa: F401
        voices += [
            {"value": "edge:en-US-AndrewMultilingualNeural", "label": "Andrew — male, English (Edge TTS, free)"},
            {"value": "edge:en-US-AvaMultilingualNeural", "label": "Ava — female, English (Edge TTS, free)"},
            {"value": "edge:pt-BR-AntonioNeural", "label": "Antonio — male, Portuguese (Edge TTS, free)"},
            {"value": "edge:pt-BR-FranciscaNeural", "label": "Francisca — female, Portuguese (Edge TTS, free)"},
            {"value": "edge:pt-BR-ThalitaMultilingualNeural", "label": "Thalita — female, Portuguese (Edge TTS, free)"},
            {"value": "edge:es-MX-JorgeNeural", "label": "Jorge — male, Spanish (Edge TTS, free)"},
        ]
    except ImportError:
        pass
    for onnx in sorted((REPO_ROOT / ".runtime" / "piper").glob("*.onnx")):
        name = onnx.stem
        voices.append({"value": f"piper:{name}", "label": f"{name} (Piper, offline — more robotic)"})
    voices.append({"value": "none", "label": "No narration"})
    return voices


def _music() -> list[dict[str, str]]:
    music = [
        {"value": "original", "label": "Original track composed locally (free, royalty-free)"},
        {"value": "none", "label": "No music"},
        {"value": "pixabay", "label": "Search Pixabay (may be blocked)"},
        {"value": "auto", "label": "Let the agent choose"},
    ]
    lib = REPO_ROOT / "music_library"
    if lib.is_dir():
        for f in sorted(lib.iterdir()):
            if f.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg"} and not (
                f.suffix.lower() == ".wav" and f.with_suffix(".mp3").exists()
            ):
                music.append({"value": f"library:{f.name}", "label": f"From the library: {f.name}"})
    return music


def options() -> dict[str, Any]:
    return {
        "claude_available": bool(find_claude()),
        "pipelines": _pipelines(),
        "durations": [
            {"value": v, "label": l} for v, l in (
                (15, "15 seconds"), (30, "30 seconds"), (45, "45 seconds"), (60, "1 minute"),
                (90, "1 min 30 s"), (120, "2 minutes"), (180, "3 minutes"))
        ],
        "languages": [
            {"value": "en-US", "label": "English"},
            {"value": "pt-BR", "label": "Portuguese (Brazil)"},
            {"value": "es", "label": "Spanish"},
        ],
        "voices": _voices(),
        "music": _music(),
        "aspects": [
            {"value": "16:9", "label": "16:9 — landscape (YouTube)"},
            {"value": "9:16", "label": "9:16 — vertical (Reels, TikTok, Shorts)"},
            {"value": "1:1", "label": "1:1 — square (feed)"},
        ],
        "engines": [
            {"value": "auto", "label": "Let the agent choose"},
            {"value": "hyperframes", "label": "HyperFrames (HTML + GSAP)"},
            {"value": "remotion", "label": "Remotion (React)"},
        ],
        "tones": [
            {"value": "educational", "label": "Educational and clear"},
            {"value": "fun", "label": "Fun and light"},
            {"value": "cinematic", "label": "Cinematic and dramatic"},
            {"value": "corporate", "label": "Corporate and professional"},
            {"value": "inspiring", "label": "Inspiring"},
        ],
        "captions": [
            {"value": "phrases", "label": "Captions by phrase"},
            {"value": "none", "label": "No captions"},
        ],
        "approvals": [
            {"value": "gates", "label": "Approve every stage (recommended)"},
            {"value": "final", "label": "Only approve the final video"},
        ],
    }


# ---------------------------------------------------------------------------
# Brief → prompt
# ---------------------------------------------------------------------------

def _label(opts: list[dict], value: Any) -> str:
    for o in opts:
        if str(o["value"]) == str(value):
            return o["label"]
    return str(value)


def slugify(text: str, max_len: int = 48) -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-")
    return (slug[:max_len].rstrip("-")) or "video"


def unique_project_id(topic: str) -> str:
    base = slugify(topic)
    pid, n = base, 2
    while (PROJECTS_DIR / pid).exists() or (JOBS_DIR / pid).exists():
        pid = f"{base}-{n}"
        n += 1
    return pid


def build_prompt(form: dict[str, Any], project_id: str) -> str:
    o = options()
    voice = form.get("voice", "auto")
    music = form.get("music", "original")

    voice_line = _label(o["voices"], voice)
    if voice.startswith("edge:"):
        voice_line += (f". Generate it with edge-tts (already in the .venv: `edge-tts --voice {voice[5:]} "
                       f"--text ... --write-media ...`), no API key needed")
    elif voice.startswith("piper:"):
        voice_line += f". Use piper_tts with the model .runtime/piper/{voice[6:]}.onnx"

    music_line = _label(o["music"], music)
    if music == "original":
        music_line += (". Compose an original instrumental track locally (numpy, seeded), "
                       "save it to music_library/ and use it in the project; zero cost, no third-party licence")
    elif music.startswith("library:"):
        music_line = f"Use the file music_library/{music[8:]}"

    approvals = form.get("approvals", "gates")
    approval_text = (
        "Respect ALL of the pipeline's human approval gates."
        if approvals == "gates" else
        "The user pre-approved the intermediate stages: decide proposal, script, scene plan and assets "
        "yourself (log the decisions in decision_log with user_approved=false and the reason), "
        "but STOP at the final gate (publish) and ask for approval of the rendered video."
    )

    notes = (form.get("notes") or "").strip()
    lines = [
        "Production request from the OwlCut form (the OpenMontage web UI).",
        "",
        "## Brief",
        f"- Topic / request: {form['topic'].strip()}",
        f"- Video type (pipeline): {form.get('pipeline', 'animated-explainer')}",
        f"- Target length: {form.get('duration', 45)} seconds",
        f"- Language of narration and on-screen text: {_label(o['languages'], form.get('language', 'en-US'))}",
        f"- Voice: {voice_line}",
        f"- Music: {music_line}",
        f"- Format: {form.get('aspect', '16:9')}",
        f"- Render engine: {_label(o['engines'], form.get('engine', 'auto'))}",
        f"- Tone: {_label(o['tones'], form.get('tone', 'educational'))}",
        f"- Captions: {_label(o['captions'], form.get('captions', 'phrases'))}",
        f"- User notes: {notes or '(none)'}",
        "",
        "## Session rules",
        "1. Read AGENT_GUIDE.md before anything else and follow the chosen pipeline strictly "
        "(Rule Zero, director skills, checkpoints, schemas).",
        f"2. Use exactly the project_id `{project_id}` (folder projects/{project_id}/) and create "
        "project.json right away so OwlCut can show live progress.",
        f"3. {approval_text}",
        "4. You are running without an interactive terminal: the AskUserQuestion tool is NOT available. "
        "At each gate (or whenever you need a user decision), write the checkpoint as "
        "awaiting_human and END the turn with a short, self-contained final message in English: "
        "what was done, what needs approval (with file paths to review) and the options. "
        "The user's answer arrives as the next message in this session.",
        "5. Never replace choices from the brief on your own. If something blocks (a tool failed, "
        "a key is missing), stop and explain the alternatives in the final message.",
        "6. Activate the venv (`source .venv/bin/activate`) and run scripts with `PYTHONPATH=.` from the repo root.",
        f"7. Final render goes to projects/{project_id}/renders/final.mp4. After the publish gate, "
        "finish with a final summary and the video path.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Job manager
# ---------------------------------------------------------------------------

class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        self._recover()

    # ---- persistence ----------------------------------------------------

    def _dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,80}", job_id or ""):
            raise KeyError(job_id)
        return JOBS_DIR / job_id

    def _load(self, job_id: str) -> dict[str, Any]:
        path = self._dir(job_id) / "job.json"
        if not path.is_file():
            raise KeyError(job_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, job: dict[str, Any]) -> None:
        job["updated_at"] = time.time()
        d = self._dir(job["id"])
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(d / "job.json")

    def _log(self, job_id: str, entry: dict[str, Any]) -> None:
        entry.setdefault("ts", time.time())
        with open(self._dir(job_id) / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _recover(self) -> None:
        """Jobs left 'running' by a previous server process can be resumed by a reply."""
        for path in JOBS_DIR.glob("*/job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("status") in ACTIVE:
                job["status"] = "interrupted"
                job["last_message"] = (job.get("last_message") or "") + (
                    "\n\n_The OwlCut server restarted during this turn. "
                    "Send a message (e.g. \"continue\") to resume._")
                self._save(job)

    # ---- queries --------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        jobs = []
        for path in JOBS_DIR.glob("*/job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            jobs.append({k: job.get(k) for k in (
                "id", "project_id", "kind", "title", "status", "created_at", "updated_at", "cost_usd", "turns")})
        jobs.sort(key=lambda j: j.get("updated_at") or 0, reverse=True)
        return jobs

    def get(self, job_id: str) -> dict[str, Any]:
        job = self._load(job_id)
        log_path = self._dir(job_id) / "log.jsonl"
        entries = []
        if log_path.is_file():
            lines = log_path.read_text(encoding="utf-8").splitlines()[-LOG_TAIL:]
            for line in lines:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        job["log"] = entries
        renders = PROJECTS_DIR / job["project_id"] / "renders"
        job["renders"] = sorted(
            (p.name for p in renders.glob("*.mp4")),
            key=lambda n: (renders / n).stat().st_mtime, reverse=True,
        ) if renders.is_dir() else []
        project_dir = PROJECTS_DIR / job["project_id"]
        job["project_exists"] = project_dir.is_dir()
        job["stages"] = _stages_for(job, project_dir)
        return job

    # ---- commands -------------------------------------------------------

    def create(self, form: dict[str, Any]) -> dict[str, Any]:
        claude = find_claude()
        if not claude:
            raise RuntimeError("Claude Code CLI not found. Set OPENMONTAGE_CLAUDE_BIN to the binary path.")
        kind = "clips" if form.get("kind") == "clips" else "video"
        source = None
        if kind == "clips":
            from backlot import clipper  # local import keeps yt-dlp optional for /new
            source = clipper.probe(str(form.get("url") or ""))  # raises ValueError with a user-facing message
            if source.get("is_live"):
                raise ValueError("Live streams aren't supported yet; use the video once the stream has ended.")
            title = (form.get("title") or "").strip() or f"Clips — {source.get('title') or source['video_id']}"
            form = {**form, "pipeline": "clip-factory", "url": source["url"]}
        else:
            topic = (form.get("topic") or "").strip()
            if len(topic) < 3:
                raise ValueError("Describe the video topic.")
            title = form.get("title") or topic
        with self._lock:
            project_id = unique_project_id(title)
            job = {
                "id": project_id,
                "project_id": project_id,
                "kind": kind,
                "title": title[:120],
                "form": form,
                "source": source,
                "session_id": str(uuid.uuid4()),
                "status": "running",
                "created_at": time.time(),
                "turns": 0,
                "cost_usd": 0.0,
                "last_message": "",
            }
            self._save(job)
        if kind == "clips":
            prompt = clipper.build_prompt(form, project_id, source)
        else:
            prompt = build_prompt(form, project_id)
        self._log(job["id"], {"kind": "user", "text": prompt, "label": "Brief sent to the agent"})
        self._start_turn(job, prompt, first=True)
        return job

    def reply(self, job_id: str, text: str) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise ValueError("Mensagem vazia.")
        with self._lock:
            job = self._load(job_id)
            if job["status"] in ACTIVE or job_id in self._procs:
                raise RuntimeError("The agent is still working on this turn.")
            job["status"] = "running"
            self._save(job)
        self._log(job_id, {"kind": "user", "text": text})
        self._start_turn(job, text, first=False)
        return job

    def cancel(self, job_id: str) -> dict[str, Any]:
        proc = self._procs.get(job_id)
        if proc and proc.poll() is None:
            with self._lock:
                job = self._load(job_id)
                job["cancel_requested"] = True
                self._save(job)
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
        return self._load(job_id)

    # ---- process handling -----------------------------------------------

    def _start_turn(self, job: dict[str, Any], message: str, *, first: bool) -> None:
        claude = find_claude()
        cmd = [claude, "-p", message, "--output-format", "stream-json", "--verbose",
               "--permission-mode", "acceptEdits", "--allowedTools", *ALLOWED_TOOLS.split(),
               "--disallowedTools", "AskUserQuestion"]
        cmd += ["--session-id", job["session_id"]] if first else ["--resume", job["session_id"]]

        env = dict(os.environ)
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
            env.pop(k, None)
        venv_bin = REPO_ROOT / ".venv" / "bin"
        env["PATH"] = f"{venv_bin}{os.pathsep}/opt/homebrew/bin{os.pathsep}/usr/local/bin{os.pathsep}{env.get('PATH', '')}"
        env["VIRTUAL_ENV"] = str(REPO_ROOT / ".venv")

        proc = subprocess.Popen(
            cmd, cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
            start_new_session=True,
        )
        self._procs[job["id"]] = proc
        threading.Thread(target=self._pump, args=(job["id"], proc), daemon=True).start()

    def _pump(self, job_id: str, proc: subprocess.Popen) -> None:
        last_text, result = "", None
        stderr_tail: list[str] = []

        def read_err():
            for line in proc.stderr:  # type: ignore[union-attr]
                stderr_tail.append(line.rstrip())
                del stderr_tail[:-30]
        threading.Thread(target=read_err, daemon=True).start()

        for raw in proc.stdout:  # type: ignore[union-attr]
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type")
            if etype == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "text" and block.get("text", "").strip():
                        last_text = block["text"].strip()
                        self._log(job_id, {"kind": "assistant", "text": last_text})
                    elif block.get("type") == "tool_use":
                        self._log(job_id, {"kind": "tool", "text": _describe_tool(block)})
            elif etype == "result":
                result = ev

        proc.wait()
        self._procs.pop(job_id, None)
        with self._lock:
            job = self._load(job_id)
            job["turns"] = job.get("turns", 0) + 1
            if result:
                job["cost_usd"] = round(job.get("cost_usd", 0.0) + float(result.get("total_cost_usd") or 0.0), 4)
                if result.get("result"):
                    last_text = str(result["result"]).strip()
            job["last_message"] = last_text
            if job.pop("cancel_requested", False):
                job["status"] = "cancelled"
                job["last_message"] = (last_text + "\n\n" if last_text else "") + \
                    "_Turn cancelled. Send a message to pick up where it stopped._"
            elif proc.returncode != 0 or (result and result.get("is_error")):
                job["status"] = "error"
                detail = "\n".join(stderr_tail[-8:]) or (result or {}).get("result") or f"exit code {proc.returncode}"
                job["error"] = str(detail)[:2000]
            elif _publish_done(job["project_id"]):
                job["status"] = "done"
            else:
                job["status"] = "waiting"
            self._save(job)
        self._log(job_id, {"kind": "status", "text": job["status"]})


_STAGE_KEYS = ("name", "gated", "status", "timestamp", "human_approved", "error",
               "stalled", "stalled_minutes", "partial_progress", "versions", "undeclared")


def _stages_for(job: dict[str, Any], project_dir: Path) -> list[dict[str, Any]]:
    """Stage rail for the job page: live board state once the project exists,
    otherwise the pipeline manifest with every stage pending."""
    stages: list[dict[str, Any]] = []
    if (project_dir / "project.json").is_file():
        try:
            stages = [{k: st.get(k) for k in _STAGE_KEYS}
                      for st in load_board_state(project_dir).get("stages", [])]
        except Exception:
            stages = []
    if not stages:
        meta = _load_pipeline_meta((job.get("form") or {}).get("pipeline") or "animated-explainer")
        stages = [{"name": st["name"], "gated": st["gated"], "status": "pending"} for st in meta["stages"]]
    return stages


def _publish_done(project_id: str) -> bool:
    cp = PROJECTS_DIR / project_id / "checkpoint_publish.json"
    try:
        return json.loads(cp.read_text(encoding="utf-8")).get("status") == "completed"
    except (OSError, json.JSONDecodeError):
        return False


def _describe_tool(block: dict[str, Any]) -> str:
    name = block.get("name", "tool")
    inp = block.get("input") or {}
    if name == "Bash":
        detail = inp.get("description") or (inp.get("command") or "")[:120]
    elif name in ("Read", "Write", "Edit", "NotebookEdit"):
        detail = str(inp.get("file_path", "")).replace(str(REPO_ROOT) + "/", "")
    elif name in ("Glob", "Grep"):
        detail = inp.get("pattern", "")
    elif name in ("WebSearch",):
        detail = inp.get("query", "")
    elif name in ("WebFetch",):
        detail = inp.get("url", "")
    elif name == "Skill":
        detail = inp.get("skill", "")
    else:
        detail = ""
    return f"{name}: {detail}".strip().rstrip(":")
