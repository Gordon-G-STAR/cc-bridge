"""installer.main 入口的离线单测（不创建任何 GUI 窗口）。

覆盖 frozen 自拉起的安全边界：stdio 守卫、未知目标、隐藏控制台不崩。
"""

from __future__ import annotations

import sys

from cc_bridge.bridge import config
from cc_bridge.installer import main as m


def test_run_mcp_server_guards_missing_stdio(monkeypatch, tmp_path):
    """被错误打成 --windowed（stdin/stdout 为 None）时，明确失败返回 2，不启动 server。"""
    monkeypatch.setattr(config, "stable_app_dir", lambda: tmp_path / "cc-bridge")
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(sys, "stdout", None)
    assert m._run_mcp_server("codex") == 2
    # 致命错误应落到日志文件（此时不能用 stdout）
    assert (tmp_path / "cc-bridge" / "mcp-error.log").exists()


def test_run_mcp_server_unknown_target(monkeypatch, tmp_path):
    """stdio 正常但目标未知 → 返回 2，不会真正拉起任何 server。"""
    monkeypatch.setattr(config, "stable_app_dir", lambda: tmp_path / "cc-bridge")
    assert m._run_mcp_server("bogus") == 2


def test_run_mcp_server_clears_stale_log_on_success(monkeypatch, tmp_path):
    """server 正常启动时，应清掉上一次 windowed 失败留下的过期 mcp-error.log。"""
    appdir = tmp_path / "cc-bridge"
    appdir.mkdir(parents=True)
    stale = appdir / "mcp-error.log"
    stale.write_text("旧 windowed 失败日志，早已不成立", encoding="utf-8")
    monkeypatch.setattr(config, "stable_app_dir", lambda: appdir)
    # 让 srv() 立刻返回，不真正阻塞读 stdin。
    import cc_bridge.bridge.mcp_to_codex as codex_srv
    monkeypatch.setattr(codex_srv, "main", lambda: None)

    assert m._run_mcp_server("codex") == 0
    assert not stale.exists(), "成功启动后应清除过期失败日志"


def test_unknown_target_does_not_clear_log(monkeypatch, tmp_path):
    """未知目标是【新】失败：写下的 fatal 日志不应被误清。"""
    appdir = tmp_path / "cc-bridge"
    appdir.mkdir(parents=True)
    monkeypatch.setattr(config, "stable_app_dir", lambda: appdir)
    assert m._run_mcp_server("bogus") == 2
    assert (appdir / "mcp-error.log").exists()


def test_hide_console_window_skips_when_not_frozen(monkeypatch):
    """非 frozen（pip 的 cc-bridge-install 从终端运行）必须是 no-op，绝不动用户终端。"""
    monkeypatch.setattr(m.sys, "frozen", False, raising=False)
    assert m._hide_console_window() is False


def test_hide_console_window_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(m.os, "name", "posix")
    assert m._hide_console_window() is False
