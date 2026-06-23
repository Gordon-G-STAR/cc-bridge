from __future__ import annotations

import subprocess
import sys
import time

import pytest

from cc_bridge.bridge import config
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


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Job Object 杀进程树是 Windows 行为;POSIX 无 job(见 docs/WINDOWS_SECURITY_TESTS.md)",
)
def test_jobobject_kills_grandchild_no_write_after_snapshot(tmp_path):
    """对抗(#3a 端到端):agent 派生的【孙进程】在 job 关闭时被杀,无法在快照后偷写。

    P 被 assign 进 kill-on-close job 之后才去 spawn 孙进程 G(G 因此继承同一个 job)。
    G 先写 g_started(证明它真起来了、确实在 job 内),再 sleep,最后试图写 g_leaked
    (模拟"快照后写")。job 关闭应杀掉整棵树(P+G)→ g_leaked 永远写不出来。
    """
    started = tmp_path / "g_started.txt"
    leaked = tmp_path / "g_leaked_after_snapshot.txt"
    g_code = (
        "import time, pathlib; "
        f"pathlib.Path({str(started)!r}).write_text('up'); "
        "time.sleep(3.0); "
        f"pathlib.Path({str(leaked)!r}).write_text('leaked')"
    )
    p_code = (
        "import subprocess, sys, time; "
        "time.sleep(0.3); "  # 等主测试把 P assign 进 job,G 才会继承 job
        f"subprocess.Popen([sys.executable, '-c', {g_code!r}]); "
        "time.sleep(30)"
    )
    p = subprocess.Popen(
        [sys.executable, "-c", p_code], **config.subprocess_creation_kwargs()
    )
    try:
        with kill_on_close_job() as job:
            assert job.assign(p.pid) is True
            for _ in range(100):  # 最多 ~10s 等孙进程起来
                if started.exists():
                    break
                time.sleep(0.1)
            assert started.exists(), "孙进程未启动,PoC 前提不成立"
        # job 关闭 → 应杀掉 P+G。等 4s(超过 G 的 sleep 3.0):G 若没被杀就会写 leaked。
        time.sleep(4.0)
        assert not leaked.exists(), (
            "孙进程在 job 关闭后仍写盘 —— Job Object 没拦住'快照后写'"
        )
    finally:
        if p.poll() is None:
            p.kill()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
