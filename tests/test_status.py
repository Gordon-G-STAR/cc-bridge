"""cc_bridge.bridge.status 运行期就绪探测单测。"""

from __future__ import annotations

from cc_bridge.bridge import status


def test_ready_when_cli_and_logged_in():
    r = status.AgentReadiness("Codex", "C:/x/codex", "codex 1.0", logged_in=True, login_known=True)
    assert r.cli_available is True
    assert r.ready is True
    line = r.status_line()
    assert "codex 1.0" in line and "就绪" in line and "未就绪" not in line


def test_not_ready_when_no_cli():
    r = status.AgentReadiness("Codex", None, None, logged_in=False, login_known=True)
    assert r.cli_available is False
    assert r.ready is False
    assert "不可用" in r.status_line()


def test_login_unknown_is_ready_but_flagged():
    r = status.AgentReadiness("Claude", "C:/x/claude", "claude 1.0", logged_in=False, login_known=False)
    assert r.ready is True  # 未知不阻塞
    line = r.status_line()
    assert "登录态未确认" in line and "尚未就绪" not in line


def test_confirmed_logged_out_not_ready():
    r = status.AgentReadiness("Codex", "C:/x/codex", "codex 1.0", logged_in=False, login_known=True)
    assert r.ready is False
    assert "未就绪" in r.status_line()


def test_check_codex_uses_auth_file(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(status.config, "resolve_cli",
                        lambda name: "C:/x/codex" if name == "codex" else None)
    monkeypatch.setattr(status.config, "codex_auth_path", lambda: auth)
    monkeypatch.setattr(status, "cli_version", lambda name, exe: "codex 9.9")
    r = status.check_codex()
    assert r.logged_in is True and r.login_known is True and r.version == "codex 9.9"


def test_claude_login_only_credentials_file(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(status.Path, "home", staticmethod(lambda: home))

    # 没有 .credentials.json → 登录态未知（不臆断已登录）
    assert status.claude_login() == (False, False)

    # ~/.claude.json 存在也不算（它是状态文件，登出后仍在）
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    assert status.claude_login() == (False, False)

    # 只有真正的凭证文件才算「确认已登录」
    (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    assert status.claude_login() == (True, True)
