# Notice

OwlCut is a modified version of **OpenMontage**
(<https://github.com/calesthio/OpenMontage>), copyright its original authors,
distributed under the GNU Affero General Public License v3.0 (see `LICENSE`).

This fork is distributed under the same licence. If you run a modified version
of it as a network service, AGPL-3.0 section 13 requires you to offer its source
code to the service's users.

## Changes from upstream

Starting from upstream commit `d21f5d4`, this fork:

- adds the **OwlCut** web app on top of OpenMontage's Backlot board
  (`backlot/ui/`): a shared app bar, a new light/dark design system
  (`studio.css`, `shell.js`), and browser-started productions (`/new`, `/job`);
- adds the **YouTube clips** workflow (`backlot/clipper.py`, `/clips`,
  `/clips/<job>`), which drives the upstream `clip-factory` pipeline through a
  headless Claude Code session and a `clips.json` contract;
- adds the **publishing kit** (`/publish`) with per-network texts, posted
  tracking and a `.zip` export;
- adds the **clip kit** (`styles/clips/`): caption, intro and outro styles
  adapted from HyperFrames registry items, with rendered previews;
- prepares the server's `PATH` for jobs (project venv, full FFmpeg build) and
  pins `mediapipe`/`opencv` versions compatible with `face_tracker`
  (`requirements-clips.txt`);
- moves the upstream README to `docs/upstream/`.

The internal Python package keeps the upstream name `backlot`, so the upstream
agent guides (`python -m backlot open`) keep working.
