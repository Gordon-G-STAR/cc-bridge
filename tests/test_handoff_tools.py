"""PR1 —— codex_handoff / claude_handoff 工具骨架测试。

骨架的安全姿态:一律【只读】执行(codex read-only / claude plan),返回结构化
HandoffResult,fail-closed,绝不向 MCP 抛异常。
"""

from __future__ import annotations

import contextlib

import pytest

from cc_bridge.bridge import config, mcp_to_claude, mcp_to_codex
from cc_bridge.bridge.context import ContextBuilder, ProjectContext
from cc_bridge.bridge.contracts import (
    FailureKind,
    HandoffRequest,
    HandoffResult,
    RequestedScope,
)
from cc_bridge.bridge.executor import AgentExecutor, ExecutionResult
from cc_bridge.bridge.locks import LockBusy


@pytest.fixture(autouse=True)
def light_context(monkeypatch):
    def _fake_ctx(self, cwd):
        return ProjectContext(root=str(cwd), language="Python", tree="(root)\n")

    monkeypatch.setattr(ContextBuilder, "build_project_context", _fake_ctx)


@pytest.fixture(autouse=True)
def clear_caches_and_audit(monkeypatch):
    monkeypatch.delenv("CC_BRIDGE_AUDIT_LOG", raising=False)
    for store in (
        mcp_to_codex._CODEX_SESSIONS_BY_PROJECT,
        mcp_to_codex._CODEX_SESSION_LOCKS_BY_PROJECT,
        mcp_to_claude._CLAUDE_SESSIONS_BY_PROJECT,
        mcp_to_claude._CLAUDE_SESSION_LOCKS_BY_PROJECT,
    ):
        store.clear()


@pytest.fixture(autouse=True)
def isolated_app_dir(monkeypatch, tmp_path):
    app_dir = tmp_path / "app"
    monkeypatch.setattr(config, "stable_app_dir", lambda: app_dir)


def _req(writable=None) -> HandoffRequest:
    return HandoffRequest(
        contract_version="1",
        goal="评审 auth 模块",
        acceptance_criteria=["不改变公共 API"],
        requested_scope=RequestedScope(writable_paths=writable or []),
    )


# ---------------------------------------------------------------------------
# codex_handoff
# ---------------------------------------------------------------------------

async def test_codex_handoff_forces_read_only_and_returns_structured(monkeypatch, tmp_path):
    calls: list[dict] = []

    async def _fake(self, prompt, cwd, **kwargs):
        calls.append(kwargs)
        return ExecutionResult(
            success=True, output="分析完成", duration_seconds=2.0, token_usage={"t": 1}
        )

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake)

    res = await mcp_to_codex.codex_handoff(_req(), project_dir=str(tmp_path))

    assert isinstance(res, HandoffResult)
    assert res.status == "completed"
    assert res.agent_used == "codex"
    assert res.evidence_level == "unknown"        # 无证据子系统,绝不声称 verified
    assert res.verified_files_changed == []
    assert res.token_usage == {"t": 1}
    assert calls[0]["sandbox_override"] == "read-only"  # 骨架强制只读


async def test_codex_handoff_lock_busy_is_project_busy(monkeypatch, tmp_path):
    @contextlib.asynccontextmanager
    async def _busy_lock(*_args, **_kwargs):
        raise LockBusy("busy")
        yield

    async def _unexpected(self, *args, **kwargs):
        raise AssertionError("Codex should not run when project lock is busy")

    monkeypatch.setattr(mcp_to_codex, "async_project_lock", _busy_lock)
    monkeypatch.setattr(AgentExecutor, "run_codex", _unexpected)

    res = await mcp_to_codex.codex_handoff(_req(), project_dir=str(tmp_path))

    assert res.status == "failed"
    assert res.failure_kind is FailureKind.project_busy
    assert (
        res.route_reason
        == "Another bridge process is handling this project; try again shortly."
    )


async def test_codex_handoff_ignores_requested_writes_but_notes_it(monkeypatch, tmp_path):
    calls: list[dict] = []

    async def _fake(self, prompt, cwd, **kwargs):
        calls.append(kwargs)
        return ExecutionResult(success=True, output="ok", duration_seconds=1.0)

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake)

    res = await mcp_to_codex.codex_handoff(
        _req(writable=["src/auth"]), project_dir=str(tmp_path)
    )
    assert calls[0]["sandbox_override"] == "read-only"   # 申请了写,仍只读
    assert "只读" in res.route_reason
    assert "忽略" in res.route_reason


async def test_codex_handoff_bad_project_dir_is_fail_closed(monkeypatch):
    async def _boom(self, *a, **k):
        raise AssertionError("project_dir 非法时不应调用 Codex")

    monkeypatch.setattr(AgentExecutor, "run_codex", _boom)

    res = await mcp_to_codex.codex_handoff(_req(), project_dir="relative/dir")
    assert isinstance(res, HandoffResult)
    assert res.status == "failed"
    assert res.failure_kind is FailureKind.invalid_contract


async def test_codex_handoff_swallows_exception(monkeypatch, tmp_path):
    async def _boom(self, *a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(AgentExecutor, "run_codex", _boom)

    res = await mcp_to_codex.codex_handoff(_req(), project_dir=str(tmp_path))
    assert res.status == "failed"
    assert res.failure_kind is FailureKind.crashed


async def test_codex_handoff_timeout_maps_failure_kind(monkeypatch, tmp_path):
    async def _fake(self, prompt, cwd, **kwargs):
        return ExecutionResult(
            success=False, output="", timed_out=True, duration_seconds=1.0
        )

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake)

    res = await mcp_to_codex.codex_handoff(_req(), project_dir=str(tmp_path))
    assert res.status == "failed"
    assert res.failure_kind is FailureKind.timeout


# ---------------------------------------------------------------------------
# claude_handoff
# ---------------------------------------------------------------------------

async def test_claude_handoff_forces_plan_mode(monkeypatch, tmp_path):
    calls: list[dict] = []

    async def _fake(self, prompt, cwd, **kwargs):
        calls.append(kwargs)
        return ExecutionResult(success=True, output="评审完成", duration_seconds=2.0)

    monkeypatch.setattr(AgentExecutor, "run_claude", _fake)

    res = await mcp_to_claude.claude_handoff(
        _req(writable=["src/x"]), project_dir=str(tmp_path)
    )
    assert isinstance(res, HandoffResult)
    assert res.agent_used == "claude"
    assert res.evidence_level == "unknown"
    assert calls[0]["permission_override"] == "plan"   # 骨架强制只读 plan


async def test_claude_handoff_lock_busy_is_project_busy(monkeypatch, tmp_path):
    @contextlib.asynccontextmanager
    async def _busy_lock(*_args, **_kwargs):
        raise LockBusy("busy")
        yield

    async def _unexpected(self, *args, **kwargs):
        raise AssertionError("Claude should not run when project lock is busy")

    monkeypatch.setattr(mcp_to_claude, "async_project_lock", _busy_lock)
    monkeypatch.setattr(AgentExecutor, "run_claude", _unexpected)

    res = await mcp_to_claude.claude_handoff(_req(), project_dir=str(tmp_path))

    assert res.status == "failed"
    assert res.failure_kind is FailureKind.project_busy
    assert (
        res.route_reason
        == "Another bridge process is handling this project; try again shortly."
    )


async def test_claude_handoff_bad_project_dir_is_fail_closed(monkeypatch):
    async def _boom(self, *a, **k):
        raise AssertionError("project_dir 非法时不应调用 Claude")

    monkeypatch.setattr(AgentExecutor, "run_claude", _boom)

    res = await mcp_to_claude.claude_handoff(_req(), project_dir="relative/dir")
    assert res.status == "failed"
    assert res.failure_kind is FailureKind.invalid_contract


async def test_claude_handoff_swallows_exception(monkeypatch, tmp_path):
    async def _boom(self, *a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(AgentExecutor, "run_claude", _boom)

    res = await mcp_to_claude.claude_handoff(_req(), project_dir=str(tmp_path))
    assert res.status == "failed"
    assert res.failure_kind is FailureKind.crashed


async def test_claude_handoff_timeout_maps_kind(monkeypatch, tmp_path):
    async def _fake(self, prompt, cwd, **kwargs):
        return ExecutionResult(
            success=False, output="", timed_out=True, duration_seconds=1.0
        )

    monkeypatch.setattr(AgentExecutor, "run_claude", _fake)

    res = await mcp_to_claude.claude_handoff(_req(), project_dir=str(tmp_path))
    assert res.status == "failed"
    assert res.failure_kind is FailureKind.timeout


# ---------------------------------------------------------------------------
# Codex 复审驱动:agent_unavailable 映射、续接、以及"只读却报告改动"的异常防御
# ---------------------------------------------------------------------------

async def test_codex_handoff_agent_unavailable_maps_kind(monkeypatch, tmp_path):
    async def _fake(self, prompt, cwd, **kwargs):
        # exit_code=None 且非超时、非成功 => agent_unavailable
        return ExecutionResult(
            success=False, output="", exit_code=None, duration_seconds=0.1
        )

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake)

    res = await mcp_to_codex.codex_handoff(_req(), project_dir=str(tmp_path))
    assert res.status == "failed"
    assert res.failure_kind is FailureKind.agent_unavailable


async def test_codex_handoff_continue_session_reuses(monkeypatch, tmp_path):
    calls: list[str | None] = []

    async def _fake(self, prompt, cwd, **kwargs):
        calls.append(kwargs.get("resume_session_id"))
        return ExecutionResult(
            success=True,
            output="ok",
            session_id=kwargs.get("resume_session_id") or "sid-1",
            duration_seconds=1.0,
        )

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake)

    await mcp_to_codex.codex_handoff(_req(), project_dir=str(tmp_path))
    await mcp_to_codex.codex_handoff(
        _req(), project_dir=str(tmp_path), continue_session=True
    )
    assert calls == [None, "sid-1"]


async def test_codex_handoff_readonly_anomaly_is_flagged(monkeypatch, tmp_path):
    """只读执行却报告文件改动 => 必须显式标成 scope_violation,绝不静默吞掉。"""

    async def _fake(self, prompt, cwd, **kwargs):
        return ExecutionResult(
            success=True, output="ok", files_changed=["src/x.py"], duration_seconds=1.0
        )

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake)

    res = await mcp_to_codex.codex_handoff(_req(), project_dir=str(tmp_path))
    assert res.status == "scope_violation"
    assert res.scope_violations == ["src/x.py"]
    assert res.failure_kind is FailureKind.scope_violation
    assert res.verified_files_changed == []   # 仍不声称 verified
