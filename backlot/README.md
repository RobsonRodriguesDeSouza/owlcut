# Backlot — the living storyboard

A read-only local board that shows a production happening: pipeline stages
lighting up, the script as a screenplay page, the scene plan as a filmstrip
that fills in as assets generate, decisions, spend, and activity — all
derived from what the pipeline already writes to `projects/<id>/`.

```bash
python -m backlot open <project-id>   # start server if needed + open browser
python -m backlot open                # library view (all projects)
python -m backlot serve --port 4750   # run the server in the foreground
```

Backlot binds to `127.0.0.1` and accepts HTTP requests only for `127.0.0.1`
or `localhost`, on the selected port. Other `Host` headers receive HTTP 400
to protect local project data from DNS rebinding.

## How it stays live

No agent involvement. A `watchfiles` watcher on `projects/` publishes change
notifications over SSE; the browser refetches board state. State sources:

| Board element | Disk source |
|---|---|
| identity / rail order | `project.json` + `pipeline_defs/<type>.yaml` |
| stage states, gates, versions | `checkpoint_<stage>.json` + `history/` |
| script card / modal | `artifacts/script.json` |
| filmstrip cards | `scene_plan × script × asset_manifest` join |
| generating shimmer, activity | `events.jsonl` (written by `BaseTool` instrumentation) |
| cost meter | checkpoint `cost_snapshot` |
| renders | `renders/*.mp4` (+ root-level mp4 heuristic) |

Projects without checkpoints degrade gracefully to a "what the watcher
found" view — media, snapshots, renders.

**Replay**: a completed run can be scrubbed end-to-end (▶ REPLAY RUN on the
board) — reconstructed from checkpoint history and event timestamps.

Try it without a real production:

```bash
python scripts/backlot_simulate_run.py          # live demo run (~1 min)
python -m backlot open backlot-demo-run
```

Design doc: `internal/design/LIVING_STORYBOARD.md`.

## Creating videos from the browser (`/new`)

OwlCut (this UI) can also *start* productions. **New video** in the app bar opens a
form (topic, pipeline, duration, format, language, voice, music, tone,
captions, engine, approval mode). Submitting it launches a headless Claude
Code session (`claude -p`, see `backlot/jobs.py`) that follows
`AGENT_GUIDE.md` like a chat session would. At each human gate the agent ends
its turn; `/job/<id>` shows its message with **Approve** / **Request changes**,
and a reply resumes the same session (`--resume`).

- Job state: `.backlot/jobs/<id>/job.json` + `log.jsonl` (never inside `projects/`).
- CLI discovery: `OPENMONTAGE_CLAUDE_BIN`, then `PATH`, then the VS Code extension's bundled binary.
- Write endpoints require JSON + a same-origin `Origin`/`Referer`, since a job runs an agent with shell access.

## Clips from YouTube (`/clips`) and publishing (`/publish`)

- `/clips` — paste a YouTube link; `backlot/clipper.py` probes it and starts a `clip-factory`
  job (`kind: "clips"`). The agent keeps `projects/<id>/clips/clips.json` up to date
  (the contract is written into its brief), and `/clips/<job>` reads it to show the
  candidates on a timeline with trimming, selection and approval.
- `/publish/<job>` — per-network captions, titles and hashtags (agent draft from
  `clips.json`, user edits in `clips/publish_edits.json`), "posted" tracking and a
  `.zip` export organised by network.
- Caption/intro/outro looks come from `styles/clips/` (see its README).
