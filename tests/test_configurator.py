"""configurator 单元测试（离线）.

通过 monkeypatch 把 Claude 桌面版 / Codex 的配置文件路径都重定向到 tmp_path，
避免触碰用户真实配置，验证：

- register_all() 后两端配置都写对了（JSON 里 mcpServers / TOML 里 [mcp_servers.*]）；
- 再次 register → already_present 为 True；
- unregister_all() 后两端注册项都被移除。
"""

from __future__ import annotations

import json
import sys
import tomllib

import pytest

from cc_bridge.bridge import config
from cc_bridge.installer.configurator import (
    _CLAUDE_SERVER_KEY,
    _CLAUDE_SERVER_MODULE,
    _CODEX_SERVER_KEY,
    _CODEX_SERVER_MODULE,
    ConfigChange,
    Configurator,
)


@pytest.fixture
def patched_paths(tmp_path, monkeypatch):
    """把两端配置路径都指到 tmp_path 下，返回 (claude_json, codex_toml) 两个 Path。"""
    claude_path = tmp_path / "claude" / "claude_desktop_config.json"
    codex_home = tmp_path / "codex_home"
    codex_path = codex_home / "config.toml"

    monkeypatch.setattr(config, "claude_desktop_config_path", lambda: claude_path)
    monkeypatch.setattr(config, "codex_home", lambda: codex_home)
    monkeypatch.setattr(config, "codex_config_path", lambda: codex_path)
    # 让 mcp_launch_command 走确定的 `sys.executable -m` 分支：
    # 不依赖 PATH 上是否恰好存在 cc-bridge-mcp-* console-script（CI 上可能有）。
    monkeypatch.setattr(config, "resolve_cli", lambda name: None)

    return claude_path, codex_path


def test_register_all_writes_both_targets(patched_paths):
    claude_path, codex_path = patched_paths

    changes = Configurator().register_all()

    # 顺序：[Claude Desktop, Codex]
    assert [c.target for c in changes] == ["Claude Desktop", "Codex"]
    assert all(isinstance(c, ConfigChange) for c in changes)
    assert all(c.success for c in changes)
    # 首次注册都不是 already_present
    assert all(not c.already_present for c in changes)

    # --- Claude 端：JSON 读回断言 mcpServers 条目正确 -------------------------
    assert claude_path.exists()
    data = json.loads(claude_path.read_text(encoding="utf-8"))
    entry = data["mcpServers"][_CLAUDE_SERVER_KEY]
    expected_command, expected_args = config.mcp_launch_command(_CLAUDE_SERVER_MODULE)
    assert entry["command"] == expected_command == sys.executable
    assert entry["args"] == expected_args == ["-m", _CLAUDE_SERVER_MODULE]

    # --- Codex 端：tomllib 读回断言 section 与 args 正确 ----------------------
    assert codex_path.exists()
    parsed = tomllib.loads(codex_path.read_text(encoding="utf-8"))
    assert _CODEX_SERVER_KEY in parsed["mcp_servers"]
    codex_entry = parsed["mcp_servers"][_CODEX_SERVER_KEY]
    codex_command, codex_args = config.mcp_launch_command(_CODEX_SERVER_MODULE)
    assert codex_entry["command"] == codex_command
    assert codex_entry["args"] == codex_args == ["-m", _CODEX_SERVER_MODULE]


def test_register_twice_is_idempotent(patched_paths):
    claude_path, codex_path = patched_paths

    first = Configurator().register_all()
    assert all(not c.already_present for c in first)

    second = Configurator().register_all()
    # 第二次：两端都应识别为已存在
    assert [c.target for c in second] == ["Claude Desktop", "Codex"]
    assert all(c.success for c in second)
    assert all(c.already_present for c in second)

    # 没有重复写入：Codex 块只出现一次
    text = codex_path.read_text(encoding="utf-8")
    assert text.count(f"[mcp_servers.{_CODEX_SERVER_KEY}]") == 1


def test_unregister_all_removes_both(patched_paths):
    claude_path, codex_path = patched_paths

    Configurator().register_all()
    changes = Configurator().unregister_all()

    assert [c.target for c in changes] == ["Claude Desktop", "Codex"]
    assert all(c.success for c in changes)

    # Claude 端：mcpServers 里不再有我们的 key
    data = json.loads(claude_path.read_text(encoding="utf-8"))
    assert _CLAUDE_SERVER_KEY not in data.get("mcpServers", {})

    # Codex 端：section 与 cc-bridge 标记都被移除
    text = codex_path.read_text(encoding="utf-8")
    assert f"[mcp_servers.{_CODEX_SERVER_KEY}]" not in text


def test_register_preserves_existing_codex_content(patched_paths):
    """注册 Codex 时只追加块，不破坏用户已有的 TOML 配置。"""
    _claude_path, codex_path = patched_paths
    codex_path.parent.mkdir(parents=True, exist_ok=True)
    codex_path.write_text('model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "x"\n', encoding="utf-8")

    change = Configurator().register_in_codex()
    assert change.success
    assert not change.already_present

    parsed = tomllib.loads(codex_path.read_text(encoding="utf-8"))
    # 用户原有内容仍在
    assert parsed["model"] == "gpt-5"
    assert "other" in parsed["mcp_servers"]
    # 我们的块也写进去了
    assert _CODEX_SERVER_KEY in parsed["mcp_servers"]


def test_unregister_when_nothing_registered(patched_paths):
    """两端都没有配置文件时，卸载应成功且不报错。"""
    changes = Configurator().unregister_all()
    assert [c.target for c in changes] == ["Claude Desktop", "Codex"]
    assert all(c.success for c in changes)
    assert all(not c.already_present for c in changes)


def test_register_preserves_existing_claude_content(patched_paths):
    """注册 Claude 时保留用户已有的 mcpServers 与其它顶层字段。"""
    claude_path, _codex_path = patched_paths
    claude_path.parent.mkdir(parents=True, exist_ok=True)
    claude_path.write_text(
        json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "node"}}}),
        encoding="utf-8",
    )

    change = Configurator().register_in_claude_desktop()
    assert change.success

    data = json.loads(claude_path.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert "other" in data["mcpServers"]
    assert _CLAUDE_SERVER_KEY in data["mcpServers"]


def test_register_codex_refuses_corrupt_toml(patched_paths):
    """已有的 config.toml 非法且没有我们的标记时，拒绝写入、保留原文件、如实报失败。"""
    _claude_path, codex_path = patched_paths
    codex_path.parent.mkdir(parents=True, exist_ok=True)
    broken = "model = \nthis is not = valid = toml ["
    codex_path.write_text(broken, encoding="utf-8")

    change = Configurator().register_in_codex()
    assert change.success is False
    assert "TOML" in change.message
    # 原文件一字未改
    assert codex_path.read_text(encoding="utf-8") == broken


def test_no_leftover_tmp_file_after_register(patched_paths):
    """原子写不应残留 .cc-bridge.tmp 临时文件。"""
    claude_path, codex_path = patched_paths
    Configurator().register_all()
    for d in (claude_path.parent, codex_path.parent):
        assert not list(d.glob("*.cc-bridge.tmp"))


def test_register_claude_rejects_non_object_mcpservers(patched_paths):
    """mcpServers 是合法 JSON 但不是对象（如 []）→ 返回 success=False，不崩、不覆盖。"""
    claude_path, _codex = patched_paths
    claude_path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({"mcpServers": []})
    claude_path.write_text(original, encoding="utf-8")

    change = Configurator().register_in_claude_desktop()
    assert change.success is False
    assert "mcpServers" in change.message
    assert claude_path.read_text(encoding="utf-8") == original  # 原文件一字未改


def test_register_claude_rejects_non_object_toplevel(patched_paths):
    """顶层是合法 JSON 但不是对象（如 []）→ success=False，不覆盖用户文件。"""
    claude_path, _codex = patched_paths
    claude_path.parent.mkdir(parents=True, exist_ok=True)
    original = "[1, 2, 3]"
    claude_path.write_text(original, encoding="utf-8")

    change = Configurator().register_in_claude_desktop()
    assert change.success is False
    assert claude_path.read_text(encoding="utf-8") == original


def test_unregister_claude_rejects_non_object_toplevel(patched_paths):
    claude_path, _codex = patched_paths
    claude_path.parent.mkdir(parents=True, exist_ok=True)
    claude_path.write_text('"just a string"', encoding="utf-8")

    change = Configurator().unregister_claude_desktop()
    assert change.success is False


def test_register_claude_preserves_user_env_on_update(patched_paths):
    """command 变化需要刷新时，必须保留用户在该记录上加的 env，不能静默清空。"""
    claude_path, _codex = patched_paths
    claude_path.parent.mkdir(parents=True, exist_ok=True)
    claude_path.write_text(
        json.dumps({"mcpServers": {_CLAUDE_SERVER_KEY: {
            "command": "OLD-PYTHON", "args": ["x"], "env": {"K": "V"}}}}),
        encoding="utf-8",
    )

    change = Configurator().register_in_claude_desktop()
    assert change.success and not change.already_present

    entry = json.loads(claude_path.read_text(encoding="utf-8"))["mcpServers"][_CLAUDE_SERVER_KEY]
    assert entry["command"] == sys.executable                 # command/args 被更新
    assert entry["args"] == ["-m", _CLAUDE_SERVER_MODULE]
    assert entry["env"] == {"K": "V"}                          # 但用户 env 保留


def test_register_claude_no_write_when_unchanged_keeps_env(patched_paths):
    """command/args/env 都没变 → already_present、不写盘、env 原样保留。"""
    claude_path, _codex = patched_paths
    claude_path.parent.mkdir(parents=True, exist_ok=True)
    cmd, args = config.mcp_launch_command(_CLAUDE_SERVER_MODULE)
    claude_path.write_text(
        json.dumps({"mcpServers": {_CLAUDE_SERVER_KEY: {
            "command": cmd, "args": args, "env": {"CC_BRIDGE_TIMEOUT": "600"}}}}),
        encoding="utf-8",
    )

    change = Configurator().register_in_claude_desktop()
    assert change.already_present is True

    entry = json.loads(claude_path.read_text(encoding="utf-8"))["mcpServers"][_CLAUDE_SERVER_KEY]
    assert entry["env"] == {"CC_BRIDGE_TIMEOUT": "600"}


def test_register_claude_merges_safety_env_and_preserves_user_env(patched_paths):
    claude_path, _codex = patched_paths
    claude_path.parent.mkdir(parents=True, exist_ok=True)
    cmd, args = config.mcp_launch_command(_CLAUDE_SERVER_MODULE)
    claude_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    _CLAUDE_SERVER_KEY: {
                        "command": cmd,
                        "args": args,
                        "env": {"USER_KEY": "keep", "CC_BRIDGE_TIMEOUT": "600"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    change = Configurator().register_in_claude_desktop(
        env={"CC_BRIDGE_ALLOWED_ROOTS": "C:/repo", "CC_BRIDGE_TIMEOUT": "900"}
    )

    assert change.success
    entry = json.loads(claude_path.read_text(encoding="utf-8"))["mcpServers"][_CLAUDE_SERVER_KEY]
    assert entry["env"] == {
        "USER_KEY": "keep",
        "CC_BRIDGE_TIMEOUT": "900",
        "CC_BRIDGE_ALLOWED_ROOTS": "C:/repo",
    }


def test_register_claude_tolerates_non_dict_existing_env(patched_paths):
    """已存在记录的 env 被用户写成非 dict（如字符串）时，写安全 env 不能抛异常：
    应从空 env 起步合并新键，绝不让 dict() 的 TypeError 逃出本方法的 OSError 兜底。"""
    claude_path, _codex = patched_paths
    claude_path.parent.mkdir(parents=True, exist_ok=True)
    cmd, args = config.mcp_launch_command(_CLAUDE_SERVER_MODULE)
    claude_path.write_text(
        json.dumps({"mcpServers": {_CLAUDE_SERVER_KEY: {
            "command": cmd, "args": args, "env": "not-a-dict"}}}),
        encoding="utf-8",
    )

    change = Configurator().register_in_claude_desktop(
        env={"CC_BRIDGE_AUDIT_LOG": "C:/log"}
    )

    assert change.success
    entry = json.loads(claude_path.read_text(encoding="utf-8"))["mcpServers"][_CLAUDE_SERVER_KEY]
    assert entry["env"] == {"CC_BRIDGE_AUDIT_LOG": "C:/log"}


def test_register_codex_writes_env_block_with_literal_windows_path(patched_paths):
    _claude_path, codex_path = patched_paths

    change = Configurator().register_in_codex(
        env={"CC_BRIDGE_ALLOWED_ROOTS": r"C:\Users\me\repo"}
    )

    assert change.success
    text = codex_path.read_text(encoding="utf-8")
    assert "env = {" in text
    assert "CC_BRIDGE_ALLOWED_ROOTS = 'C:\\Users\\me\\repo'" in text


def test_register_without_env_keeps_default_env_shape(patched_paths):
    claude_path, codex_path = patched_paths

    Configurator().register_all()

    entry = json.loads(claude_path.read_text(encoding="utf-8"))["mcpServers"][_CLAUDE_SERVER_KEY]
    assert entry["env"] == {}
    assert "env = {" not in codex_path.read_text(encoding="utf-8")


def test_atomic_write_cleans_tmp_on_replace_failure(tmp_path, monkeypatch):
    """os.replace 失败（如桌面应用占用文件）时不得残留 .cc-bridge.tmp。"""
    from cc_bridge.installer import configurator as cfg

    def boom(src, dst):
        raise PermissionError("file is locked by the desktop app")

    monkeypatch.setattr(cfg.os, "replace", boom)
    target = tmp_path / "claude_desktop_config.json"
    with pytest.raises(OSError):
        cfg._atomic_write_text(target, "some data")
    assert not list(tmp_path.glob("*.cc-bridge.tmp"))  # 临时文件已被清理
