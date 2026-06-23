"""MCP server：让 Codex 调用 Claude（装进 Codex）.

这个 server 把 Claude CLI 暴露成 Codex 里的两个工具：

- ``claude_analyze``：把任务连同当前项目上下文转交给 Claude，让它做架构评审、
  复杂调试、方案设计（必要时也会改代码），再把结果摘要返回给 Codex；
- ``claude_status``：报告 Claude 是否就绪（命令行可用 / 已登录 / 版本）。

调用链一律复用「地基」组件，绝不重复实现 CLI 调用逻辑：
ContextBuilder -> AgentExecutor.run_claude -> ResultParser。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

from . import config, evidence, policy
from .audit import append_audit_record
from .context import ContextBuilder, require_project_dir
from .contracts import FailureKind, HandoffRequest, HandoffResult, fail_closed_result
from .executor import AgentExecutor
from .handoff import authorize, execution_to_handoff, handoff_goal_text
from .locks import LockBusy, async_project_lock
from .parser import ResultParser
from .status import check_claude

mcp = FastMCP("bridge-to-claude")

_DRY_RUN_PROMPT_SUFFIX = (
    "\n\n这是预演（dry run）：只描述将要做的改动、会改哪些文件，"
    "不要真正修改文件或落盘。"
)
_DRY_RUN_SUMMARY_PREFIX = "【dry-run 预演】\n"
_DRY_RUN_CLAUDE_PERMISSION_MODE = "plan"

_CLAUDE_SESSIONS_BY_PROJECT: dict[str, str] = {}
_CLAUDE_SESSION_LOCKS_BY_PROJECT: dict[str, asyncio.Lock] = {}
_CLAUDE_SESSION_CACHE_MAX_PROJECTS = 256


def _trim_claude_session_caches(protected_cwd: str | None = None) -> None:
    max_projects = _CLAUDE_SESSION_CACHE_MAX_PROJECTS
    if max_projects < 1:
        max_projects = 1

    def _project_count() -> int:
        return len(
            set(_CLAUDE_SESSIONS_BY_PROJECT) | set(_CLAUDE_SESSION_LOCKS_BY_PROJECT)
        )

    while _project_count() > max_projects:
        candidates = [
            *list(_CLAUDE_SESSION_LOCKS_BY_PROJECT),
            *[
                cwd
                for cwd in _CLAUDE_SESSIONS_BY_PROJECT
                if cwd not in _CLAUDE_SESSION_LOCKS_BY_PROJECT
            ],
        ]
        evicted = False
        for cwd in candidates:
            if cwd == protected_cwd:
                continue
            lock = _CLAUDE_SESSION_LOCKS_BY_PROJECT.get(cwd)
            if lock is not None and lock.locked():
                continue
            _CLAUDE_SESSION_LOCKS_BY_PROJECT.pop(cwd, None)
            _CLAUDE_SESSIONS_BY_PROJECT.pop(cwd, None)
            evicted = True
            break
        if not evicted:
            break


def _claude_session_lock(cwd: str) -> asyncio.Lock:
    lock = _CLAUDE_SESSION_LOCKS_BY_PROJECT.get(cwd)
    if lock is None:
        lock = asyncio.Lock()
        _CLAUDE_SESSION_LOCKS_BY_PROJECT[cwd] = lock
        _trim_claude_session_caches(protected_cwd=cwd)
    return lock


def _remember_claude_session(cwd: str, session_id: str) -> None:
    _CLAUDE_SESSIONS_BY_PROJECT[cwd] = session_id
    _trim_claude_session_caches(protected_cwd=cwd)


def _mark_dry_run_summary(summary: str) -> str:
    return f"{_DRY_RUN_SUMMARY_PREFIX}{summary}"


def _make_progress_callback(ctx: Context | None):
    """把 executor 的进度标签转成 MCP progress/info；任何异常都静默降级。"""
    if ctx is None:
        return None
    state = {"count": 0, "last_message": "", "last_at": 0.0}

    async def _on_progress(message: str) -> None:
        try:
            text = " ".join(str(message).split())
            if not text or text == state["last_message"]:
                return
            now = time.monotonic()
            if state["count"] and now - state["last_at"] < 0.25:
                return
            if len(text) > 240:
                text = text[:237] + "..."
            state["count"] += 1
            state["last_message"] = text
            state["last_at"] = now
            await ctx.report_progress(
                progress=float(state["count"]), total=None, message=text
            )
            await ctx.info(text)
        except Exception:
            pass

    return _on_progress


@mcp.tool(
    name="claude_analyze",
    description=(
        "把一个任务转交给 Claude（Anthropic 的编码 agent）去处理。"
        "Claude 尤其擅长架构评审、复杂调试、方案/技术设计与权衡分析；"
        "必要时它也会在指定项目目录里【实际修改代码】。它走用户自己的 "
        "Claude Max 订阅额度（不消耗 Codex/ChatGPT 的额度）。\n\n"
        "调用约定：\n"
        "- task：用清晰的自然语言描述要让 Claude 做的事，例如『评审这套模块的"
        "  架构并指出风险』『定位这个偶发崩溃的根因』『为 X 设计实现方案』。\n"
        "- project_dir：【必填】当前项目的【绝对路径】（例如 "
        "  'C:/Users/me/proj' 或 '/home/me/proj'）。Claude 会以此为工作目录读取"
        "  上下文、必要时改文件。【不传、传相对路径、或目录不存在都会被直接拒绝】——"
        "  cc-bridge 不会猜测目录，以免在错误的地方改文件。\n"
        "- continue_session：传 true 续接该项目上一次的 Claude 会话（多轮接力）。\n"
        "- dry_run：传 true 进入预演——Claude 只分析、说清会改什么，不真正改文件（plan 模式）。\n\n"
        "适用场景：架构/代码评审、疑难 bug 的根因分析、方案设计与取舍建议。"
        "返回值是 Claude 处理结果的自然语言摘要（含成功与否、改动的文件列表、说明）。"
    ),
)
async def claude_analyze(
    task: str,
    project_dir: str | None = None,
    continue_session: bool = False,
    dry_run: bool = False,
    ctx: Context = None,
) -> str:
    """让 Claude 在 ``project_dir`` 里处理 ``task``，返回结果摘要字符串。

    整个函数体包在 try/except 里：无论发生什么都返回友好的字符串，
    绝不把异常抛给 MCP 框架。
    """
    try:
        cwd = str(Path(require_project_dir(project_dir)).resolve())
    except (OSError, ValueError) as exc:
        return f"无法调用 Claude：{exc}"
    try:
        # 同一条 policy 地板:legacy 工具与 handoff 共用本地策略(关闭开关 / 链深上限 / 引擎钳制),
        # 绝无绕过 policy 的满权后门(F5 / #12)。dry_run 仍优先强制只读 plan。
        cfg = config.BridgeConfig.from_env()
        legacy = policy.decide_legacy(
            agent="claude",
            policy=policy.LocalPolicy.from_env(),
            chain=policy.ChainContext.from_env(),
            codex_cap=cfg.codex_sandbox,
            claude_cap=cfg.claude_permission_mode,
        )
        if legacy.refusal:
            return legacy.refusal
        on_progress = _make_progress_callback(ctx)
        # 上下文收集（git 探测 + 目录遍历）放到线程里，不阻塞事件循环、可被取消；
        # 底层 git 调用已硬化超时（见 config.git_capture）。
        config.debug_log(f"claude_analyze: 开始收集上下文 {cwd}")
        project_ctx = await asyncio.to_thread(ContextBuilder().build_project_context, cwd)
        config.debug_log("claude_analyze: 上下文就绪，开始调用 Claude")
        prompt = ContextBuilder().build_task_prompt(task, project_ctx, caller="codex")
        if dry_run:
            prompt += _DRY_RUN_PROMPT_SUFFIX
        async with _claude_session_lock(cwd):
            resume_session_id = (
                _CLAUDE_SESSIONS_BY_PROJECT.get(cwd) if continue_session else None
            )
            run_kwargs = {
                "resume_session_id": resume_session_id,
                "on_progress": on_progress,
                "permission_override": (
                    _DRY_RUN_CLAUDE_PERMISSION_MODE if dry_run else legacy.engine_mode
                ),
                "extra_env": legacy.child_env,
            }
            result = await AgentExecutor(cfg).run_claude(prompt, cwd, **run_kwargs)
            if result.session_id:
                _remember_claude_session(cwd, result.session_id)
        append_audit_record(
            direction="claude",
            cwd=cwd,
            task=task,
            success=result.success,
            files_changed=result.files_changed,
        )
        config.debug_log(f"claude_analyze: Claude 返回 success={result.success}")
        parsed = ResultParser().parse(result, "claude")
        summary = ResultParser().summarize_for_caller(parsed, "claude")
        if dry_run:
            return _mark_dry_run_summary(summary)
        return summary
    except Exception as exc:  # noqa: BLE001 — 兜底，绝不向 MCP 抛异常
        return (
            "调用 Claude 时出现内部错误，未能完成任务。\n"
            f"错误信息：{exc}\n"
            "请确认 Claude 命令行已安装并已登录（Claude 桌面版 + Max 账号），再重试。"
        )


@mcp.tool(
    name="claude_handoff",
    description=(
        "【v0.2 结构化委派】把一份结构化委派合同(HandoffRequest:目标 / 验收标准 / "
        "申请的权限范围 requested_scope / 检查项 check_ids)交给 Claude,返回结构化结果 "
        "HandoffResult。与 claude_analyze 的区别:输入输出都是版本化结构,且 requested_scope "
        "只是【申请】——由本地策略重授权(effective = requested ∩ 父链 ∩ 本地策略 ∩ 引擎上限);"
        "申请超出策略会被收窄 / 降级为只读 / 拒绝;链深超限或需审批(headless)一律 fail-closed。"
        "project_dir 必填、须为存在的绝对路径。"
    ),
)
async def claude_handoff(
    request: HandoffRequest,
    project_dir: str | None = None,
    continue_session: bool = False,
    ctx: Context = None,
) -> HandoffResult:
    """把结构化合同交给 Claude(经本地策略重授权),返回 HandoffResult,绝不向 MCP 抛异常。"""
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
        plan = authorize(handoff_id, request, cwd, agent="claude", cfg=cfg)
        if isinstance(plan, HandoffResult):
            return plan

        before = evidence.baseline(cwd)
        on_progress = _make_progress_callback(ctx)
        project_ctx = await asyncio.to_thread(
            ContextBuilder().build_project_context, cwd
        )
        prompt = ContextBuilder().build_task_prompt(
            handoff_goal_text(request), project_ctx, caller="codex"
        )
        async with async_project_lock(cwd, timeout=5.0), _claude_session_lock(cwd):
            resume_session_id = (
                _CLAUDE_SESSIONS_BY_PROJECT.get(cwd) if continue_session else None
            )
            result = await AgentExecutor(cfg).run_claude(
                prompt,
                cwd,
                timeout=request.timeout_seconds,
                resume_session_id=resume_session_id,
                on_progress=on_progress,
                permission_override=plan.engine_mode,
                extra_env=plan.child_env,
            )
            if result.session_id:
                _remember_claude_session(cwd, result.session_id)
        ev = evidence.gather(cwd, before, writable_paths=plan.effective_writable)
        append_audit_record(
            direction="claude",
            cwd=cwd,
            task=handoff_goal_text(request),
            success=result.success,
            files_changed=result.files_changed,
            extra={
                "mode": "handoff",
                "depth": plan.depth,
                "write_granted": plan.write_granted,
                "effective_writable": list(plan.effective_writable),
            },
        )
        parsed = ResultParser().parse(result, "claude")
        summary = ResultParser().summarize_for_caller(parsed, "claude")
        return execution_to_handoff(
            handoff_id, request, result, summary, "claude", evidence=ev, plan=plan
        )
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
            reason=f"调用 Claude 时内部错误:{exc}",
            status="failed",
        )


@mcp.tool(
    name="claude_status",
    description=(
        "查询 Claude 当前是否就绪：命令行是否可用、是否已登录、版本号。"
        "在调用 claude_analyze 之前如果不确定 Claude 能否工作，可以先调用它。"
    ),
)
async def claude_status() -> str:
    """返回 Claude 的运行期就绪状态，不抛异常。"""
    try:
        return check_claude().status_line()
    except Exception as exc:  # noqa: BLE001
        return f"无法检测 Claude 状态：{exc}"


def main() -> None:
    """以 stdio 方式启动 MCP server。"""
    mcp.run()


if __name__ == "__main__":
    main()
