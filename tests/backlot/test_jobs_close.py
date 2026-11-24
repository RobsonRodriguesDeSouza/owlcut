"""Library "Cancel": JobManager.close + in-progress summaries (backlot/jobs.py)."""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from backlot import jobs as jobs_mod


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(jobs_mod, "PROJECTS_DIR", tmp_path / "projects")
    return jobs_mod.JobManager()


def _job(m, jid, status, **extra):
    job = {"id": jid, "project_id": jid, "kind": "clips", "title": jid, "status": status,
           "form": {"pipeline": "clip-factory"}, "session_id": "x", "created_at": time.time(), **extra}
    m._save(job)
    return job


def test_close_idle_job_takes_it_off_the_open_list(manager):
    _job(manager, "a", "waiting", last_message="**Stopped** at the `script` gate.\n\nReview it.")
    row = next(j for j in manager.list() if j["id"] == "a")
    assert row["stages_total"] > 0 and row["stage"]
    assert row["summary"] == "Stopped at the script gate. Review it."
    assert manager.close("a")["status"] == "closed"
    row = next(j for j in manager.list() if j["id"] == "a")
    assert row["status"] == "closed" and row["closed_at"]       # still described, for the Cancelled list


def test_close_leaves_finished_jobs_alone(manager):
    _job(manager, "d", "done")
    assert manager.close("d")["status"] == "done"
    with pytest.raises(KeyError):
        manager.close("nope")


def test_close_running_job_stops_the_agent(manager):
    _job(manager, "r", "running")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    manager._procs["r"] = proc
    import threading
    t = threading.Thread(target=manager._pump, args=("r", proc), daemon=True)
    t.start()
    manager.close("r")
    t.join(timeout=10)
    assert proc.poll() is not None
    job = json.loads((jobs_mod.JOBS_DIR / "r" / "job.json").read_text())
    assert job["status"] == "closed" and "close_requested" not in job and "cancel_requested" not in job


def test_error_summary_is_exposed(manager):
    _job(manager, "e", "error", error="You've hit your limit\nmore", last_message="x")
    row = next(j for j in manager.list() if j["id"] == "e")
    assert row["error"].startswith("You've hit your limit")


def test_importing_and_building_the_app_never_touches_jobs(manager):
    _job(manager, "live", "running")
    from backlot import server as server_mod
    server_mod.create_app()                      # what `import backlot.server` does at module level
    assert json.loads((jobs_mod.JOBS_DIR / "live" / "job.json").read_text())["status"] == "running"
    manager.recover()                            # what the serving process does at startup
    assert json.loads((jobs_mod.JOBS_DIR / "live" / "job.json").read_text())["status"] == "interrupted"


def test_agent_model_defaults_to_sonnet_and_is_overridable(monkeypatch, tmp_path):
    monkeypatch.delenv("OWLCUT_AGENT_MODEL", raising=False)
    monkeypatch.setattr(jobs_mod, "REPO_ROOT", tmp_path)
    assert jobs_mod.agent_model() == jobs_mod.DEFAULT_AGENT_MODEL == "claude-sonnet-5-5"
    (tmp_path / ".env").write_text('FOO=1\nOWLCUT_AGENT_MODEL="claude-opus-5-5"\n')
    assert jobs_mod.agent_model() == "claude-opus-5-5"
    monkeypatch.setenv("OWLCUT_AGENT_MODEL", "default")
    assert jobs_mod.agent_model() == "default"


def test_turn_command_passes_the_agent_model(manager, monkeypatch):
    seen = {}

    class FakeProc:
        pid = 0
        stdout = iter(())
        stderr = iter(())
        returncode = 0
        def wait(self): return 0
        def poll(self): return 0

    monkeypatch.setattr(jobs_mod, "find_claude", lambda: "/bin/claude")
    monkeypatch.setattr(jobs_mod.subprocess, "Popen", lambda cmd, **kw: seen.setdefault("cmd", cmd) and FakeProc())
    # no pump thread: it would outlive the test and write into the real .backlot/jobs
    monkeypatch.setattr(jobs_mod.threading, "Thread", lambda *a, **kw: type("T", (), {"start": lambda self: None})())
    monkeypatch.setenv("OWLCUT_AGENT_MODEL", "claude-sonnet-5-5")
    manager._start_turn(_job(manager, "m", "running"), "hi", first=True)
    cmd = seen["cmd"]
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5"


def test_reopen_restores_the_previous_state(manager):
    _job(manager, "w", "waiting")
    manager.close("w")
    row = next(j for j in manager.list() if j["id"] == "w")
    assert row["status"] == "closed" and row["closed_at"] and row["stages_total"] > 0   # list shows progress
    assert manager.reopen("w")["status"] == "waiting"
    _job(manager, "e", "error")
    manager.close("e")
    assert manager.reopen("e")["status"] == "error"
    _job(manager, "d", "done")
    assert manager.reopen("d")["status"] == "done"          # only closed jobs move


def test_delete_cancelled_production_removes_job_and_project(manager, tmp_path):
    from backlot import exports
    _job(manager, "gone", "waiting", kind="video")
    pdir = jobs_mod.PROJECTS_DIR / "gone" / "renders"
    pdir.mkdir(parents=True)
    (pdir / "final.mp4").write_bytes(b"x" * 1000)
    folder = exports.publish("video", "project:gone", "Gone", 0, [(pdir / "final.mp4", "final.mp4")])
    assert folder and folder.is_dir()

    with pytest.raises(ValueError):                                  # only cancelled ones
        manager.delete("gone")
    manager.close("gone")
    dry = manager.delete("gone", dry_run=True)
    assert dry["bytes"] >= 1000                                      # the app's own copy of the video
    assert dry["with_exports_bytes"] - dry["bytes"] == 1000 and dry["export"] == str(folder)
    assert (jobs_mod.PROJECTS_DIR / "gone").is_dir()                 # dry run deletes nothing

    manager.delete("gone")                                           # OwlCutVideos kept by default
    assert not (jobs_mod.PROJECTS_DIR / "gone").exists() and not (jobs_mod.JOBS_DIR / "gone").exists()
    assert (folder / "final.mp4").read_bytes() == b"x" * 1000
    assert exports.existing_folder("video", "project:gone") is None  # forgotten by the index
    with pytest.raises(KeyError):
        manager.delete("gone")


def test_delete_can_include_the_owlcutvideos_folder(manager, monkeypatch):
    import lib.paths
    from backlot import exports
    monkeypatch.setattr(lib.paths, "PROJECTS_DIR", jobs_mod.PROJECTS_DIR)
    _job(manager, "both", "closed")
    pdir = jobs_mod.PROJECTS_DIR / "both" / "renders" / "clips"
    pdir.mkdir(parents=True)
    (pdir / "c01_9x16.mp4").write_bytes(b"y" * 500)
    folder = exports.export_project("both", "clips", "Both", 0)
    assert folder.is_dir()
    assert manager.delete("both", include_exports=True)["bytes"] >= 500
    assert not folder.exists()


def test_delete_never_leaves_the_projects_dir(manager, tmp_path):
    outside = tmp_path / "precious"
    outside.mkdir()
    _job(manager, "evil", "closed", project_id="../precious")
    manager.delete("evil")
    assert outside.is_dir()                                          # bad project_id: only the job goes
