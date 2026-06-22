"""EnvironmentDetector 离线单测。

不碰真实 CLI / 文件系统：monkeypatch 掉
- detector.config.resolve_cli（控制 claude/codex/git 是否「可用」）
- cc_bridge.bridge.config.run_capture（伪造 --version 输出）
- detector.config.codex_auth_path（指向存在 / 不存在的 tmp 文件）
- detector.config.claude_desktop_config_path（指向 tmp，避免读到真实环境）

并把 Claude 登录态相关的凭证路径指向不存在的 tmp，保证可控。
"""

from __future__ import annotations


from cc_bridge.bridge import status as bstatus
from cc_bridge.installer import detector as det


def _patch_resolve(monkeypatch, mapping):
    monkeypatch.setattr(det.config, "resolve_cli", lambda name: mapping.get(name))


def _patch_version(monkeypatch, version_text="1.2.3 (cli)"):
    # 版本探测（status.cli_version）现在走 config.run_capture；伪造它的返回（stdout 是 bytes）。
    def fake_run_capture(argv, *, timeout, extra_env=None):
        return bstatus.config.CapturedRun(returncode=0, stdout=version_text.encode("utf-8"))

    monkeypatch.setattr(bstatus.config, "run_capture", fake_run_capture)


def _patch_codex_auth(monkeypatch, path):
    monkeypatch.setattr(det.config, "codex_auth_path", lambda: path)


def _isolate_claude_login(monkeypatch, tmp_path, exists=False):
    """把 Claude 登录判断引到可控路径。

    _claude_logged_in 先看 ~/.claude/.credentials.json 与 ~/.claude.json，
    都不存在时回退到「CLI 是否可用」。这里把 home 指到一个临时空目录，
    确保不会误读真实环境里的凭证。exists=True 时创建凭证文件。
    """
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    if exists:
        (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(det.Path, "home", staticmethod(lambda: home))
    # 桌面版配置目录也指向 tmp，避免 _claude_desktop_installed 命中真实路径
    cfg_path = tmp_path / "claude_desktop" / "claude_desktop_config.json"
    monkeypatch.setattr(det.config, "claude_desktop_config_path", lambda: cfg_path)


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

def test_check_claude_ready_when_cli_and_logged_in(monkeypatch, tmp_path):
    _patch_resolve(monkeypatch, {"claude": "C:/fake/claude.exe"})
    _patch_version(monkeypatch, "claude 1.0.0")
    _isolate_claude_login(monkeypatch, tmp_path, exists=True)

    result = det.EnvironmentDetector().check_claude_desktop()

    assert result.cli_available is True
    assert result.logged_in is True
    assert result.ready is True
    assert result.version == "claude 1.0.0"
    assert result.cli_path == "C:/fake/claude.exe"


def test_check_claude_not_ready_when_cli_missing(monkeypatch, tmp_path):
    _patch_resolve(monkeypatch, {})  # claude 不可用
    _patch_version(monkeypatch)
    _isolate_claude_login(monkeypatch, tmp_path, exists=False)

    result = det.EnvironmentDetector().check_claude_desktop()

    assert result.cli_available is False
    # CLI 不可用 -> ready 必为 False
    assert result.ready is False
    assert result.version is None


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

def test_check_codex_ready_when_auth_exists(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    _patch_resolve(monkeypatch, {"codex": "C:/fake/codex.exe"})
    _patch_version(monkeypatch, "codex 2.0.0")
    _patch_codex_auth(monkeypatch, auth)

    result = det.EnvironmentDetector().check_codex_desktop()

    assert result.cli_available is True
    assert result.logged_in is True
    assert result.ready is True
    assert result.version == "codex 2.0.0"


def test_check_codex_not_logged_in_when_auth_missing(monkeypatch, tmp_path):
    auth = tmp_path / "missing_auth.json"  # 不创建
    _patch_resolve(monkeypatch, {"codex": "C:/fake/codex.exe"})
    _patch_version(monkeypatch)
    _patch_codex_auth(monkeypatch, auth)

    result = det.EnvironmentDetector().check_codex_desktop()

    assert result.cli_available is True
    assert result.logged_in is False
    assert result.ready is False


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def test_check_all_all_ready(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    _patch_resolve(monkeypatch, {"claude": "C:/fake/claude.exe", "codex": "C:/fake/codex.exe"})
    _patch_version(monkeypatch, "v1")
    _patch_codex_auth(monkeypatch, auth)
    _isolate_claude_login(monkeypatch, tmp_path, exists=True)

    status = det.EnvironmentDetector().check_all()

    assert status.all_ready is True
    d = status.as_dict()
    assert d["all_ready"] is True
    assert d["claude"]["name"] == "Claude"
    assert d["codex"]["name"] == "Codex"


def test_check_all_not_ready_when_codex_unauthed(monkeypatch, tmp_path):
    auth = tmp_path / "missing.json"  # 不创建 -> codex 未登录
    _patch_resolve(monkeypatch, {"claude": "C:/fake/claude.exe", "codex": "C:/fake/codex.exe"})
    _patch_version(monkeypatch, "v1")
    _patch_codex_auth(monkeypatch, auth)
    _isolate_claude_login(monkeypatch, tmp_path, exists=True)

    status = det.EnvironmentDetector().check_all()

    assert status.claude.ready is True
    assert status.codex.ready is False
    assert status.all_ready is False


def test_check_claude_login_unknown_when_cli_present_no_creds(monkeypatch, tmp_path):
    """CLI 可用但找不到凭证 → 登录态未知：不臆断已登录，但也不阻塞。"""
    _patch_resolve(monkeypatch, {"claude": "C:/fake/claude.exe"})
    _patch_version(monkeypatch, "claude 1.0.0")
    _isolate_claude_login(monkeypatch, tmp_path, exists=False)

    result = det.EnvironmentDetector().check_claude_desktop()

    assert result.cli_available is True
    assert result.logged_in is False
    assert result.login_known is False
    assert result.ready is True            # 不臆断未登录而阻塞
    assert "无法确认" in result.message     # 但消息如实说明


def test_check_claude_login_unknown_when_only_claude_json(monkeypatch, tmp_path):
    """只有 ~/.claude.json（CLI 状态文件，登出后仍在）→ 登录态未知，不得误报已登录。"""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude.json").write_text("{}", encoding="utf-8")  # 注意：不是 .credentials.json
    monkeypatch.setattr(det.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(det.config, "claude_desktop_config_path", lambda: tmp_path / "cfg.json")
    _patch_resolve(monkeypatch, {"claude": "C:/fake/claude.exe"})
    _patch_version(monkeypatch, "claude 1.0.0")

    result = det.EnvironmentDetector().check_claude_desktop()

    assert result.logged_in is False
    assert result.login_known is False
    assert result.ready is True  # 未知不阻塞
