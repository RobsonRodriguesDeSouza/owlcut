# OwlCut usage guide

This guide covers installing OwlCut, each screen of its web app, the clip style kit, configuration and troubleshooting.

- [1. Install](#1-install)
- [2. Start the app](#2-start-the-app)
- [3. Make clips from a YouTube video](#3-make-clips-from-a-youtube-video)
- [4. Review and trim the clips](#4-review-and-trim-the-clips)
- [5. Publish](#5-publish)
- [6. Make a ranking video](#6-make-a-ranking-video)
- [7. Make a video from a topic](#7-make-a-video-from-a-topic)
- [8. The clip style kit](#8-the-clip-style-kit)
- [9. Configuration](#9-configuration)
- [10. Troubleshooting](#10-troubleshooting)
- [11. For developers](#11-for-developers)

---

## 1. Install

### Requirements

| Tool | Version | Why |
|---|---|---|
| Python | 3.10+ (3.11+ recommended) | OpenMontage tools and the OwlCut server |
| Node.js | 22+ | HyperFrames rendering (captions, intros) and Remotion |
| FFmpeg | full build with `libass`, `drawtext`, `libx264` | cutting, reframing, burning text |
| Deno | any recent | yt-dlp needs a JavaScript runtime to read YouTube |
| Claude Code CLI | signed in | the agent that runs every production |
| `uv` | optional | faster installs; used automatically when present |

On macOS with Homebrew:

```bash
brew install python@3.11 node deno ffmpeg-full uv
```

Homebrew's plain `ffmpeg` formula is a minimal build without `drawtext`/`libass`. OwlCut automatically puts the keg-only `ffmpeg-full` first on its `PATH` (see [Configuration](#8-configuration)), so you don't need to unlink anything.

### Project setup

```bash
git clone <your-fork-url> OwlCut
cd OwlCut
make setup          # venv, Python deps, Remotion, Piper TTS, HyperFrames cache, .env from .env.example
make setup-clips    # yt-dlp, youtube-transcript-api, faster-whisper, mediapipe 0.10.14, opencv < 5
```

`make setup-clips` pins `mediapipe==0.10.14` and `opencv-python-headless<5` on purpose: newer releases removed the APIs that face tracking uses.

API keys in `.env` are optional. The clips workflow runs fully locally; keys only unlock extra providers (image/video generation, premium voices) for the **New video** flow.

## 2. Start the app

```bash
make owlcut         # or: .venv/bin/python -m backlot serve
```

Open <http://127.0.0.1:4750>. The app bar at the top is the same on every page:

- **Library**: all projects and recent productions.
- **New video**: make a video from a topic.
- **Clips**: make clips from a YouTube video.
- **Publish**: publishing kits for finished clips.
- On the right, the production status (it links to whatever needs you) and the light/dark theme switch.

The server only listens on `127.0.0.1`. Productions run as background Claude Code sessions; they keep going while the page is closed, but stop if the server stops (send "continue" afterwards to resume).

## 3. Make clips from a YouTube video

Open **Clips** (`/clips`).

### Step 1: the video

1. Paste a YouTube link. `youtube.com/watch?v=…`, `youtu.be/…` and `/shorts/…` all work; channels and playlists don't.
2. OwlCut reads the video's details without downloading it: title, channel, length, available captions and chapters.
3. **Range of interest** (optional):
   - On YouTube, right-click the video and choose **Copy video URL at current time**. When you paste that link, the first clip starts from that moment.
   - Or type a start and end (`12:30`, `1:02:03`), or click one of the video's chapters.

### Step 2: the clips

- **How many clips**: 3, 5, 8 or 10. The agent finds at least twice as many candidates and keeps the best.
- **Length of each clip**: Short (15–30 s), Medium (30–60 s) or Long (60–90 s).
- **Where you'll post**: TikTok, Instagram Reels and YouTube Shorts share one 9:16 render; Feed adds a 4:5 render; YouTube (landscape) adds 16:9.

### Step 3: the look

Pick the captions, the intro, the ending and the accent colour. The previews are real renders from the clip kit. See [section 7](#7-the-clip-style-kit) for details on each style.

### Step 4: control

- **Caption language**: the same language as the video, or a specific language.
- **Review**:
  - *Review the clips before rendering (recommended)*: the agent stops after ranking so you can choose and trim.
  - *Only review the finished videos*: the agent picks the clips itself and stops when they are rendered.
- **Notes**: anything the agent should know, such as the topic to focus on, what to avoid, or lines that must be included.

Click **Find the best clips**. You land on the review page while the agent works.

## 4. Review and trim the clips

The review page (`/clips/<job>`) updates by itself every few seconds.

**While the agent works** you see its progress through the stages: brief, transcript and candidates, framing, captions and assets, edit, render, publish. **Agent activity** shows every step it takes.

**When the candidates are ready**, the page has two columns:

- **Left:** the source video, with a timeline that marks every clip (highlighted clips are selected).
- **Right:** a card for each suggested clip, best score first. Each card shows:
  - its score (0–100) and its hook line;
  - **▶ Play segment**, which plays only that clip in the player on the left;
  - **Start / End** controls: `−0.5` and `+0.5` nudge the cut, and `here` uses the player's current position;
  - why the agent picked it, how it can be framed, and its transcript and per-criterion scores;
  - **Use**, which adds the clip to the selection or takes it out. Clips the agent rejected are listed too, so you can bring one back.

The bar at the bottom shows how many clips are selected and their total length. When the agent is waiting for you:

- **Approve and render N** sends your exact selection and trims to the agent.
- **Ask for something else** sends a free-form request, such as "find shorter, funnier moments".

**Finished clips** appear at the bottom with a player and a download button for each. After rendering you can still trim a clip and click **Re-render N trimmed**, or **Approve finished clips** to move on to publishing.

## 5. Publish

Open **Publish** in the app bar, or **Open publishing kit** on the review page (`/publish/<job>`).

- Pick a network tab. Your choice is remembered.
- For every clip you get its video and these fields, with live character counters:

  | Network | Title | Caption limit | Hashtags |
  |---|---|---|---|
  | TikTok | – | 2,200 | up to 30 |
  | Instagram Reels | – | 2,200 | up to 30 |
  | YouTube Shorts | up to 100 | 5,000 | up to 15 |
  | Feed (Instagram/LinkedIn) | – | 2,200 | up to 30 |
  | YouTube (landscape) | up to 100 | 5,000 | up to 15 |

- The agent writes a first draft in its publish stage. Your edits are saved when you leave a field and never overwrite the agent's file, or vice versa.
- **Copy caption + hashtags** and **Copy title** put the text on your clipboard. **Download video** and **Download cover** save the files.
- Tick **Posted** once a clip is live. The progress bar counts what is left for that network.
- **Download kit (.zip)** builds a package:

  ```
  kit-<project>.zip
  ├── TikTok/
  │   ├── 01-<clip-title>.mp4
  │   ├── 01-<clip-title>.txt        # caption + hashtags
  │   └── 01-<clip-title>-cover.jpg
  ├── YouTube Shorts/
  │   └── 01-<clip-title>.txt        # starts with "TITLE: …"
  └── CHECKLIST.txt                  # [x] already posted / [ ] still to post
  ```

Posting itself is manual for now. OwlCut doesn't hold any social network credentials.

## 6. Make a ranking video

**Ranking** (`/ranking`) joins several YouTube moments into one vertical video (1080×1920). A title stays fixed at the top for the whole video, and each moment gets a big **#N** badge with its name. It runs entirely on your machine with yt-dlp and ffmpeg, so it uses no Claude usage.

1. Open **Ranking**, type the header title (for example "Top 6 goals of the week") and click **Create ranking**.
2. For each slot, paste a YouTube link and click **Load**. Slot 1 is **#1**. Use ▲ ▼ to reorder slots. You can add up to 10.
3. Click **Open player**, play the video, and use **Mark now** to set the start and the end. You can also type the times (`1:02.5`). **▶ Play the cut** plays just that part. Each cut can be 1 to 90 seconds long.
4. Type the **name** shown under the number. Choose the **order**: countdown (#6 → #1) or top first. Pick an accent colour.
5. Click **Render video**. OwlCut downloads only the parts you marked, lays them out and joins them. Watch the result on the page or click **Download MP4**.

Everything autosaves. Rankings live in `.backlot/rankings/<id>/`.

## 7. Make a video from a topic

**New video** (`/new`) starts any OpenMontage pipeline from a form: topic, video type, length, format, language, narration voice (free Edge TTS voices or offline Piper), background music (an original track composed locally, your own file from `music_library/`, or none), tone, captions, render engine and approval mode.

The production page (`/job/<id>`) shows the stages, the agent's latest message, **Approve and continue** / **Request changes**, the activity log and the rendered video. **View storyboard** opens OpenMontage's live board for the project.

## 8. The clip style kit

The looks live in `styles/clips/` and are built with HyperFrames (HTML + GSAP, rendered frame by frame, so every render is identical).

| Group | Style | Look |
|---|---|---|
| Captions | **Highlight** | Bold caps; the spoken word gets a block in the accent colour (its text switches to dark on light accents). |
| | **Karaoke** | The line sits in a light pill; each word darkens as it is spoken; key words take the accent colour. |
| | **Clean** | White with a soft shadow, wiped in word by word. Calm; good for interviews. |
| | **Impact** | One big word at a time, slamming in from alternating sides; long or key words in the accent colour. |
| Intro | **Hook** | The clip's hook line blurs in at the top over a soft shade for ~2.4 s. |
| | **Rising letters** | The hook in big condensed letters that rise one by one. |
| | **Title card** | White card with a colour tab: clip title and channel name. |
| Ending | **Call to action** | A pill in the accent colour for the last ~2 s (e.g. "Follow for part 2"). |

Layout rules for 9:16: captions sit at about 66 % of the height, with a wider right margin, so they never sit under the like/comment/share buttons or the caption area at the bottom.

## 9. Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENMONTAGE_CLAUDE_BIN` | auto | Path to the Claude Code CLI. Without it, OwlCut tries `PATH`, then the VS Code extension's bundled binary, then `~/.claude/local/claude`. |
| `OPENMONTAGE_FFMPEG_DIR` | auto | Folder with a full FFmpeg build to put first on the server's `PATH`. By default, Homebrew's `ffmpeg-full` (`/opt/homebrew/opt/ffmpeg-full/bin` or `/usr/local/opt/ffmpeg-full/bin`). |
| `BACKLOT_PORT` | `4750` | Port of the web app. |

Where things are stored:

| Path | Content |
|---|---|
| `projects/<id>/` | Each production: source video, transcript, clip renders, covers, artifacts. Git-ignored. |
| `projects/<id>/clips/clips.json` | The clip list the agent maintains (the review page reads it). |
| `projects/<id>/clips/publish_edits.json` | Your publishing edits and posted marks. |
| `.backlot/jobs/<id>/` | Agent session state and activity log. Git-ignored. |

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| "Claude Code CLI was not found" | Install Claude Code and sign in, or set `OPENMONTAGE_CLAUDE_BIN`. |
| "Couldn't read this video" | Check that the video is public and the link is correct. YouTube needs Deno installed for yt-dlp; update yt-dlp with `make setup-clips`. |
| "This video is private or requires a login" | OwlCut only works with public videos. |
| Captions or titles missing from renders; FFmpeg errors mention `drawtext`/`subtitles` | Install a full FFmpeg build (`brew install ffmpeg-full`) or set `OPENMONTAGE_FFMPEG_DIR`. |
| Face tracking errors (`mp.solutions`, `CascadeClassifier`) | Run `make setup-clips` again to restore the pinned versions. |
| A production says "The OwlCut server restarted during this turn" | Open it and send `continue`. |
| yt-dlp warns that Python 3.10 is deprecated | It still works; recreate the venv with Python 3.11+ when convenient. |
| The style previews look outdated after editing the kit | `.venv/bin/python -m styles.clips.build previews` |

## 11. For developers

### Tests

```bash
make test                                   # full OpenMontage suite
.venv/bin/python -m pytest tests/backlot    # OwlCut server, UI contracts, clips and publishing
```

Some upstream tests call `ffmpeg` and `python` from `PATH`. Run them with the full FFmpeg build and the venv first on `PATH`.

### The `clips.json` contract

The agent writes `projects/<id>/clips/clips.json` (atomically) and keeps it up to date at every stage. Times are seconds in the source video. File paths are relative to the project folder:

```json
{
  "version": 1,
  "source": { "url": "…", "title": "…", "channel": "…", "duration_seconds": 1800,
              "video_path": "assets/video/source.mp4" },
  "status": "analyzing | awaiting_selection | rendering | awaiting_review | published",
  "clips": [{
    "id": "c01", "title": "…", "hook": "…", "start": 123.4, "end": 158.9,
    "score": 87, "scores": { "hook": 9, "coherence": 8, "value": 9, "energy": 8, "platform_fit": 9 },
    "reason": "…", "transcript": "…", "crop": "centred face",
    "status": "candidate | selected | rejected | rendered",
    "renders": { "9:16": "renders/clips/c01_9x16.mp4" },
    "thumbnail": "renders/clips/c01_cover.jpg",
    "publish": { "tiktok": { "caption": "…", "hashtags": ["#…"] } }
  }]
}
```

`backlot/clipper.py` (`load_clips`) validates it on read: it drops clips with invalid times and ignores paths that are absolute, leave the project (`..`) or don't exist.

### Building a clip with the kit

```python
from styles.clips.build import build_clip_project

build_clip_project(
    "projects/demo/hyperframes/c01_9x16",
    video="projects/demo/assets/video/c01_9x16_raw.mp4",   # trimmed + reframed clip
    duration=31.2,
    words=[{"text": "Nobody", "start": 0.12, "end": 0.48}, ...],  # seconds from clip start
    caption="highlight",
    intro={"style": "hook", "text": "The secret is in the first 3 seconds"},
    outro={"style": "cta", "text": "Follow for part 2"},
    accent="#ffd400",
    keywords=["secret"],
)
```

```bash
npx --yes hyperframes@0.9.41 render projects/demo/hyperframes/c01_9x16 \
    -o projects/demo/renders/clips/c01_9x16.mp4 --quality delivery
```

Encode the raw clip with dense keyframes (`-g 30`) so seeking stays fast. For 4:5 pass `width=1080, height=1350`. To add a style, extend `STYLES` in `styles/clips/build.py` and the matching branch in `kit/clip-kit.js` / `kit/clip-kit.css`, then regenerate the previews.

### HTTP API (local only)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/clips/options` | Form options and style catalogue |
| POST | `/api/clips/probe` | `{url}` → video details (no download) |
| POST | `/api/jobs` | `{kind: "clips", url, …}` starts a clips production |
| GET | `/api/clips/<job>` | Job status + normalised `clips.json` |
| POST | `/api/jobs/<job>/reply` | `{text}` sends a message to the agent |
| GET | `/api/publish` | Publishing kits list |
| GET / POST | `/api/publish/<job>` · `/api/publish/<job>/<clip>/<platform>` | Kit state · save `{title, caption, hashtags, posted}` |
| GET | `/api/publish/<job>/kit.zip` | Export bundle |

Write endpoints require a JSON body and a same-origin `Origin`/`Referer`, because a production runs an agent with shell access.
