"""mcp_to_claude 单元测试（离线）.

monkeypatch：
- AgentExecutor.run_claude 替换成 async stub，返回预制的 ExecutionResult；
- ContextBuilder.build_project_context 返回轻量 ProjectContext，避免真实 git/fs。

验证 claude_analyze 的成功路径、异常路径都返回字符串（绝不抛异常），
claude_status 返回非空字符串。
"""

from __future__ import annotations

import asyncio

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


@pytest.fixture(autouse=True)
def clean_policy_env(monkeypatch):
    """从默认本地策略出发,清掉可能从开发机继承的 policy / 链路 env。"""
    for name in (
        "CC_BRIDGE_POLICY_WRITABLE_PATHS",
        "CC_BRIDGE_POLICY_READONLY",
        "CC_BRIDGE_POLICY_ALLOW_NETWORK",
        "CC_BRIDGE_POLICY_MAX_DEPTH",
        "CC_BRIDGE_POLICY_REQUIRE_APPROVAL",
        "CC_BRIDGE_LEGACY_TOOLS",
        "CC_BRIDGE_CHAIN_DEPTH",
        "CC_BRIDGE_CHAIN_SCOPE",
        "CC_BRIDGE_CODEX_SANDBOX",
        "CC_BRIDGE_CLAUDE_PERMISSION",
    ):
        monkeypatch.delenv(name, raising=False)


def _patch_run_claude(monkeypatch, result=None, exc=None):
    async def _fake_run_claude(
        self, prompt, cwd, timeout=None, resume_session_id=None, on_progress=None,
        **kwargs,
    ):
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(AgentExecutor, "run_claude", _fake_run_claude)


@pytest.fixture(autouse=True)
def clear_claude_session_cache():
    if hasattr(mcp_to_claude, "_CLAUDE_SESSIONS_BY_PROJECT"):
        mcp_to_claude._CLAUDE_SESSIONS_BY_PROJECT.clear()
    if hasattr(mcp_to_claude, "_CLAUDE_SESSION_LOCKS_BY_PROJECT"):
        mcp_to_claude._CLAUDE_SESSION_LOCKS_BY_PROJECT.clear()
    yield
    if hasattr(mcp_to_claude, "_CLAUDE_SESSIONS_BY_PROJECT"):
        mcp_to_claude._CLAUDE_SESSIONS_BY_PROJECT.clear()
    if hasattr(mcp_to_claude, "_CLAUDE_SESSION_LOCKS_BY_PROJECT"):
        mcp_to_claude._CLAUDE_SESSION_LOCKS_BY_PROJECT.clear()


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


async def test_claude_analyze_default_does_not_add_dry_run_controls(
    monkeypatch, tmp_path
):
    calls: list[dict] = []

    async def _fake_run_claude(self, prompt, cwd, **kwargs):
        calls.append({"prompt": prompt, "kwargs": kwargs})
        return ExecutionResult(success=True, output="ok", duration_seconds=1.0)

    monkeypatch.setattr(AgentExecutor, "run_claude", _fake_run_claude)

    out = await mcp_to_claude.claude_analyze("normal task", project_dir=str(tmp_path))

    assert "dry-run" not in out
    assert "预演" not in out
    # PR5:legacy 工具走 policy 地板;默认(未收紧)仍是 bypassPermissions,但显式流过。
    assert calls[0]["kwargs"]["permission_override"] == "bypassPermissions"
    assert "dry run" not in calls[0]["prompt"].lower()
    assert "不要真正修改文件" not in calls[0]["prompt"]


async def test_claude_analyze_dry_run_uses_plan_and_marks_prompt_and_summary(
    monkeypatch, tmp_path
):
    calls: list[dict] = []

    async def _fake_run_claude(self, prompt, cwd, **kwargs):
        calls.append({"prompt": prompt, "kwargs": kwargs})
        return ExecutionResult(
            success=True,
            output="would edit docs/review.md",
            duration_seconds=1.0,
        )

    monkeypatch.setattr(AgentExecutor, "run_claude", _fake_run_claude)

    out = await mcp_to_claude.claude_analyze(
        "plan an edit", project_dir=str(tmp_path), dry_run=True
    )

    assert calls[0]["kwargs"]["permission_override"] == "plan"
    assert "dry run" in calls[0]["prompt"].lower()
    assert "不要真正修改文件" in calls[0]["prompt"]
    assert "dry-run" in out
    assert "预演" in out


async def test_claude_analyze_returns_and_reuses_session_id(monkeypatch, tmp_path):
    calls: list[str | None] = []

    async def _fake_run_claude(
        self, prompt, cwd, timeout=None, resume_session_id=None, on_progress=None,
        **kwargs,
    ):
        calls.append(resume_session_id)
        return ExecutionResult(
            success=True,
            output="ok",
            duration_seconds=1.0,
            session_id=resume_session_id or "sid-first",
        )

    monkeypatch.setattr(AgentExecutor, "run_claude", _fake_run_claude)

    first = await mcp_to_claude.claude_analyze("first", project_dir=str(tmp_path))
    second = await mcp_to_claude.claude_analyze(
        "second", project_dir=str(tmp_path), continue_session=True
    )

    assert calls == [None, "sid-first"]
    assert "sid-first" in first
    assert "sid-first" in second


async def test_claude_analyze_continue_session_serializes_per_project(
    monkeypatch, tmp_path
):
    cwd = str(tmp_path.resolve())
    mcp_to_claude._CLAUDE_SESSIONS_BY_PROJECT[cwd] = "sid-old"
    calls: list[str | None] = []
    active = 0
    max_active = 0

    async def _fake_run_claude(
        self, prompt, cwd, timeout=None, resume_session_id=None, on_progress=None,
        **kwargs,
    ):
        nonlocal active, max_active
        calls.append(resume_session_id)
        call_no = len(calls)
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.05)
        finally:
            active -= 1
        return ExecutionResult(
            success=True,
            output="ok",
            duration_seconds=1.0,
            session_id="sid-first" if call_no == 1 else "sid-second",
        )

    monkeypatch.setattr(AgentExecutor, "run_claude", _fake_run_claude)

    await asyncio.gather(
        mcp_to_claude.claude_analyze(
            "continue 1", project_dir=str(tmp_path), continue_session=True
        ),
        mcp_to_claude.claude_analyze(
            "continue 2", project_dir=str(tmp_path), continue_session=True
        ),
    )

    assert max_active == 1
    assert calls == ["sid-old", "sid-first"]
    assert mcp_to_claude._CLAUDE_SESSIONS_BY_PROJECT[cwd] == "sid-second"


async def test_claude_analyze_continue_false_starts_new_session(monkeypatch, tmp_path):
    cwd = str(tmp_path.resolve())
    mcp_to_claude._CLAUDE_SESSIONS_BY_PROJECT[cwd] = "sid-old"
    calls: list[str | None] = []

    async def _fake_run_claude(
        self, prompt, cwd, timeout=None, resume_session_id=None, on_progress=None,
        **kwargs,
    ):
        calls.append(resume_session_id)
        return ExecutionResult(success=True, output="ok", session_id="sid-new")

    monkeypatch.setattr(AgentExecutor, "run_claude", _fake_run_claude)

    out = await mcp_to_claude.claude_analyze(
        "no continue", project_dir=str(tmp_path), continue_session=False
    )

    assert calls == [None]
    assert mcp_to_claude._CLAUDE_SESSIONS_BY_PROJECT[cwd] == "sid-new"
    assert "sid-new" in out


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
    assert "dry_run" in claude_tool.inputSchema.get("properties", {})


async def test_claude_handoff_exposes_input_and_output_schema():
    """PR6:结构化委派的输入 / 输出 schema 都对 MCP Inspector 可见(版本化)。"""
    from cc_bridge.bridge.mcp_to_claude import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    handoff = tools["claude_handoff"]
    assert "request" in handoff.inputSchema.get("properties", {})
    assert "HandoffRequest" in (handoff.inputSchema.get("$defs") or {})
    assert "ctx" not in handoff.inputSchema.get("properties", {})
    out = getattr(handoff, "outputSchema", None)
    assert out is not None
    assert "status" in (out.get("properties") or {})


@pytest.fixture(autouse=True)
def clear_audit_log_env(monkeypatch):
    monkeypatch.delenv("CC_BRIDGE_AUDIT_LOG", raising=False)


async def test_claude_analyze_writes_audit_log_when_enabled(monkeypatch, tmp_path):
    import json

    log_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("CC_BRIDGE_AUDIT_LOG", str(log_path))
    _patch_run_claude(
        monkeypatch,
        result=ExecutionResult(
            success=False,
            output="nope",
            files_changed=["docs/review.md"],
            duration_seconds=1.0,
        ),
    )

    await mcp_to_claude.claude_analyze("audit me", project_dir=str(tmp_path))

    record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["direction"] == "claude"
    assert record["cwd"] == str(tmp_path.resolve())
    assert record["task_summary"] == "audit me"
    assert record["success"] is False
    assert record["files_changed"] == ["docs/review.md"]


async def test_claude_status_login_unknown(monkeypatch):
    monkeypatch.setattr(
        mcp_to_claude, "check_claude",
        lambda: AgentReadiness("Claude", "C:/fake/claude", "claude 1.0.0", False, False),
    )
    out = await mcp_to_claude.claude_status()
    assert "登录态未确认" in out
    assert "尚未就绪" not in out
