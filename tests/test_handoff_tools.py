"""codex_handoff / claude_handoff 工具层测试(PR1 骨架 → PR5 策略重授权)。

安全姿态:输入输出结构化,fail-closed,绝不向 MCP 抛异常。自 PR5 起,写入由本地策略
重授权(无写申请 / 只读策略 => 只读;申请项目内写 => 授权;链深超限 / 需审批 => fail-closed)。
"""

from __future__ import annotations

import contextlib
from pathlib import Path
import subprocess

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


def _hide_parent_git_repo(monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(project_root))


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


@pytest.fixture(autouse=True)
def clean_policy_env(monkeypatch):
    """每个用例从【默认本地策略】出发:清掉一切可能从开发机继承的 policy / 链路 env。"""
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


def _req(writable=None) -> HandoffRequest:
    return HandoffRequest(
        contract_version="1",
        goal="评审 auth 模块",
        acceptance_criteria=["不改变公共 API"],
        requested_scope=RequestedScope(writable_paths=writable or []),
    )


def _run_git(repo, *args) -> None:
    git = config.resolve_cli("git")
    if git is None:
        pytest.skip("git is required for snapshot evidence tests")
    subprocess.run(
        [git, *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _init_git_repo(repo) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _run_git(repo, "add", "README.md")
    _run_git(
        repo,
        "-c",
        "user.name=cc-bridge test",
        "-c",
        "user.email=test@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "init",
    )


# ---------------------------------------------------------------------------
# codex_handoff
# ---------------------------------------------------------------------------

async def test_codex_handoff_no_write_request_is_read_only_and_structured(monkeypatch, tmp_path):
    _hide_parent_git_repo(monkeypatch)
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
    assert res.evidence_level == "unknown"
    assert res.verified_files_changed == []
    assert res.token_usage == {"t": 1}
    assert calls[0]["sandbox_override"] == "read-only"  # 无写申请 => 只读


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


async def test_codex_handoff_grants_requested_writes_under_default_policy(monkeypatch, tmp_path):
    """PR5:默认本地策略(项目根可写)下,申请项目内写入 => 授予 workspace-write。"""
    calls: list[dict] = []

    async def _fake(self, prompt, cwd, **kwargs):
        calls.append(kwargs)
        return ExecutionResult(success=True, output="ok", duration_seconds=1.0)

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake)

    res = await mcp_to_codex.codex_handoff(
        _req(writable=["src/auth"]), project_dir=str(tmp_path)
    )
    assert calls[0]["sandbox_override"] == "workspace-write"   # 被授权写
    # 链路 env 被下传:depth+1 + 授予的 scope。
    assert calls[0]["extra_env"]["CC_BRIDGE_CHAIN_DEPTH"] == "1"
    assert "src/auth" in calls[0]["extra_env"]["CC_BRIDGE_CHAIN_SCOPE"]
    assert "可写" in res.route_reason


async def test_codex_handoff_readonly_policy_downgrades_writes(monkeypatch, tmp_path):
    """CC_BRIDGE_POLICY_READONLY=1 => 即便申请写,也降级为只读(README 改不了它)。"""
    monkeypatch.setenv("CC_BRIDGE_POLICY_READONLY", "1")
    calls: list[dict] = []

    async def _fake(self, prompt, cwd, **kwargs):
        calls.append(kwargs)
        return ExecutionResult(success=True, output="ok", duration_seconds=1.0)

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake)

    res = await mcp_to_codex.codex_handoff(
        _req(writable=["src/auth"]), project_dir=str(tmp_path)
    )
    assert calls[0]["sandbox_override"] == "read-only"
    assert "只读" in res.route_reason


async def test_codex_handoff_depth_limit_denies(monkeypatch, tmp_path):
    """链深 >= 上限 => policy_denied,绝不执行(防无界再入)。"""
    monkeypatch.setenv("CC_BRIDGE_POLICY_MAX_DEPTH", "2")
    monkeypatch.setenv("CC_BRIDGE_CHAIN_DEPTH", "2")

    async def _boom(self, *a, **k):
        raise AssertionError("链深超限时绝不应调用 Codex")

    monkeypatch.setattr(AgentExecutor, "run_codex", _boom)

    res = await mcp_to_codex.codex_handoff(_req(writable=["src"]), project_dir=str(tmp_path))
    assert res.status == "policy_denied"
    assert res.failure_kind is FailureKind.policy_denied


async def test_codex_handoff_approval_required_when_headless(monkeypatch, tmp_path):
    """要求审批 + headless(默认拒绝者)=> approval_required,fail-closed。"""
    monkeypatch.setenv("CC_BRIDGE_POLICY_REQUIRE_APPROVAL", "1")

    async def _boom(self, *a, **k):
        raise AssertionError("需审批且无审批者时绝不应执行")

    monkeypatch.setattr(AgentExecutor, "run_codex", _boom)

    res = await mcp_to_codex.codex_handoff(
        _req(writable=["src/auth"]), project_dir=str(tmp_path)
    )
    assert res.status == "approval_required"
    assert res.agent_used == "codex"


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

async def test_claude_handoff_readonly_when_no_writes_requested(monkeypatch, tmp_path):
    """没有申请写 => 只读 plan 模式(不再是骨架强制,而是策略推导)。"""
    _hide_parent_git_repo(monkeypatch)
    calls: list[dict] = []

    async def _fake(self, prompt, cwd, **kwargs):
        calls.append(kwargs)
        return ExecutionResult(success=True, output="评审完成", duration_seconds=2.0)

    monkeypatch.setattr(AgentExecutor, "run_claude", _fake)

    res = await mcp_to_claude.claude_handoff(_req(), project_dir=str(tmp_path))
    assert isinstance(res, HandoffResult)
    assert res.agent_used == "claude"
    assert res.evidence_level == "unknown"
    assert calls[0]["permission_override"] == "plan"   # 无写申请 => 只读


async def test_claude_handoff_grants_writes_under_default_policy(monkeypatch, tmp_path):
    """默认策略下,申请项目内写入 => 授予写权限模式(默认 bypassPermissions)。"""
    _hide_parent_git_repo(monkeypatch)
    calls: list[dict] = []

    async def _fake(self, prompt, cwd, **kwargs):
        calls.append(kwargs)
        return ExecutionResult(success=True, output="改完了", duration_seconds=2.0)

    monkeypatch.setattr(AgentExecutor, "run_claude", _fake)

    res = await mcp_to_claude.claude_handoff(
        _req(writable=["src/x"]), project_dir=str(tmp_path)
    )
    assert res.agent_used == "claude"
    assert calls[0]["permission_override"] == "bypassPermissions"
    assert calls[0]["extra_env"]["CC_BRIDGE_CHAIN_DEPTH"] == "1"


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
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    async def _fake(self, prompt, cwd, **kwargs):
        path = repo / "src" / "x.py"
        path.parent.mkdir(parents=True)
        path.write_text("x = 1\n", encoding="utf-8")
        return ExecutionResult(
            success=True,
            output="ok",
            files_changed=["reported-only.py"],
            duration_seconds=1.0,
        )

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake)

    res = await mcp_to_codex.codex_handoff(_req(), project_dir=str(repo))
    assert res.status == "scope_violation"
    assert res.scope_violations == ["src/x.py"]
    assert "reported-only.py" not in res.scope_violations
    assert res.failure_kind is FailureKind.scope_violation
    assert res.verified_files_changed == []   # 仍不声称 verified
    assert res.side_effects.worktree_files == "detected_but_not_reverted"


# ---------------------------------------------------------------------------
# legacy 工具(codex_execute / claude_analyze)走同一 policy 地板
# ---------------------------------------------------------------------------

async def test_codex_execute_disabled_by_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_BRIDGE_LEGACY_TOOLS", "0")

    async def _boom(self, *a, **k):
        raise AssertionError("legacy 关闭时不应执行 Codex")

    monkeypatch.setattr(AgentExecutor, "run_codex", _boom)
    out = await mcp_to_codex.codex_execute("做点事", project_dir=str(tmp_path))
    assert isinstance(out, str)
    assert "禁用" in out


async def test_codex_execute_readonly_policy_clamps(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_BRIDGE_POLICY_READONLY", "1")
    calls: list[dict] = []

    async def _fake(self, prompt, cwd, **kwargs):
        calls.append(kwargs)
        return ExecutionResult(success=True, output="ok", duration_seconds=1.0)

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake)
    await mcp_to_codex.codex_execute("做点事", project_dir=str(tmp_path))
    assert calls[0]["sandbox_override"] == "read-only"
    assert calls[0]["extra_env"]["CC_BRIDGE_CHAIN_DEPTH"] == "1"


async def test_codex_execute_depth_exceeded_refuses(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_BRIDGE_POLICY_MAX_DEPTH", "1")
    monkeypatch.setenv("CC_BRIDGE_CHAIN_DEPTH", "1")

    async def _boom(self, *a, **k):
        raise AssertionError("链深超限时不应执行")

    monkeypatch.setattr(AgentExecutor, "run_codex", _boom)
    out = await mcp_to_codex.codex_execute("做点事", project_dir=str(tmp_path))
    assert "链路深度" in out


async def test_claude_analyze_disabled_by_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_BRIDGE_LEGACY_TOOLS", "0")

    async def _boom(self, *a, **k):
        raise AssertionError("legacy 关闭时不应执行 Claude")

    monkeypatch.setattr(AgentExecutor, "run_claude", _boom)
    out = await mcp_to_claude.claude_analyze("评审", project_dir=str(tmp_path))
    assert "禁用" in out
