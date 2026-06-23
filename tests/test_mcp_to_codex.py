"""mcp_to_codex 单元测试（离线）.

monkeypatch：
- AgentExecutor.run_codex 替换成 async stub，返回预制的 ExecutionResult；
- ContextBuilder.build_project_context 返回轻量 ProjectContext，避免真实 git/fs。

验证 codex_execute 的成功路径、异常路径都返回字符串（绝不抛异常），
codex_status 返回非空字符串。
"""

from __future__ import annotations

import asyncio

import pytest

from cc_bridge.bridge import config, gitsafe, handoff_store, mcp_to_codex
from cc_bridge.bridge.context import ContextBuilder, ProjectContext
from cc_bridge.bridge.contracts import HandoffRequest, HandoffResult, RequestedScope
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


def _patch_run_codex(monkeypatch, result=None, exc=None):
    async def _fake_run_codex(
        self, prompt, cwd, timeout=None, resume_session_id=None, on_progress=None,
        **kwargs,
    ):
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake_run_codex)


def _handoff_request() -> HandoffRequest:
    return HandoffRequest(
        contract_version="1",
        goal="async handoff task",
        requested_scope=RequestedScope(),
    )


@pytest.fixture(autouse=True)
def clear_codex_session_cache():
    mcp_to_codex._CODEX_SESSIONS_BY_PROJECT.clear()
    mcp_to_codex._CODEX_SESSION_LOCKS_BY_PROJECT.clear()
    yield
    mcp_to_codex._CODEX_SESSIONS_BY_PROJECT.clear()
    mcp_to_codex._CODEX_SESSION_LOCKS_BY_PROJECT.clear()


async def test_codex_execute_success(monkeypatch, tmp_path):
    _patch_run_codex(
        monkeypatch,
        result=ExecutionResult(
            success=True,
            output="实现完成，所有测试通过。",
            files_changed=["src/foo.py", "tests/test_foo.py"],
            duration_seconds=3.2,
        ),
    )

    out = await mcp_to_codex.codex_execute("写个函数", project_dir=str(tmp_path))

    assert isinstance(out, str)
    # 成功摘要含「已完成」
    assert "已完成" in out
    # 改动文件信息被带出来
    assert "改动文件" in out
    assert "src/foo.py" in out


async def test_codex_execute_default_does_not_add_dry_run_controls(
    monkeypatch, tmp_path
):
    calls: list[dict] = []

    async def _fake_run_codex(self, prompt, cwd, **kwargs):
        calls.append({"prompt": prompt, "kwargs": kwargs})
        return ExecutionResult(success=True, output="ok", duration_seconds=1.0)

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake_run_codex)

    out = await mcp_to_codex.codex_execute("normal task", project_dir=str(tmp_path))

    assert "dry-run" not in out
    assert "预演" not in out
    # PR5:legacy 工具走 policy 地板;默认(未收紧)仍是 workspace-write,但显式流过。
    assert calls[0]["kwargs"]["sandbox_override"] == "workspace-write"
    assert "dry run" not in calls[0]["prompt"].lower()
    assert "不要真正修改文件" not in calls[0]["prompt"]


async def test_codex_execute_dry_run_uses_read_only_and_marks_prompt_and_summary(
    monkeypatch, tmp_path
):
    calls: list[dict] = []

    async def _fake_run_codex(self, prompt, cwd, **kwargs):
        calls.append({"prompt": prompt, "kwargs": kwargs})
        return ExecutionResult(
            success=True,
            output="would edit src/foo.py and tests/test_foo.py",
            duration_seconds=1.0,
        )

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake_run_codex)

    out = await mcp_to_codex.codex_execute(
        "plan an edit", project_dir=str(tmp_path), dry_run=True
    )

    assert calls[0]["kwargs"]["sandbox_override"] == "read-only"
    assert "dry run" in calls[0]["prompt"].lower()
    assert "不要真正修改文件" in calls[0]["prompt"]
    assert "dry-run" in out
    assert "预演" in out


async def test_codex_execute_safe_mode_prepare_failure_does_not_run_codex(
    monkeypatch, tmp_path
):
    async def _boom(self, prompt, cwd, **kwargs):
        raise AssertionError("safe 前置失败时不应调用 Codex")

    monkeypatch.setattr(
        mcp_to_codex.gitsafe,
        "prepare_safe_branch",
        lambda cwd: gitsafe.SafePrep(ok=False, message="需要干净工作区"),
    )
    monkeypatch.setattr(AgentExecutor, "run_codex", _boom)

    out = await mcp_to_codex.codex_execute(
        "safe task", project_dir=str(tmp_path), git_mode="safe"
    )

    assert "无法以 safe 模式" in out


async def test_codex_execute_summary_includes_report_header(monkeypatch, tmp_path):
    async def _fake_run_codex(self, prompt, cwd, **kwargs):
        return ExecutionResult(success=True, output="ok", duration_seconds=1.0)

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake_run_codex)

    out = await mcp_to_codex.codex_execute("report task", project_dir=str(tmp_path))

    assert "【cc-bridge 报告】Claude → Codex" in out


async def test_codex_execute_returns_and_reuses_session_id(monkeypatch, tmp_path):
    """continue_session=True 时续接同一 project_dir 上一次记录的 Codex session。"""
    calls: list[str | None] = []

    async def _fake_run_codex(
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

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake_run_codex)

    first = await mcp_to_codex.codex_execute("第一次", project_dir=str(tmp_path))
    second = await mcp_to_codex.codex_execute(
        "第二次", project_dir=str(tmp_path), continue_session=True
    )

    assert calls == [None, "sid-first"]
    assert "sid-first" in first
    assert "sid-first" in second


async def test_codex_execute_continue_session_serializes_per_project(monkeypatch, tmp_path):
    """同一 cwd 的并发续接必须串行，第二个调用看到第一个写回的 session id。"""
    cwd = str(tmp_path.resolve())
    mcp_to_codex._CODEX_SESSIONS_BY_PROJECT[cwd] = "sid-old"
    calls: list[str | None] = []
    active = 0
    max_active = 0

    async def _fake_run_codex(
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

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake_run_codex)

    await asyncio.gather(
        mcp_to_codex.codex_execute("继续 1", project_dir=str(tmp_path), continue_session=True),
        mcp_to_codex.codex_execute("继续 2", project_dir=str(tmp_path), continue_session=True),
    )

    assert max_active == 1
    assert calls == ["sid-old", "sid-first"]
    assert mcp_to_codex._CODEX_SESSIONS_BY_PROJECT[cwd] == "sid-second"


async def test_codex_execute_continue_false_starts_new_session(monkeypatch, tmp_path):
    """即使已有缓存，continue_session=False 也必须保持开新会话。"""
    mcp_to_codex._CODEX_SESSIONS_BY_PROJECT[str(tmp_path)] = "sid-old"
    calls: list[str | None] = []

    async def _fake_run_codex(
        self, prompt, cwd, timeout=None, resume_session_id=None, on_progress=None,
        **kwargs,
    ):
        calls.append(resume_session_id)
        return ExecutionResult(success=True, output="ok", session_id="sid-new")

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake_run_codex)

    out = await mcp_to_codex.codex_execute(
        "不续接", project_dir=str(tmp_path), continue_session=False
    )

    assert calls == [None]
    assert mcp_to_codex._CODEX_SESSIONS_BY_PROJECT[str(tmp_path)] == "sid-new"
    assert "sid-new" in out


async def test_codex_execute_progress_ctx_errors_do_not_break(monkeypatch, tmp_path):
    """MCP progress/info 抛异常时，工具仍然返回正常字符串。"""

    class BadCtx:
        async def report_progress(self, *args, **kwargs):
            raise RuntimeError("progress broke")

        async def info(self, *args, **kwargs):
            raise RuntimeError("info broke")

    async def _fake_run_codex(
        self, prompt, cwd, timeout=None, resume_session_id=None, on_progress=None,
        **kwargs,
    ):
        assert on_progress is not None
        await on_progress("正在执行 pytest")
        return ExecutionResult(success=True, output="ok")

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake_run_codex)

    out = await mcp_to_codex.codex_execute("x", project_dir=str(tmp_path), ctx=BadCtx())

    assert isinstance(out, str)
    assert "ok" in out


async def test_codex_execute_failure_result_is_string(monkeypatch, tmp_path):
    """executor 返回失败结果（非异常）→ 仍是友好字符串，含「未能完成」。"""
    _patch_run_codex(
        monkeypatch,
        result=ExecutionResult(
            success=False,
            output="",
            error="not logged in",
            exit_code=1,
            raw_stderr="unauthorized",
        ),
    )

    out = await mcp_to_codex.codex_execute("做点事", project_dir=str(tmp_path))
    assert isinstance(out, str)
    assert "未能完成" in out


async def test_codex_execute_swallows_exception(monkeypatch, tmp_path):
    """executor 抛异常 → 不向 MCP 抛，返回字符串。"""
    _patch_run_codex(monkeypatch, exc=RuntimeError("boom"))

    out = await mcp_to_codex.codex_execute("触发异常", project_dir=str(tmp_path))
    assert isinstance(out, str)
    assert out  # 非空
    assert "boom" in out


async def test_codex_execute_rejects_missing_project_dir(monkeypatch):
    """不传 project_dir → 直接拒绝，绝不擅自用进程 cwd 去改文件。"""
    async def _boom(self, *a, **k):
        raise AssertionError("project_dir 缺失时不应真正调用 Codex")

    monkeypatch.setattr(AgentExecutor, "run_codex", _boom)
    out = await mcp_to_codex.codex_execute("无目录")
    assert isinstance(out, str)
    assert "project_dir" in out


async def test_codex_execute_rejects_relative_project_dir(monkeypatch):
    async def _boom(self, *a, **k):
        raise AssertionError("相对路径时不应真正调用 Codex")

    monkeypatch.setattr(AgentExecutor, "run_codex", _boom)
    out = await mcp_to_codex.codex_execute("x", project_dir="relative/dir")
    assert "绝对路径" in out


async def test_codex_execute_rejects_nonexistent_dir(monkeypatch, tmp_path):
    async def _boom(self, *a, **k):
        raise AssertionError("目录不存在时不应真正调用 Codex")

    monkeypatch.setattr(AgentExecutor, "run_codex", _boom)
    out = await mcp_to_codex.codex_execute("x", project_dir=str(tmp_path / "nope"))
    assert "不存在" in out


async def test_codex_handoff_async_initializes_and_spawns_runner(monkeypatch, tmp_path):
    calls: dict[str, object] = {}
    status_writes: list[tuple[str, str, str]] = []

    def fake_init(request, cwd, *, agent, caller):
        calls["init"] = {
            "request": request,
            "cwd": cwd,
            "agent": agent,
            "caller": caller,
        }
        return "hid-fixed"

    def fake_spawn(handoff_id, *, cwd, env=None):
        calls["spawn"] = {"handoff_id": handoff_id, "cwd": cwd, "env": env}
        return 12345

    monkeypatch.setattr(handoff_store, "init_handoff", fake_init)
    monkeypatch.setattr(config, "spawn_detached_runner", fake_spawn)
    monkeypatch.setattr(
        mcp_to_codex,
        "authorize",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("async handoff 不应重复 authorize")
        ),
    )
    monkeypatch.setattr(
        handoff_store,
        "write_pid",
        lambda handoff_id, pid: calls.setdefault("pid", (handoff_id, pid)),
    )
    monkeypatch.setattr(
        handoff_store,
        "write_status",
        lambda handoff_id, state, note="": status_writes.append(
            (handoff_id, state, note)
        ),
    )

    out = await mcp_to_codex.codex_handoff_async(
        _handoff_request(), project_dir=str(tmp_path)
    )

    assert out == {"handoff_id": "hid-fixed", "state": "running"}
    assert calls["init"]["cwd"] == str(tmp_path.resolve())
    assert calls["init"]["agent"] == "codex"
    assert calls["init"]["caller"] == "claude"
    assert calls["spawn"] == {
        "handoff_id": "hid-fixed",
        "cwd": str(tmp_path.resolve()),
        "env": None,
    }
    assert calls["pid"] == ("hid-fixed", 12345)
    assert status_writes == [
        ("hid-fixed", "running", "runner 已启动(pid=12345)")
    ]


async def test_codex_handoff_status_and_result_read_store(monkeypatch):
    monkeypatch.setattr(
        handoff_store,
        "read_status",
        lambda handoff_id: {"state": "running", "note": "busy"}
        if handoff_id == "hid-running"
        else None,
    )

    assert await mcp_to_codex.codex_handoff_status("hid-running") == {
        "state": "running",
        "note": "busy",
    }
    assert await mcp_to_codex.codex_handoff_status("missing") == {
        "state": "unknown",
        "note": "无此 handoff_id",
    }

    result = HandoffResult(
        contract_version="1",
        handoff_id="hid-done",
        status="completed",
        agent_used="codex",
        summary="done",
    )
    monkeypatch.setattr(
        handoff_store,
        "read_result",
        lambda handoff_id: result if handoff_id == "hid-done" else None,
    )
    monkeypatch.setattr(
        handoff_store,
        "read_status",
        lambda handoff_id: {"state": "running", "note": "busy"}
        if handoff_id == "hid-pending"
        else None,
    )

    assert await mcp_to_codex.codex_handoff_result("hid-done") == result.model_dump(
        mode="json"
    )
    assert await mcp_to_codex.codex_handoff_result("hid-pending") == {
        "state": "running",
        "note": "结果尚未就绪或不存在",
    }


async def test_codex_status_ready(monkeypatch):
    monkeypatch.setattr(
        mcp_to_codex, "check_codex",
        lambda: AgentReadiness("Codex", "C:/fake/codex", "codex 1.0.0", True, True),
    )
    out = await mcp_to_codex.codex_status()
    assert isinstance(out, str) and out.strip()
    assert "codex 1.0.0" in out
    # 区分就绪 / 未就绪：就绪态绝不能包含「尚未就绪 / 未就绪」
    assert "未就绪" not in out


async def test_codex_status_not_ready(monkeypatch):
    monkeypatch.setattr(
        mcp_to_codex, "check_codex",
        lambda: AgentReadiness("Codex", "C:/fake/codex", "codex 1.0.0", False, True),
    )
    out = await mcp_to_codex.codex_status()
    assert "未就绪" in out  # 未登录 → ready=False → 「尚未就绪」


async def test_codex_status_swallows_exception(monkeypatch):
    def _boom():
        raise RuntimeError("status failed")

    monkeypatch.setattr(mcp_to_codex, "check_codex", _boom)

    out = await mcp_to_codex.codex_status()
    assert isinstance(out, str)
    assert out.strip()


def test_mcp_server_metadata():
    """模块级 mcp 是名为 bridge-to-codex 的 FastMCP；main 可调用。"""
    assert mcp_to_codex.mcp.name == "bridge-to-codex"
    assert callable(mcp_to_codex.main)


def test_codex_session_caches_are_capped(monkeypatch):
    monkeypatch.setattr(mcp_to_codex, "_CODEX_SESSION_CACHE_MAX_PROJECTS", 3)

    lock = mcp_to_codex._codex_session_lock("project-0")
    for index in range(5):
        cwd = f"project-{index}"
        mcp_to_codex._codex_session_lock(cwd)
        mcp_to_codex._remember_codex_session(cwd, f"sid-{index}")

    assert len(mcp_to_codex._CODEX_SESSIONS_BY_PROJECT) == 3
    assert len(mcp_to_codex._CODEX_SESSION_LOCKS_BY_PROJECT) == 3
    assert mcp_to_codex._CODEX_SESSIONS_BY_PROJECT == {
        "project-2": "sid-2",
        "project-3": "sid-3",
        "project-4": "sid-4",
    }
    assert "project-0" not in mcp_to_codex._CODEX_SESSION_LOCKS_BY_PROJECT
    assert not lock.locked()


async def test_codex_execute_ctx_is_injected_not_input_schema():
    from cc_bridge.bridge.mcp_to_codex import mcp

    assert mcp._tool_manager._tools["codex_execute"].context_kwarg == "ctx"
    tools = await mcp.list_tools()
    codex_tool = next(tool for tool in tools if tool.name == "codex_execute")
    assert "ctx" not in codex_tool.inputSchema.get("properties", {})
    assert "dry_run" in codex_tool.inputSchema.get("properties", {})


async def test_codex_handoff_exposes_input_and_output_schema():
    """PR6:结构化委派的输入 / 输出 schema 都对 MCP Inspector 可见(版本化)。"""
    from cc_bridge.bridge.mcp_to_codex import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    handoff = tools["codex_handoff"]
    assert "request" in handoff.inputSchema.get("properties", {})
    assert "HandoffRequest" in (handoff.inputSchema.get("$defs") or {})
    assert "ctx" not in handoff.inputSchema.get("properties", {})
    out = getattr(handoff, "outputSchema", None)
    assert out is not None
    assert "status" in (out.get("properties") or {})


@pytest.fixture(autouse=True)
def clear_audit_log_env(monkeypatch):
    monkeypatch.delenv("CC_BRIDGE_AUDIT_LOG", raising=False)


async def test_codex_execute_writes_audit_log_when_enabled(monkeypatch, tmp_path):
    import json

    log_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("CC_BRIDGE_AUDIT_LOG", str(log_path))
    _patch_run_codex(
        monkeypatch,
        result=ExecutionResult(
            success=True,
            output="ok",
            files_changed=["src/foo.py"],
            duration_seconds=1.0,
        ),
    )

    await mcp_to_codex.codex_execute("audit me", project_dir=str(tmp_path))

    record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["direction"] == "codex"
    assert record["cwd"] == str(tmp_path.resolve())
    assert record["task_summary"] == "audit me"
    assert record["success"] is True
    assert record["files_changed"] == ["src/foo.py"]
