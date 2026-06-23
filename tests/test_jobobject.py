from __future__ import annotations

import subprocess
import sys

import pytest

from cc_bridge.bridge.jobobject import kill_on_close_job, supported


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only no-op behavior")
def test_jobobject_is_noop_off_windows():
    assert supported() is False

    with kill_on_close_job() as job:
        assert job.assign(123) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only job object behavior")
def test_jobobject_kills_assigned_child_on_exit():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        with kill_on_close_job() as job:
            assert job.assign(child.pid) is True

        child.wait(timeout=3)
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
