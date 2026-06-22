"""mcp_to_claude 单元测试（离线）.

monkeypatch：
- AgentExecutor.run_claude 替换成 async stub，返回预制的 ExecutionResult；
- ContextBuilder.build_project_context 返回轻量 ProjectContext，避免真实 git/fs。

验证 claude_analyze 的成功路径、异常路径都返回字符串（绝不抛异常），
claude_status 返回非空字符串。
"""

from __future__ import annotations

import pytest

from cc_bridge.bridge import mcp_to_claude
from cc_bridge.bridge.context import ContextBuilder, ProjectContext
from cc_bridge.bridge.executor import AgentExecutor, ExecutionResult
from cc_bridge.bridge.status import AgentReadiness


@pytest.fixture(autouse=True)
def light_context(monkeypatch):
    """ContextBuilder 返回轻量上下文，避免真实 git / 文件系统扫描。"""
    def _fake_ctx(self, cwd):
        return ProjectContext(root=str(cwd), language="Python", tree="(root)\n")

    monkeypatch.setattr(ContextBuilder, "build_project_context", _fake_ctx)


def _patch_run_claude(monkeypatch, result=None, exc=None):
    async def _fake_run_claude(self, prompt, cwd, timeout=None, on_progress=None):
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(AgentExecutor, "run_claude", _fake_run_claude)


async def test_claude_analyze_success(monkeypatch, tmp_path):
    _patch_run_claude(
        monkeypatch,
        result=ExecutionResult(
            success=True,
            output="架构评审完成，未发现重大风险。",
            files_changed=["docs/review.md"],
            duration_seconds=5.0,
        ),
    )

    out = await mcp_to_claude.claude_analyze("评审架构", project_dir=str(tmp_path))

    assert isinstance(out, str)
    assert "已完成" in out
    assert "改动文件" in out
    assert "docs/review.md" in out


async def test_claude_analyze_failure_result_is_string(monkeypatch, tmp_path):
    """executor 返回失败结果（非异常）→ 仍是友好字符串，含「未能完成」。"""
    _patch_run_claude(
        monkeypatch,
        result=ExecutionResult(
            success=False,
            output="",
            error="rate limit exceeded",
            exit_code=1,
            raw_stderr="429 too many requests",
        ),
    )

    out = await mcp_to_claude.claude_analyze("做点事", project_dir=str(tmp_path))
    assert isinstance(out, str)
    assert "未能完成" in out


async def test_claude_analyze_swallows_exception(monkeypatch, tmp_path):
    """executor 抛异常 → 不向 MCP 抛，返回字符串。"""
    _patch_run_claude(monkeypatch, exc=RuntimeError("kaboom"))

    out = await mcp_to_claude.claude_analyze("触发异常", project_dir=str(tmp_path))
    assert isinstance(out, str)
    assert out
    assert "kaboom" in out


async def test_claude_analyze_rejects_missing_project_dir(monkeypatch):
    """不传 project_dir → 直接拒绝，绝不擅自用进程 cwd。"""
    async def _boom(self, *a, **k):
        raise AssertionError("project_dir 缺失时不应真正调用 Claude")

    monkeypatch.setattr(AgentExecutor, "run_claude", _boom)
    out = await mcp_to_claude.claude_analyze("无目录")
    assert isinstance(out, str)
    assert "project_dir" in out


async def test_claude_analyze_rejects_relative_project_dir(monkeypatch):
    async def _boom(self, *a, **k):
        raise AssertionError("相对路径时不应真正调用 Claude")

    monkeypatch.setattr(AgentExecutor, "run_claude", _boom)
    out = await mcp_to_claude.claude_analyze("x", project_dir="relative/dir")
    assert "绝对路径" in out


async def test_claude_status_ready(monkeypatch):
    monkeypatch.setattr(
        mcp_to_claude, "check_claude",
        lambda: AgentReadiness("Claude", "C:/fake/claude", "claude 1.0.0", True, True),
    )
    out = await mcp_to_claude.claude_status()
    assert isinstance(out, str) and out.strip()
    assert "claude 1.0.0" in out
    assert "未就绪" not in out


async def test_claude_status_not_ready(monkeypatch):
    monkeypatch.setattr(
        mcp_to_claude, "check_claude",
        lambda: AgentReadiness("Claude", "C:/fake/claude", "claude 1.0.0", False, True),
    )
    out = await mcp_to_claude.claude_status()
    assert "未就绪" in out


async def test_claude_status_swallows_exception(monkeypatch):
    def _boom():
        raise RuntimeError("status failed")

    monkeypatch.setattr(mcp_to_claude, "check_claude", _boom)

    out = await mcp_to_claude.claude_status()
    assert isinstance(out, str)
    assert out.strip()


def test_mcp_server_metadata():
    """模块级 mcp 是名为 bridge-to-claude 的 FastMCP；main 可调用。"""
    assert mcp_to_claude.mcp.name == "bridge-to-claude"
    assert callable(mcp_to_claude.main)


async def test_claude_analyze_ctx_is_injected_not_input_schema():
    from cc_bridge.bridge.mcp_to_claude import mcp

    assert mcp._tool_manager._tools["claude_analyze"].context_kwarg == "ctx"
    tools = await mcp.list_tools()
    claude_tool = next(tool for tool in tools if tool.name == "claude_analyze")
    assert "ctx" not in claude_tool.inputSchema.get("properties", {})


async def test_claude_status_login_unknown(monkeypatch):
    monkeypatch.setattr(
        mcp_to_claude, "check_claude",
        lambda: AgentReadiness("Claude", "C:/fake/claude", "claude 1.0.0", False, False),
    )
    out = await mcp_to_claude.claude_status()
    assert "登录态未确认" in out
    assert "尚未就绪" not in out
