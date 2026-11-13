# OwlCut clip kit — the look of vertical clips

Word-by-word captions, intros and an ending call to action for the clips made on OwlCut's `/clips` page.

- `kit/clip-kit.js` + `kit/clip-kit.css` — the engine (GSAP, deterministic, 9:16 and 4:5 with social safe zones).
- `build.py` — `STYLES` (the catalogue served to the form), `build_clip_project(...)` (the agent builds one
  HyperFrames project per clip with it) and `previews` (renders the previews the form shows).
- `previews/*.mp4` — previews rendered by the same engine. Regenerate after changing the kit:
  `.venv/bin/python -m styles.clips.build previews`
- `compositions/` — the original HyperFrames registry items the looks were adapted from
  (caption-highlight, caption-pill-karaoke, caption-clip-wipe, caption-kinetic-slam, blur-in,
  bottom-up-letters, lt-clean-bar). Kept as reference; not mounted directly.

Render one clip: `npx --yes hyperframes@0.9.41 render <out_dir> -o <file>.mp4 --quality delivery`.
