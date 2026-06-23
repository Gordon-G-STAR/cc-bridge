"""PR2c —— 桥自己的 git 调用硬化(中和 .git/config / hooks / filters 的执行通道)。"""

from __future__ import annotations

from cc_bridge.bridge import config
from cc_bridge.bridge.config import CapturedRun


def test_git_capture_is_hardened(monkeypatch):
    captured: dict = {}

    def _fake_run_capture(argv, *, timeout, extra_env=None):
        captured["argv"] = list(argv)
        captured["env"] = dict(extra_env or {})
        return CapturedRun(0)

    monkeypatch.setattr(config, "run_capture", _fake_run_capture)

    config.git_capture("git", "/proj", ["status", "--porcelain"], timeout=5)

    argv = captured["argv"]
    assert argv[0] == "git"
    assert "--no-pager" in argv
    assert "core.fsmonitor=false" in argv
    assert any(a.startswith("core.hooksPath=") for a in argv)   # hooks 指向 devnull
    assert "core.sshCommand=false" in argv
    # 仍把请求的子命令原样带上
    assert "status" in argv and "--porcelain" in argv
    # 环境硬化
    assert captured["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"
