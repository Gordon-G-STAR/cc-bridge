from __future__ import annotations

import subprocess
import sys
import textwrap
import time

import pytest

from cc_bridge.bridge import config
from cc_bridge.bridge.locks import LockBusy, project_lock


def test_project_lock_can_be_reacquired_after_release(monkeypatch, tmp_path):
    app_dir = tmp_path / "app"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setattr(config, "stable_app_dir", lambda: app_dir)

    with project_lock(project_dir):
        assert list((app_dir / "locks").glob("*.lock"))

    with project_lock(project_dir):
        assert list((app_dir / "locks").glob("*.lock"))


def test_same_project_spellings_share_one_lock(monkeypatch, tmp_path):
    app_dir = tmp_path / "app"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setattr(config, "stable_app_dir", lambda: app_dir)

    if sys.platform == "win32":
        alternate = str(project_dir).upper() + "\\"
    else:
        alternate = str(project_dir) + "/"

    with project_lock(project_dir):
        with pytest.raises(LockBusy):
            with project_lock(alternate, timeout=0.2, poll=0.05):
                pass
        assert len(list((app_dir / "locks").glob("*.lock"))) == 1


def test_lock_busy_message_is_ascii(monkeypatch, tmp_path):
    app_dir = tmp_path / "app"
    project_dir = tmp_path / ("project-" + chr(0x4E00))
    project_dir.mkdir()
    monkeypatch.setattr(config, "stable_app_dir", lambda: app_dir)

    with project_lock(project_dir):
        with pytest.raises(LockBusy) as exc:
            with project_lock(project_dir, timeout=0.2, poll=0.05):
                pass
    str(exc.value).encode("ascii")


def test_project_lock_excludes_other_process(monkeypatch, tmp_path):
    app_dir = tmp_path / "app"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setattr(config, "stable_app_dir", lambda: app_dir)

    script = textwrap.dedent(
        """
        import sys
        import time
        from pathlib import Path
        from cc_bridge.bridge import config
        from cc_bridge.bridge.locks import project_lock

        config.stable_app_dir = lambda: Path(sys.argv[1])
        with project_lock(sys.argv[2], timeout=5.0, poll=0.05):
            print("locked", flush=True)
            time.sleep(1.5)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(app_dir), str(project_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        line = ""
        while time.monotonic() < deadline:
            line = child.stdout.readline() if child.stdout is not None else ""
            if line:
                break
        assert line.strip() == "locked"

        with pytest.raises(LockBusy):
            with project_lock(project_dir, timeout=0.5, poll=0.05):
                pass

        assert child.wait(timeout=5) == 0
        with project_lock(project_dir, timeout=1.0, poll=0.05):
            pass
    finally:
        if child.poll() is None:
            child.kill()
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, stdout + stderr
