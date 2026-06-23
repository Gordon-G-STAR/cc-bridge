"""MCP server：让 Claude 调用 Codex（装进 Claude Desktop）.

这个 server 把 Codex CLI 暴露成 Claude Desktop 里的两个工具：

- ``codex_execute``：把任务连同当前项目上下文转交给 Codex，让它在指定项目目录里
  实际干活（改文件、跑测试、重构、生成代码），再把结果摘要返回给 Claude；
- ``codex_status``：报告 Codex 是否就绪（命令行可用 / 已登录 / 版本）。

调用链一律复用「地基」组件，绝不重复实现 CLI 调用逻辑：
ContextBuilder -> AgentExecutor.run_codex -> ResultParser。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

from . import config, evidence, gitsafe, handoff_store, policy
from .audit import append_audit_record
from .context import ContextBuilder, require_project_dir
from .contracts import FailureKind, HandoffRequest, HandoffResult, fail_closed_result
from .executor import AgentExecutor
from .handoff import authorize, execution_to_handoff, handoff_goal_text, maybe_failover
from .locks import LockBusy, async_project_lock
from .parser import ResultParser
from .progress import make_progress_callback as _make_progress_callback
from .status import check_codex

mcp = FastMCP("bridge-to-codex")

_DRY_RUN_PROMPT_SUFFIX = (
    "\n\n这是预演（dry run）：只描述将要做的改动、会改哪些文件，"
    "不要真正修改文件或落盘。"
)
_DRY_RUN_SUMMARY_PREFIX = "【dry-run 预演】\n"
_DRY_RUN_CODEX_SANDBOX = "read-only"

_CODEX_SESSIONS_BY_PROJECT: dict[str, str] = {}
_CODEX_SESSION_LOCKS_BY_PROJECT: dict[str, asyncio.Lock] = {}
_CODEX_SESSION_CACHE_MAX_PROJECTS = 256


def _trim_codex_session_caches(protected_cwd: str | None = None) -> None:
    max_projects = _CODEX_SESSION_CACHE_MAX_PROJECTS
    if max_projects < 1:
        max_projects = 1

    def _project_count() -> int:
        return len(
            set(_CODEX_SESSIONS_BY_PROJECT) | set(_CODEX_SESSION_LOCKS_BY_PROJECT)
        )

    while _project_count() > max_projects:
        candidates = [
            *list(_CODEX_SESSION_LOCKS_BY_PROJECT),
            *[
                cwd
                for cwd in _CODEX_SESSIONS_BY_PROJECT
                if cwd not in _CODEX_SESSION_LOCKS_BY_PROJECT
            ],
        ]
        evicted = False
        for cwd in candidates:
            if cwd == protected_cwd:
                continue
            lock = _CODEX_SESSION_LOCKS_BY_PROJECT.get(cwd)
            if lock is not None and lock.locked():
                continue
            _CODEX_SESSION_LOCKS_BY_PROJECT.pop(cwd, None)
            _CODEX_SESSIONS_BY_PROJECT.pop(cwd, None)
            evicted = True
            break
        if not evicted:
            break


def _codex_session_lock(cwd: str) -> asyncio.Lock:
    lock = _CODEX_SESSION_LOCKS_BY_PROJECT.get(cwd)
    if lock is None:
        lock = asyncio.Lock()
        _CODEX_SESSION_LOCKS_BY_PROJECT[cwd] = lock
        _trim_codex_session_caches(protected_cwd=cwd)
    return lock


def _remember_codex_session(cwd: str, session_id: str) -> None:
    _CODEX_SESSIONS_BY_PROJECT[cwd] = session_id
    _trim_codex_session_caches(protected_cwd=cwd)


def _mark_dry_run_summary(summary: str) -> str:
    return f"{_DRY_RUN_SUMMARY_PREFIX}{summary}"


@mcp.tool(
    name="codex_execute",
    description=(
        "把一个开发任务转交给 Codex（OpenAI 的编码 agent）去实际执行。"
        "Codex 会在你指定的项目目录里【真正修改文件】——它擅长跑测试、重构、"
        "实现/生成代码、批量改动，并能直接在工作区落盘。它走用户自己的 ChatGPT "
        "订阅额度（不消耗 Claude 的额度）。\n\n"
        "调用约定：\n"
        "- task：用清晰的自然语言描述要让 Codex 做的事（越具体越好，"
        "  例如要改哪些行为、要满足哪些测试、约束条件等）。\n"
        "- project_dir：【必填】当前项目的【绝对路径】（例如 "
        "  'C:/Users/me/proj' 或 '/home/me/proj'）。Codex 会以此为工作目录并在"
        "  其中改文件。【不传、传相对路径、或目录不存在都会被直接拒绝】——"
        "  cc-bridge 不会猜测目录，以免在错误的地方改文件。\n"
        "- continue_session：传 true 续接该项目上一次的 Codex 会话（多轮接力）。\n"
        "- dry_run：传 true 进入预演——Codex 只分析、说清会改什么，不真正落盘（read-only）。\n"
        "- git_mode：传 \"safe\" 时把改动隔离到临时分支，跑完提交到该分支并切回原分支，"
        "使原分支不受影响（要求干净的 git 工作区）。默认 \"off\"。\n\n"
        "适用场景：需要落地的代码改动、写/修测试、重构、按规格生成新代码。"
        "返回值是 Codex 执行结果的自然语言摘要（含成功与否、改动的文件列表、说明）。"
    ),
)
async def codex_execute(
    task: str,
    project_dir: str | None = None,
    continue_session: bool = False,
    dry_run: bool = False,
    git_mode: str = "off",
    ctx: Context = None,
) -> str:
    """让 Codex 在 ``project_dir`` 里执行 ``task``，返回结果摘要字符串。

    整个函数体包在 try/except 里：无论发生什么都返回友好的字符串，
    绝不把异常抛给 MCP 框架。
    """
    try:
        cwd = str(Path(require_project_dir(project_dir)).resolve())
    except (OSError, ValueError) as exc:
        return f"无法调用 Codex：{exc}"
    try:
        # 同一条 policy 地板:legacy 工具与 handoff 共用本地策略(关闭开关 / 链深上限 / 引擎钳制),
        # 绝无绕过 policy 的满权后门(F5 / #12)。dry_run 仍优先强制只读。
        cfg = config.BridgeConfig.from_env()
        legacy = policy.decide_legacy(
            agent="codex",
            policy=policy.LocalPolicy.from_env(),
            chain=policy.ChainContext.from_env(),
            codex_cap=cfg.codex_sandbox,
            claude_cap=cfg.claude_permission_mode,
        )
        if legacy.refusal:
            return legacy.refusal
        on_progress = _make_progress_callback(ctx)
        # 收集上下文（含 git 探测 + 目录遍历）放到线程里跑，绝不阻塞 MCP 事件循环，
        # 也保证工具可被宿主取消；底层 git 调用已硬化超时（见 config.git_capture）。
        config.debug_log(f"codex_execute: 开始收集上下文 {cwd}")
        project_ctx = await asyncio.to_thread(ContextBuilder().build_project_context, cwd)
        config.debug_log("codex_execute: 上下文就绪，开始调用 Codex")
        prompt = ContextBuilder().build_task_prompt(task, project_ctx, caller="claude")
        if dry_run:
            prompt += _DRY_RUN_PROMPT_SUFFIX
        safe_suffix = ""
        async with _codex_session_lock(cwd):
            prep = None
            if git_mode == "safe" and not dry_run:
                prep = await asyncio.to_thread(gitsafe.prepare_safe_branch, cwd)
                if not prep.ok:
                    return f"无法以 safe 模式调用 Codex：{prep.message}"
            resume_session_id = _CODEX_SESSIONS_BY_PROJECT.get(cwd) if continue_session else None
            run_kwargs = {
                "resume_session_id": resume_session_id,
                "on_progress": on_progress,
                "sandbox_override": (
                    _DRY_RUN_CODEX_SANDBOX if dry_run else legacy.engine_mode
                ),
                "extra_env": legacy.child_env,
            }
            result = await AgentExecutor(cfg).run_codex(prompt, cwd, **run_kwargs)
            if result.session_id:
                _remember_codex_session(cwd, result.session_id)
            if prep is not None:
                finish = await asyncio.to_thread(
                    gitsafe.finish_safe_branch,
                    cwd,
                    prep.original_branch,
                    prep.temp_branch,
                    task,
                )
                safe_suffix = "\n\n" + finish.note
                if finish.diff_summary:
                    safe_suffix += "\n\n" + finish.diff_summary
        append_audit_record(
            direction="codex",
            cwd=cwd,
            task=task,
            success=result.success,
            files_changed=result.files_changed,
        )
        config.debug_log(f"codex_execute: Codex 返回 success={result.success}")
        parsed = ResultParser().parse(result, "codex")
        summary = ResultParser().summarize_for_caller(
            parsed, "codex", caller="claude", project_dir=cwd, task=task
        )
        summary = summary + safe_suffix
        if dry_run:
            return _mark_dry_run_summary(summary)
        return summary
    except Exception as exc:  # noqa: BLE001 — 兜底，绝不向 MCP 抛异常
        return (
            "调用 Codex 时出现内部错误，未能完成任务。\n"
            f"错误信息：{exc}\n"
            "请确认 Codex 命令行已安装并已登录（codex login），再重试。"
        )


@mcp.tool(
    name="codex_handoff",
    description=(
        "【v0.2 结构化委派】把一份结构化委派合同(HandoffRequest:目标 / 验收标准 / "
        "申请的权限范围 requested_scope / 检查项 check_ids)交给 Codex,返回结构化结果 "
        "HandoffResult。与 codex_execute 的区别:输入输出都是版本化结构,且 requested_scope "
        "只是【申请】——由本地策略重授权(effective = requested ∩ 父链 ∩ 本地策略 ∩ 引擎上限);"
        "申请超出策略会被收窄 / 降级为只读 / 拒绝;链深超限或需审批(headless)一律 fail-closed。"
        "project_dir 必填、须为存在的绝对路径。"
    ),
)
async def codex_handoff(
    request: HandoffRequest,
    project_dir: str | None = None,
    continue_session: bool = False,
    ctx: Context = None,
) -> HandoffResult:
    """把结构化合同交给 Codex(经本地策略重授权),返回 HandoffResult,绝不向 MCP 抛异常。"""
    handoff_id = uuid.uuid4().hex[:12]
    try:
        cwd = str(Path(require_project_dir(project_dir)).resolve())
    except (OSError, ValueError) as exc:
        return fail_closed_result(
            handoff_id,
            failure_kind=FailureKind.invalid_contract,
            reason=f"project_dir 无效:{exc}",
            status="failed",
        )
    try:
        cfg = config.BridgeConfig.from_env()
        # 本地策略重授权:deny / approval_required / 链深超限 => fail-closed,绝不执行。
        plan = authorize(handoff_id, request, cwd, agent="codex", cfg=cfg)
        if isinstance(plan, HandoffResult):
            return plan

        before = evidence.baseline(cwd)
        on_progress = _make_progress_callback(ctx)
        project_ctx = await asyncio.to_thread(
            ContextBuilder().build_project_context, cwd
        )
        prompt = ContextBuilder().build_task_prompt(
            handoff_goal_text(request), project_ctx, caller="claude"
        )
        async with async_project_lock(cwd, timeout=5.0), _codex_session_lock(cwd):
            resume_session_id = (
                _CODEX_SESSIONS_BY_PROJECT.get(cwd) if continue_session else None
            )
            result = await AgentExecutor(cfg).run_codex(
                prompt,
                cwd,
                timeout=request.timeout_seconds,
                resume_session_id=resume_session_id,
                on_progress=on_progress,
                sandbox_override=plan.engine_mode,
                extra_env=plan.child_env,
            )
            if result.session_id:
                _remember_codex_session(cwd, result.session_id)
        ev = evidence.gather(cwd, before, writable_paths=plan.effective_writable)
        parsed = ResultParser().parse(result, "codex")
        summary = ResultParser().summarize_for_caller(parsed, "codex")
        primary_result = execution_to_handoff(
            handoff_id, request, result, summary, "codex", evidence=ev, plan=plan
        )
        # PR7:主 agent 若【可证明零副作用】且合同允许,透明 failover 到 Claude。
        final = await maybe_failover(
            primary_result,
            primary_agent="codex",
            request=request,
            cwd=cwd,
            cfg=cfg,
            caller="claude",
            on_progress=on_progress,
        )
        append_audit_record(
            direction="codex",
            cwd=cwd,
            task=handoff_goal_text(request),
            success=final.status == "completed",
            files_changed=final.verified_files_changed,
            extra={
                "mode": "handoff",
                "depth": plan.depth,
                "write_granted": plan.write_granted,
                "effective_writable": list(plan.effective_writable),
                "agent_used": final.agent_used,
                "failover": final.agent_used not in (None, "codex"),
            },
        )
        return final
    except LockBusy:
        return fail_closed_result(
            handoff_id,
            failure_kind=FailureKind.project_busy,
            reason="Another bridge process is handling this project; try again shortly.",
            status="failed",
        )
    except Exception as exc:  # noqa: BLE001 — 兜底,绝不向 MCP 抛异常
        return fail_closed_result(
            handoff_id,
            failure_kind=FailureKind.crashed,
            reason=f"调用 Codex 时内部错误:{exc}",
            status="failed",
        )


@mcp.tool(
    name="codex_handoff_async",
    description=(
        "异步结构化委派给 Codex:立即返回 handoff_id,执行在独立后台进程进行。"
        "用 codex_handoff_status / codex_handoff_result 查询状态和结果;不阻塞、不撞 MCP -32001;"
        "暂不支持 continue_session。授权不在此工具重复执行,由 runner 权威执行。"
    ),
)
async def codex_handoff_async(
    request: HandoffRequest,
    project_dir: str | None = None,
    ctx: Context = None,
) -> dict:
    """初始化 handoff spec 并 detached spawn runner,不重复授权。"""
    try:
        cwd = str(Path(require_project_dir(project_dir)).resolve())
    except (OSError, ValueError) as exc:
        return {"state": "failed", "note": f"project_dir 无效:{exc}"}

    try:
        max_allowed = config.max_async_handoffs()
        if handoff_store.count_active() >= max_allowed:
            return {
                "state": "failed",
                "note": (
                    "并发上限已满"
                    f"(CC_BRIDGE_MAX_ASYNC_HANDOFFS={max_allowed}),"
                    "请稍后再试或等已有任务完成"
                ),
            }
    except Exception as exc:  # noqa: BLE001 - MCP 边界兜底，不能抛给宿主
        return {"state": "failed", "note": f"并发检测失败:{exc}"}

    try:
        handoff_id = handoff_store.init_handoff(
            request, cwd, agent="codex", caller="claude"
        )
    except Exception as exc:  # noqa: BLE001 - MCP 工具边界不抛异常
        return {"state": "failed", "note": f"初始化失败:{exc}"}

    try:
        pid = config.spawn_detached_runner(handoff_id, cwd=cwd)
        handoff_store.write_pid(handoff_id, pid)
        handoff_store.write_status(
            handoff_id, "running", f"runner 已启动(pid={pid})"
        )
        return {"handoff_id": handoff_id, "state": "running"}
    except Exception as exc:  # noqa: BLE001 - spawn 失败也落状态并返回 dict
        try:
            handoff_store.write_status(handoff_id, "failed", f"spawn 失败:{exc}")
        except Exception:
            pass
        return {"handoff_id": handoff_id, "state": "failed", "note": str(exc)}


@mcp.tool(
    name="codex_handoff_status",
    description="查询异步 Codex handoff 的当前状态;永不抛异常。",
)
async def codex_handoff_status(handoff_id: str) -> dict:
    try:
        status = handoff_store.read_status(handoff_id)
        if status is not None and status.get("state") == "running":
            if not handoff_store.runner_alive(handoff_id):
                handoff_store.write_status(
                    handoff_id, "interrupted", "runner 进程已不存在"
                )
                status = handoff_store.read_status(handoff_id) or {
                    "state": "interrupted",
                    "note": "runner 进程已不存在",
                }
        return status or {"state": "unknown", "note": "无此 handoff_id"}
    except Exception as exc:  # noqa: BLE001
        return {"state": "failed", "note": str(exc)}


@mcp.tool(
    name="codex_handoff_cancel",
    description="取消异步 Codex handoff；杀掉 runner 进程树并标记 interrupted，永不抛异常。",
)
async def codex_handoff_cancel(handoff_id: str) -> dict:
    try:
        pid = handoff_store.read_pid(handoff_id)
        if pid is None:
            status = handoff_store.read_status(handoff_id) or {}
            return {
                "state": status.get("state", "unknown"),
                "note": "无运行中的 runner",
            }
        handoff_store.kill_runner(pid)
        handoff_store.write_status(handoff_id, "interrupted", "已取消")
        return {"handoff_id": handoff_id, "state": "interrupted"}
    except Exception as exc:  # noqa: BLE001
        return {"state": "failed", "note": str(exc)}


@mcp.tool(
    name="codex_handoff_result",
    description="读取异步 Codex handoff 的最终结构化结果;尚未完成时返回当前状态。",
)
async def codex_handoff_result(handoff_id: str) -> dict:
    try:
        result = handoff_store.read_result(handoff_id)
        if result is not None:
            return result.model_dump(mode="json")
        status = handoff_store.read_status(handoff_id)
        return {
            "state": (status or {}).get("state", "unknown"),
            "note": "结果尚未就绪或不存在",
        }
    except Exception as exc:  # noqa: BLE001
        return {"state": "failed", "note": str(exc)}


@mcp.tool(
    name="codex_status",
    description=(
        "查询 Codex 当前是否就绪：命令行是否可用、是否已登录、版本号。"
        "在调用 codex_execute 之前如果不确定 Codex 能否工作，可以先调用它。"
    ),
)
async def codex_status() -> str:
    """返回 Codex 的运行期就绪状态，不抛异常。"""
    try:
        return check_codex().status_line()
    except Exception as exc:  # noqa: BLE001
        return f"无法检测 Codex 状态：{exc}"


def main() -> None:
    """以 stdio 方式启动 MCP server。"""
    mcp.run()


if __name__ == "__main__":
    main()
