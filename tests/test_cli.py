"""cli 单元测试（离线）.

只测纯逻辑与参数解析，不触发真实检测 / 写配置 / 跨 agent 调用：
- version 子命令返回 0；
- status 子命令在「全就绪 / 未就绪」两种情况下分别返回 0 / 1（monkeypatch 检测器）；
- build_parser 能正确解析各子命令并挂上对应 func；
- 无子命令时打印 help 并返回 0。
"""

from __future__ import annotations

import argparse

from cc_bridge import cli
from cc_bridge.installer.detector import CheckResult, EnvironmentStatus


def _check(name: str, ready: bool) -> CheckResult:
    return CheckResult(
        name=name,
        installed=True,
        cli_available=ready,
        logged_in=ready,
        version="1.0.0",
        message=f"{name} {'已就绪' if ready else '未就绪'}。",
    )


def _status(claude_ready: bool, codex_ready: bool) -> EnvironmentStatus:
    return EnvironmentStatus(
        claude=_check("Claude", claude_ready),
        codex=_check("Codex", codex_ready),
    )


def test_version_returns_zero(capsys):
    rc = cli.main(["version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "cc-bridge" in out


def test_status_all_ready_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        "cc_bridge.installer.detector.EnvironmentDetector.check_all",
        lambda self: _status(True, True),
    )
    rc = cli.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "环境检测" in out


def test_status_not_ready_returns_one(monkeypatch, capsys):
    monkeypatch.setattr(
        "cc_bridge.installer.detector.EnvironmentDetector.check_all",
        lambda self: _status(True, False),
    )
    rc = cli.main(["status"])
    assert rc == 1


def test_doctor_is_alias_of_status(monkeypatch):
    monkeypatch.setattr(
        "cc_bridge.installer.detector.EnvironmentDetector.check_all",
        lambda self: _status(True, True),
    )
    assert cli.main(["doctor"]) == 0


def test_no_subcommand_prints_help_returns_zero(capsys):
    rc = cli.main([])
    assert rc == 0
    out = capsys.readouterr().out
    # argparse help 里会包含 prog 名称
    assert "cc-bridge" in out


def test_build_parser_parses_subcommands():
    parser = cli.build_parser()

    args = parser.parse_args(["status"])
    assert args.func is cli.cmd_status

    args = parser.parse_args(["doctor"])
    assert args.func is cli.cmd_status

    args = parser.parse_args(["version"])
    assert args.func is cli.cmd_version

    args = parser.parse_args(["uninstall"])
    assert args.func is cli.cmd_uninstall

    args = parser.parse_args(["test"])
    assert args.func is cli.cmd_test

    args = parser.parse_args(["selftest"])
    assert args.func is cli.cmd_selftest


def test_build_parser_install_flags():
    parser = cli.build_parser()

    args = parser.parse_args(["install"])
    assert args.func is cli.cmd_install
    assert args.no_test is False
    assert args.force is False

    args = parser.parse_args(["install", "--no-test", "--force"])
    assert args.no_test is True
    assert args.force is True


def test_install_passes_safety_env_to_configurator(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        "cc_bridge.installer.detector.EnvironmentDetector.check_all",
        lambda self: _status(True, True),
    )

    def _register_all(self, env=None):
        captured["env"] = env
        return [
            argparse.Namespace(
                target="Claude Desktop", success=True, already_present=False, message="ok"
            ),
            argparse.Namespace(target="Codex", success=True, already_present=False, message="ok"),
        ]

    monkeypatch.setattr(
        "cc_bridge.installer.configurator.Configurator.register_all", _register_all
    )

    rc = cli.main(
        [
            "install",
            "--no-test",
            "--allowed-roots",
            "X",
            "--codex-sandbox",
            "read-only",
            "--audit-log",
            "Y",
        ]
    )

    assert rc == 0
    assert captured["env"] == {
        "CC_BRIDGE_ALLOWED_ROOTS": "X",
        "CC_BRIDGE_CODEX_SANDBOX": "read-only",
        "CC_BRIDGE_AUDIT_LOG": "Y",
    }


def test_install_without_safety_flags_passes_no_env(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        "cc_bridge.installer.detector.EnvironmentDetector.check_all",
        lambda self: _status(True, True),
    )

    def _register_all(self, env=None):
        captured["env"] = env
        return [
            argparse.Namespace(
                target="Claude Desktop", success=True, already_present=False, message="ok"
            ),
            argparse.Namespace(target="Codex", success=True, already_present=False, message="ok"),
        ]

    monkeypatch.setattr(
        "cc_bridge.installer.configurator.Configurator.register_all", _register_all
    )

    rc = cli.main(["install", "--no-test"])

    assert rc == 0
    assert captured["env"] is None


def _selftest_result(success: bool, host: str):
    from cc_bridge.installer.mcp_selftest import McpSelfTestResult

    return McpSelfTestResult(
        server_key="codex",
        host=host,
        success=success,
        detail="OK" if success else "MCP 握手失败",
        duration_seconds=1.0,
        tools_found=["codex_execute", "codex_status"] if success else [],
    )


def test_cmd_selftest_all_ok_returns_zero(monkeypatch, capsys):
    from cc_bridge.installer import mcp_selftest

    monkeypatch.setattr(
        mcp_selftest,
        "selftest_all",
        lambda *a, **k: [
            _selftest_result(True, "Claude 桌面版"),
            _selftest_result(True, "Codex"),
        ],
    )
    assert cli.cmd_selftest(argparse.Namespace()) == 0
    assert "✅" in capsys.readouterr().out


def test_cmd_selftest_failure_returns_one(monkeypatch):
    from cc_bridge.installer import mcp_selftest

    monkeypatch.setattr(
        mcp_selftest,
        "selftest_all",
        lambda *a, **k: [
            _selftest_result(True, "Claude 桌面版"),
            _selftest_result(False, "Codex"),
        ],
    )
    assert cli.cmd_selftest(argparse.Namespace()) == 1


def test_cmd_status_directly(monkeypatch):
    """直接调用 cmd_status，覆盖未就绪分支返回 1。"""
    monkeypatch.setattr(
        "cc_bridge.installer.detector.EnvironmentDetector.check_all",
        lambda self: _status(False, False),
    )
    parser = cli.build_parser()
    args = parser.parse_args(["status"])
    assert args.func(args) == 1
