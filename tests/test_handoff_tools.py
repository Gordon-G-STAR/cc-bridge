"""PR1 —— codex_handoff / claude_handoff 工具骨架测试。

骨架的安全姿态:一律【只读】执行(codex read-only / claude plan),返回结构化
HandoffResult,fail-closed,绝不向 MCP 抛异常。
"""

from __future__ import annotations

import pytest

from cc_bridge.bridge import mcp_to_claude, mcp_to_codex
from cc_bridge.bridge.context import ContextBuilder, ProjectContext
from cc_bridge.bridge.contracts import (
    FailureKind,
    HandoffRequest,
    HandoffResult,
    RequestedScope,
)
from cc_bridge.bridge.executor import AgentExecutor, ExecutionResult


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


async def test_claude_handoff_bad_project_dir_is_fail_closed(monkeypatch):
    async def _boom(self, *a, **k):
        raise AssertionError("project_dir 非法时不应调用 Claude")

    monkeypatch.setattr(AgentExecutor, "run_claude", _boom)

    res = await mcp_to_claude.claude_handoff(_req(), project_dir="relative/dir")
    assert res.status == "failed"
    assert res.failure_kind is FailureKind.invalid_contract
