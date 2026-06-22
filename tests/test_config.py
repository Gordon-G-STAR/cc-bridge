"""config 平台关键纯函数的单测.

覆盖审查指出的盲区：build_launch_argv（Windows shim 包装）、
subprocess_creation_kwargs、BridgeConfig.from_env、以及 frozen 场景下的
mcp_launch_command 决策。这些都是确定性纯函数，最该被锁住以防回归。
"""

from __future__ import annotations

import sys

import pytest

from cc_bridge.bridge import config


# ---------------------------------------------------------------------------
# build_launch_argv —— Windows shim 包装
# ---------------------------------------------------------------------------

def test_build_launch_argv_cmd_wrapped_on_windows(monkeypatch):
    monkeypatch.setattr(config, "IS_WINDOWS", True)
    argv = config.build_launch_argv(r"C:\x\codex.cmd", ["exec", "--json"])
    assert argv == ["cmd.exe", "/d", "/s", "/c", r"C:\x\codex.cmd", "exec", "--json"]


def test_build_launch_argv_bat_wrapped_on_windows(monkeypatch):
    monkeypatch.setattr(config, "IS_WINDOWS", True)
    argv = config.build_launch_argv(r"C:\x\foo.bat", ["a"])
    assert argv[:5] == ["cmd.exe", "/d", "/s", "/c", r"C:\x\foo.bat"]


def test_build_launch_argv_ps1_wrapped_on_windows(monkeypatch):
    monkeypatch.setattr(config, "IS_WINDOWS", True)
    argv = config.build_launch_argv(r"C:\x\codex.ps1", ["exec"])
    assert argv[0] == "powershell.exe"
    assert "-File" in argv
    assert argv[-2:] == [r"C:\x\codex.ps1", "exec"]


def test_build_launch_argv_exe_direct_on_windows(monkeypatch):
    monkeypatch.setattr(config, "IS_WINDOWS", True)
    argv = config.build_launch_argv(r"C:\x\claude.exe", ["-p"])
    assert argv == [r"C:\x\claude.exe", "-p"]


def test_build_launch_argv_no_wrap_on_posix(monkeypatch):
    monkeypatch.setattr(config, "IS_WINDOWS", False)
    # 即便后缀是 .cmd，在非 Windows 上也不包装。
    argv = config.build_launch_argv("/usr/bin/codex", ["exec"])
    assert argv == ["/usr/bin/codex", "exec"]


# ---------------------------------------------------------------------------
# subprocess_creation_kwargs
# ---------------------------------------------------------------------------

def test_creation_kwargs_windows(monkeypatch):
    monkeypatch.setattr(config, "IS_WINDOWS", True)
    kw = config.subprocess_creation_kwargs()
    assert "creationflags" in kw
    assert "start_new_session" not in kw


def test_creation_kwargs_posix(monkeypatch):
    monkeypatch.setattr(config, "IS_WINDOWS", False)
    kw = config.subprocess_creation_kwargs()
    assert kw == {"start_new_session": True}


# ---------------------------------------------------------------------------
# BridgeConfig.from_env
# ---------------------------------------------------------------------------

def test_from_env_reads_overrides(monkeypatch):
    monkeypatch.setenv("CC_BRIDGE_TIMEOUT", "60")
    monkeypatch.setenv("CC_BRIDGE_MAX_OUTPUT", "1234")
    monkeypatch.setenv("CC_BRIDGE_CODEX_SANDBOX", "read-only")
    monkeypatch.setenv("CC_BRIDGE_CLAUDE_PERMISSION", "acceptEdits")
    monkeypatch.setenv("CC_BRIDGE_CODEX_MODEL", "o3")
    monkeypatch.setenv("CC_BRIDGE_CLAUDE_MODEL", "opus")
    cfg = config.BridgeConfig.from_env()
    assert cfg.timeout_seconds == 60
    assert cfg.max_output_chars == 1234
    assert cfg.codex_sandbox == "read-only"
    assert cfg.claude_permission_mode == "acceptEdits"
    assert cfg.codex_model == "o3"
    assert cfg.claude_model == "opus"


def test_from_env_invalid_int_falls_back(monkeypatch):
    monkeypatch.setenv("CC_BRIDGE_TIMEOUT", "not-a-number")
    monkeypatch.delenv("CC_BRIDGE_CODEX_MODEL", raising=False)
    cfg = config.BridgeConfig.from_env()
    assert cfg.timeout_seconds == config.DEFAULT_TIMEOUT_SECONDS
    assert cfg.codex_model is None


def test_from_env_defaults(monkeypatch):
    for var in (
        "CC_BRIDGE_TIMEOUT", "CC_BRIDGE_MAX_OUTPUT", "CC_BRIDGE_CODEX_SANDBOX",
        "CC_BRIDGE_CLAUDE_PERMISSION", "CC_BRIDGE_CODEX_MODEL", "CC_BRIDGE_CLAUDE_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = config.BridgeConfig.from_env()
    assert cfg.timeout_seconds == 300
    assert cfg.codex_sandbox == "workspace-write"
    assert cfg.claude_permission_mode == "bypassPermissions"


# ---------------------------------------------------------------------------
# mcp_launch_command —— frozen 决策
# ---------------------------------------------------------------------------

def test_mcp_launch_command_dev_uses_python_m(monkeypatch):
    monkeypatch.setattr(config, "resolve_cli", lambda name: None)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    cmd, args = config.mcp_launch_command("cc_bridge.bridge.mcp_to_codex", "cc-bridge-mcp-codex", "codex")
    assert cmd == sys.executable
    assert args == ["-m", "cc_bridge.bridge.mcp_to_codex"]


def test_mcp_launch_command_prefers_console_script(monkeypatch):
    monkeypatch.setattr(config, "resolve_cli", lambda name: "/bin/cc-bridge-mcp-codex" if name == "cc-bridge-mcp-codex" else None)
    cmd, args = config.mcp_launch_command("cc_bridge.bridge.mcp_to_codex", "cc-bridge-mcp-codex", "codex")
    assert cmd == "/bin/cc-bridge-mcp-codex"
    assert args == []


def test_mcp_launch_command_frozen_self_reexec(monkeypatch):
    monkeypatch.setattr(config, "resolve_cli", lambda name: None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(config, "_ensure_frozen_launcher", lambda: r"C:\stable\cc-bridge-installer.exe")
    cmd, args = config.mcp_launch_command("cc_bridge.bridge.mcp_to_codex", "cc-bridge-mcp-codex", "codex")
    assert cmd == r"C:\stable\cc-bridge-installer.exe"
    assert args == ["--mcp-server", "codex"]


def test_mcp_launch_command_frozen_prefers_self_over_console_script(monkeypatch):
    """frozen 即便 PATH 上有 console-script，也应自拉起，避免旧入口抢注册（版本错配）。"""
    monkeypatch.setattr(config, "resolve_cli", lambda name: "/old/cc-bridge-mcp-codex")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        config, "_ensure_frozen_launcher", lambda: r"C:\stable\cc-bridge-installer.exe"
    )
    cmd, args = config.mcp_launch_command(
        "cc_bridge.bridge.mcp_to_codex", "cc-bridge-mcp-codex", "codex"
    )
    assert cmd == r"C:\stable\cc-bridge-installer.exe"
    assert args == ["--mcp-server", "codex"]


def test_mcp_launch_command_frozen_no_option_raises(monkeypatch):
    monkeypatch.setattr(config, "resolve_cli", lambda name: None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(config, "_find_python_with_cc_bridge", lambda: None)
    # server_key=None 且找不到 python → 抛 RuntimeError，而不是写出拉不起来的命令
    with pytest.raises(RuntimeError):
        config.mcp_launch_command("cc_bridge.bridge.mcp_to_codex", None, None)


# ---------------------------------------------------------------------------
# _is_bundled_executable：onefile vs onedir/.app
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402


def test_should_refresh_launcher_missing_target(tmp_path):
    src = tmp_path / "src.exe"
    src.write_text("v1", encoding="utf-8")
    assert config._should_refresh_launcher(src, tmp_path / "nope.exe") is True


def test_should_refresh_launcher_same_size_and_time_skips(tmp_path):
    import os as _os

    src = tmp_path / "src.exe"
    tgt = tmp_path / "tgt.exe"
    src.write_text("samesize", encoding="utf-8")
    tgt.write_text("samesize", encoding="utf-8")
    # 对齐 mtime（模拟 copy2 之后的状态）：同大小 + 目标不旧于源 → 跳过。
    _os.utime(tgt, (src.stat().st_atime, src.stat().st_mtime))
    assert config._should_refresh_launcher(src, tgt) is False


def test_should_refresh_launcher_size_differs(tmp_path):
    src = tmp_path / "src.exe"
    tgt = tmp_path / "tgt.exe"
    src.write_text("longer-content", encoding="utf-8")
    tgt.write_text("short", encoding="utf-8")
    assert config._should_refresh_launcher(src, tgt) is True


def test_should_refresh_launcher_same_size_newer_source(tmp_path):
    """同样大小但源更新（重打包）必须刷新——这正是仅比大小会漏掉的关键场景。"""
    import os as _os

    src = tmp_path / "src.exe"
    tgt = tmp_path / "tgt.exe"
    tgt.write_text("samesizeAA", encoding="utf-8")
    src.write_text("samesizeBB", encoding="utf-8")  # 同长度，内容不同（新版本）
    old = tgt.stat().st_mtime
    _os.utime(tgt, (old - 100, old - 100))           # 目标更旧
    _os.utime(src, (old + 100, old + 100))           # 源更新 100s
    assert config._should_refresh_launcher(src, tgt) is True


def test_is_bundled_macos_app():
    assert config._is_bundled_executable(Path("/Applications/cc-bridge.app/Contents/MacOS/cc-bridge")) is True


def test_is_bundled_onefile_is_false(tmp_path):
    exe = tmp_path / "cc-bridge-installer.exe"
    exe.write_text("x", encoding="utf-8")
    assert config._is_bundled_executable(exe) is False


def test_is_bundled_onedir_internal(tmp_path):
    (tmp_path / "_internal").mkdir()
    exe = tmp_path / "cc-bridge-installer.exe"
    exe.write_text("x", encoding="utf-8")
    assert config._is_bundled_executable(exe) is True


# ---------------------------------------------------------------------------
# resolve_cli：环境变量逃生口 + 兜底目录（macOS Finder PATH 过短）
# ---------------------------------------------------------------------------

def test_resolve_cli_env_override(monkeypatch, tmp_path):
    fake = tmp_path / "claude-bin"
    fake.write_text("x", encoding="utf-8")
    monkeypatch.setenv("CC_BRIDGE_CLAUDE_PATH", str(fake))
    assert config.resolve_cli("claude") == str(fake)


def test_resolve_cli_env_override_ignored_if_not_a_file(monkeypatch):
    monkeypatch.setenv("CC_BRIDGE_CODEX_PATH", "/definitely/no/such/file")
    # 路径不存在 → 忽略 override，绝不返回那个无效路径
    assert config.resolve_cli("codex") != "/definitely/no/such/file"


def test_resolve_cli_falls_back_to_common_dirs(monkeypatch, tmp_path):
    tool = tmp_path / "weirdcli"
    tool.write_text("x", encoding="utf-8")
    monkeypatch.delenv("CC_BRIDGE_WEIRDCLI_PATH", raising=False)
    # which 找不到（不在 PATH），应在兜底目录里命中
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    monkeypatch.setattr(config, "_fallback_bin_dirs", lambda: [str(tmp_path)])
    assert config.resolve_cli("weirdcli") == str(tool)


# ---------------------------------------------------------------------------
# run_capture / git_capture：硬化短命令执行（真有界、不挂死、stdin 隔离）
# ---------------------------------------------------------------------------

import time as _time  # noqa: E402


def test_run_capture_normal_success():
    res = config.run_capture([sys.executable, "-c", "print('hi')"], timeout=15)
    assert res.returncode == 0
    assert res.timed_out is False
    assert b"hi" in res.stdout


def test_run_capture_bounds_a_hanging_process():
    """会睡 60s 的子进程，timeout=2 必须在远小于 60s 内带 timed_out 返回（不挂死）。"""
    start = _time.monotonic()
    res = config.run_capture(
        [sys.executable, "-c", "import time; time.sleep(60)"], timeout=2
    )
    elapsed = _time.monotonic() - start
    assert res.timed_out is True
    assert res.returncode is None
    assert elapsed < 20, f"超时未被有界，耗时 {elapsed:.1f}s"


def test_run_capture_stdin_is_devnull_not_inherited():
    """子进程读 stdin：若继承了挂起的 stdin 会永久阻塞；DEVNULL 下立即 EOF→正常退出。

    这正是「在 MCP stdio server 里跑子进程会吃掉/卡在协议管道」那个坑的回归防线。
    """
    res = config.run_capture(
        [sys.executable, "-c", "import sys; sys.stdin.read(); print('done')"], timeout=10
    )
    assert res.timed_out is False
    assert res.returncode == 0
    assert b"done" in res.stdout


def test_run_capture_unlaunchable_returns_none():
    res = config.run_capture(["definitely-no-such-binary-cc-bridge-xyz"], timeout=3)
    assert res.returncode is None
    assert res.timed_out is False


def test_git_capture_hardening_flags_and_env(monkeypatch):
    """git_capture 必须带 -c core.fsmonitor=false 与 -C cwd，并注入禁交互的 git 环境。"""
    captured = {}

    def _fake_run_capture(argv, *, timeout, extra_env=None):
        captured["argv"] = argv
        captured["env"] = extra_env
        return config.CapturedRun(returncode=0, stdout=b"main\n")

    monkeypatch.setattr(config, "run_capture", _fake_run_capture)
    res = config.git_capture("git", r"C:\proj", ["rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
    assert res.returncode == 0
    argv = captured["argv"]
    assert argv[0] == "git"
    assert "core.fsmonitor=false" in argv
    assert "-C" in argv and r"C:\proj" in argv
    assert argv[-2:] == ["--abbrev-ref", "HEAD"]
    assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert captured["env"]["GCM_INTERACTIVE"] == "Never"


def test_debug_log_silent_without_env(monkeypatch, capsys):
    monkeypatch.delenv("CC_BRIDGE_DEBUG", raising=False)
    config.debug_log("不应出现")
    assert capsys.readouterr().err == ""


def test_debug_log_writes_stderr_when_enabled(monkeypatch, capsys):
    monkeypatch.setenv("CC_BRIDGE_DEBUG", "1")
    config.debug_log("阶段A")
    err = capsys.readouterr().err
    assert "阶段A" in err and "cc-bridge" in err
