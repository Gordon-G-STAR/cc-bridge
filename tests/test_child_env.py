"""PR1 —— P0.1: 子进程环境用 allow-list 构造，不把父进程整份环境泄给 agent。"""

from __future__ import annotations

from cc_bridge.bridge.config import _is_sensitive_env_name, build_child_env


def _get_ci(env: dict, name: str):
    """大小写不敏感取值（Windows 上 os.environ 的键大小写不固定）。"""
    for k, v in env.items():
        if k.upper() == name.upper():
            return v
    return None


def _has_ci(env: dict, name: str) -> bool:
    return any(k.upper() == name.upper() for k in env)


def test_essential_kept_secret_and_random_dropped(monkeypatch):
    monkeypatch.delenv("CC_BRIDGE_ENV_PASSTHROUGH", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-xxx")          # 敏感
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "t")          # 敏感
    monkeypatch.setenv("ZZ_NOT_ALLOWLISTED", "x")            # 不在白名单
    env = build_child_env()
    assert _get_ci(env, "PATH") == "/usr/bin"
    assert not _has_ci(env, "OPENAI_API_KEY")
    assert not _has_ci(env, "ANTHROPIC_AUTH_TOKEN")
    assert not _has_ci(env, "ZZ_NOT_ALLOWLISTED")


def test_passthrough_adds_extra_name(monkeypatch):
    monkeypatch.setenv("MY_CUSTOM_HOME", "/opt/x")
    monkeypatch.setenv("CC_BRIDGE_ENV_PASSTHROUGH", "MY_CUSTOM_HOME")
    env = build_child_env()
    assert _get_ci(env, "MY_CUSTOM_HOME") == "/opt/x"


def test_passthrough_cannot_leak_secret_named(monkeypatch):
    monkeypatch.setenv("MY_API_TOKEN", "leak")
    monkeypatch.setenv("CC_BRIDGE_ENV_PASSTHROUGH", "MY_API_TOKEN")
    env = build_child_env()
    # 敏感剔除优先于 passthrough：显式追加也不能把 secret 名漏出去。
    assert not _has_ci(env, "MY_API_TOKEN")


def test_extra_env_applied_and_exempt(monkeypatch):
    monkeypatch.delenv("CC_BRIDGE_ENV_PASSTHROUGH", raising=False)
    env = build_child_env({"CC_BRIDGE_HANDOFF_DEPTH": "1", "INTERNAL_TOKEN_X": "v"})
    assert env["CC_BRIDGE_HANDOFF_DEPTH"] == "1"
    # extra_env 是桥自身显式可信的追加，不受敏感剔除约束。
    assert env["INTERNAL_TOKEN_X"] == "v"


def test_sensitive_name_detection():
    for n in [
        "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "MY_TOKEN",
        "DB_PASSWORD", "x_credential", "GH_APIKEY", "ftp_passwd",
    ]:
        assert _is_sensitive_env_name(n), n
    # 关键反例:PATH 含 "PAT" 但绝不能被当敏感(若误用 "PAT" 模式就会炸)。
    for n in ["PATH", "HOME", "SystemRoot", "NODE_OPTIONS", "USERPROFILE", "CODEX_HOME"]:
        assert not _is_sensitive_env_name(n), n
