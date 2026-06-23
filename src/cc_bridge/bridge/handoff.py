"""结构化委派(Contracted Handoff)在 MCP 工具层的共享装配逻辑。

把 :class:`~cc_bridge.bridge.executor.ExecutionResult` 映射成
:class:`~cc_bridge.bridge.contracts.HandoffResult`,并构造交给对方 agent 的目标文本。
两个方向的 server(``mcp_to_codex`` / ``mcp_to_claude``)共用,避免重复——也保证
**只有一条强制路径**:无论哪个方向,都先经 :func:`authorize` 做本地策略重授权(PR5),
再交给执行器;绝无第二条绕过 policy 的路。

PR5 起,handoff 不再恒为只读骨架:``requested_scope`` 经
``effective = requested ∩ inherited ∩ local_user_policy ∩ engine_limits`` 收窄后,
在生效范围内可授予写入(``decide_scope``);拒绝 / 需审批 / 链深超限一律 fail-closed。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from . import evidence, policy as policy_mod
from .config import BridgeConfig
from .context import ContextBuilder
from .contracts import (
    CONTRACT_VERSION,
    FailureKind,
    HandoffRequest,
    HandoffResult,
    SideEffects,
    SideEffectStatus,
    fail_closed_result,
)
from .evidence import EvidenceResult
from .executor import AgentExecutor, ExecutionResult
from .locks import async_project_lock
from .parser import ResultParser


def handoff_goal_text(request: HandoffRequest) -> str:
    """把合同的 goal + acceptance_criteria 拼成交给对方 agent 的目标文本。"""
    parts = [request.goal.strip()]
    if request.acceptance_criteria:
        parts.append(
            "验收标准:\n" + "\n".join(f"- {c}" for c in request.acceptance_criteria)
        )
    return "\n\n".join(parts)


@dataclass(frozen=True)
class HandoffPlan:
    """一次被授权的 handoff 执行计划(``decide_scope`` 判 grant 后产出)。"""

    agent: str
    write_granted: bool
    effective_writable: tuple[str, ...]
    network_granted: bool
    engine_mode: str            # codex sandbox 或 claude permission mode
    child_env: dict[str, str]   # 下传给子进程的链路 env(depth+1 + 授予的 scope)
    depth: int
    route_note: str


def _approval_required_result(
    handoff_id: str, *, reason: str, agent: str
) -> HandoffResult:
    return HandoffResult(
        contract_version=CONTRACT_VERSION,
        handoff_id=handoff_id,
        status="approval_required",
        agent_used=agent,
        route_reason=reason,
        summary=reason,
        failure_kind=None,
        evidence_level="unknown",
    )


def authorize(
    handoff_id: str,
    request: HandoffRequest,
    project_root: str,
    *,
    agent: str,
    cfg: BridgeConfig,
    policy: policy_mod.LocalPolicy | None = None,
    chain: policy_mod.ChainContext | None = None,
    provider: policy_mod.ApprovalProvider | None = None,
) -> HandoffPlan | HandoffResult:
    """本地策略重授权。grant => :class:`HandoffPlan`;deny / approval_required => fail-closed
    :class:`HandoffResult`(调用方原样返回,绝不执行)。"""
    policy = policy or policy_mod.LocalPolicy.from_env()
    chain = chain or policy_mod.ChainContext.from_env()
    provider = provider or policy_mod.get_approval_provider()

    decision = policy_mod.decide_scope(
        request.requested_scope,
        policy=policy,
        chain=chain,
        handoff_id=handoff_id,
        project_root=project_root,
        provider=provider,
    )

    if decision.decision is policy_mod.Decision.deny:
        return fail_closed_result(
            handoff_id,
            failure_kind=decision.failure_kind or FailureKind.policy_denied,
            reason=decision.reason,
            status="policy_denied",
        )
    if decision.decision is policy_mod.Decision.approval_required:
        return _approval_required_result(
            handoff_id, reason=decision.reason, agent=agent
        )

    if agent == "codex":
        engine_mode = policy_mod.effective_codex_sandbox(
            decision.write_granted, cfg.codex_sandbox
        )
    else:
        engine_mode = policy_mod.effective_claude_permission(
            decision.write_granted, cfg.claude_permission_mode
        )
    # 引擎上限可能把"授予写"再次钳成只读;以最终引擎模式为准回填 write_granted。
    write_granted = engine_mode not in ("read-only", "plan")
    effective_writable = decision.effective_writable if write_granted else ()
    child_env = chain.child_env(
        effective_writable, "request" if decision.network_granted else "deny"
    )
    route_note = f"{agent}(深度 {decision.depth};{decision.reason}"
    if write_granted != decision.write_granted:
        route_note += ";引擎上限将写入钳为只读"
    route_note += f";引擎模式 {engine_mode})"

    return HandoffPlan(
        agent=agent,
        write_granted=write_granted,
        effective_writable=effective_writable,
        network_granted=decision.network_granted,
        engine_mode=engine_mode,
        child_env=child_env,
        depth=decision.depth,
        route_note=route_note,
    )


def execution_to_handoff(
    handoff_id: str,
    request: HandoffRequest,
    result: ExecutionResult,
    summary: str,
    agent: str,
    evidence: EvidenceResult | None = None,
    plan: HandoffPlan | None = None,
) -> HandoffResult:
    """把一次执行的 ExecutionResult + 证据 + 授权计划装配成结构化 HandoffResult。"""
    failure_kind: FailureKind | None = None
    if not result.success:
        if result.timed_out:
            failure_kind = FailureKind.timeout
        elif result.agent_unavailable:
            failure_kind = FailureKind.agent_unavailable
        else:
            failure_kind = FailureKind.crashed

    route = plan.route_note if plan is not None else agent
    network_status = "unsupported"  # 网络无法真强制 / 验证 => 诚实标 unsupported

    if evidence is not None:
        if evidence.scope_violations:
            status = "scope_violation"
            failure_kind = FailureKind.scope_violation
            worktree_files = "detected_but_not_reverted"
        else:
            status = "completed" if result.success else "failed"
            # 没有回滚子系统(PR4 未接入回滚);有过改动即标"已检出未回滚",无改动标 none。
            worktree_files = (
                "detected_but_not_reverted" if evidence.verified_files else "none"
            )

        return HandoffResult(
            contract_version=CONTRACT_VERSION,
            handoff_id=handoff_id,
            status=status,
            agent_used=agent,
            route_reason=route,
            summary=summary,
            verified_files_changed=evidence.verified_files,
            scope_violations=evidence.scope_violations,
            checks=[],
            failure_kind=failure_kind,
            side_effects=SideEffects(
                worktree_files=worktree_files, network=network_status
            ),
            duration_seconds=result.duration_seconds,
            token_usage=result.token_usage,
            evidence_level=evidence.evidence_level,
        )

    # 无证据子系统:防御性地把"报告了文件改动"显式标成 scope_violation,绝不静默吞。
    status = "completed" if result.success else "failed"
    scope_violations: list[str] = []
    files_changed = list(result.files_changed or [])
    if files_changed and not (plan is not None and plan.write_granted):
        status = "scope_violation"
        scope_violations = files_changed
        failure_kind = FailureKind.scope_violation
        route = f"{route};异常:未授权写入却检测到文件改动 {files_changed},已标为越界"

    return HandoffResult(
        contract_version=CONTRACT_VERSION,
        handoff_id=handoff_id,
        status=status,
        agent_used=agent,
        route_reason=route,
        summary=summary,
        verified_files_changed=[],
        scope_violations=scope_violations,
        checks=[],
        failure_kind=failure_kind,
        side_effects=SideEffects(network=network_status),
        duration_seconds=result.duration_seconds,
        token_usage=result.token_usage,
        evidence_level="unknown",
    )


# ---------------------------------------------------------------------------
# PR7:透明 failover —— 仅在主 agent【可证明零副作用】时,才切到另一个 agent。
#
# 严格的"side_effect_state == none"证明:唯一满足的失败是 ``agent_unavailable``——
# 对应对方 CLI 缺失 / 启动失败,子进程【从未运行】,故任何类别(磁盘 / 进程 / 网络 /
# 外部服务)都不可能有副作用。其余失败(超时 / 崩溃 / 限流)可能已执行,网络又是
# ``unsupported``(无法证明为 none),一律不切。额外校验工作区证据为空作双保险。
#
# 设计:failover 在 orchestrator(本模块)做,不进 ``AgentExecutor``;``route_reason``
# 透明记录切换;只有 ``request.allow_fallback=True`` 才启用——显式指定的工具(主 agent)
# 永远优先,自动 failover 绝不覆盖显式选择。
# ---------------------------------------------------------------------------

# 仅这些失败种类【可能】可证明零副作用(再叠加证据校验)。
# 当前只有 agent_unavailable:它由 executor 的 ``ExecutionResult.agent_unavailable`` 推出
# (exit_code 为 None、未超时、未成功)——子进程【从未产生退出码】,即从未真正启动 / 启动失败,
# 故任何类别(磁盘 / 进程 / 网络 / 外部服务)都不可能有副作用。这是【结构化】结论,非自由文本。
_FAILOVER_SAFE_KINDS = frozenset({FailureKind.agent_unavailable})

# 任何类别一旦【检出】副作用(已回滚 / 未回滚)就绝不切——双保险,防 agent_unavailable
# 语义未来被改动后悄悄放行。
_DETECTED_STATUSES = frozenset(
    {SideEffectStatus.detected_and_reverted, SideEffectStatus.detected_but_not_reverted}
)
_SIDE_EFFECT_FIELDS = (
    "worktree_files",
    "outside_project_files",
    "git_index",
    "git_refs",
    "processes",
    "external_services",
)


def is_failover_safe(result: HandoffResult) -> bool:
    """主 agent 是否【可证明零副作用】,从而允许 failover。

    三重证明:(1) ``failure_kind == agent_unavailable``(进程从未启动);(2) 无 verified 写、
    无越界;(3) 副作用向量里【任何】类别都未检出效果(detected_*),且工作区为 ``none``。
    任一不满足即不切——绝不在可能已落盘 / 已产生外部效果后改交另一个 agent。
    """
    if result.failure_kind not in _FAILOVER_SAFE_KINDS:
        return False
    if result.verified_files_changed or result.scope_violations:
        return False
    se = result.side_effects
    if any(getattr(se, field) in _DETECTED_STATUSES for field in _SIDE_EFFECT_FIELDS):
        return False
    return se.worktree_files == SideEffectStatus.none


def _other_agent(agent: str) -> str:
    return "claude" if agent == "codex" else "codex"


async def execute_fallback(
    target_agent: str,
    request: HandoffRequest,
    cwd: str,
    *,
    cfg: BridgeConfig,
    caller: str,
    on_progress=None,
) -> HandoffResult:
    """在 orchestrator 层对【另一个】agent 执行同一份合同(failover 的执行腿)。

    走与主路径完全相同的强制路径:本地策略重授权 -> 上下文 -> 跨进程项目锁 -> 执行 -> 证据。
    绝不在 ``AgentExecutor`` 内部做切换。
    """
    handoff_id = uuid.uuid4().hex[:12]
    plan = authorize(handoff_id, request, cwd, agent=target_agent, cfg=cfg)
    if isinstance(plan, HandoffResult):
        return plan

    before = evidence.baseline(cwd)
    project_ctx = await asyncio.to_thread(ContextBuilder().build_project_context, cwd)
    prompt = ContextBuilder().build_task_prompt(
        handoff_goal_text(request), project_ctx, caller=caller
    )
    executor = AgentExecutor(cfg)
    async with async_project_lock(cwd, timeout=5.0):
        if target_agent == "codex":
            result = await executor.run_codex(
                prompt,
                cwd,
                timeout=request.timeout_seconds,
                on_progress=on_progress,
                sandbox_override=plan.engine_mode,
                extra_env=plan.child_env,
            )
        else:
            result = await executor.run_claude(
                prompt,
                cwd,
                timeout=request.timeout_seconds,
                on_progress=on_progress,
                permission_override=plan.engine_mode,
                extra_env=plan.child_env,
            )
    ev = evidence.gather(cwd, before, writable_paths=plan.effective_writable)
    parsed = ResultParser().parse(result, target_agent)
    summary = ResultParser().summarize_for_caller(parsed, target_agent)
    return execution_to_handoff(
        handoff_id, request, result, summary, target_agent, evidence=ev, plan=plan
    )


def _annotate_failover(
    fb_result: HandoffResult, *, primary_agent: str, primary_result: HandoffResult, target: str
) -> HandoffResult:
    # 措辞如实:只说"已改交 target 处理",不预设 target 一定成功——target 自身可能再被
    # 本地策略拒绝 / 失败,其真实 status 已在 fb_result 里,这里只补一句切换的来由。
    kind = primary_result.failure_kind.value if primary_result.failure_kind else "?"
    note = (
        f"透明 failover:主 agent {primary_agent} 不可用(failure_kind={kind})、已证明无副作用,"
        f"本次委派已改交 {target} 处理;{target} 的结果如下。"
    )
    return fb_result.model_copy(
        update={"route_reason": f"{note} | {fb_result.route_reason}"}
    )


async def maybe_failover(
    primary_result: HandoffResult,
    *,
    primary_agent: str,
    request: HandoffRequest,
    cwd: str,
    cfg: BridgeConfig,
    caller: str,
    on_progress=None,
) -> HandoffResult:
    """主结果若【可证明零副作用】且合同允许回退,则透明切到另一个 agent;否则原样返回。

    锁:本函数在【主路径项目锁释放后】运行,``execute_fallback`` 再自行获取同一把跨进程锁
    (同进程换 fd 重入同一锁会死锁,故绝不在持锁时调用)。这看似留出"主验证→回退执行"的
    竞态窗口,但当前唯一的 failover-safe 失败是 ``agent_unavailable``——主 agent【从未运行】、
    零改动,无任何跨锁不变量需要守护;回退腿又有自己完整的 baseline→exec→evidence 事务。
    将来若放宽 failover-safe 到"真跑过但无副作用",必须把主验证与回退收进同一把事务锁。
    """
    if not request.allow_fallback or not is_failover_safe(primary_result):
        return primary_result
    target = _other_agent(primary_agent)
    fb_result = await execute_fallback(
        target, request, cwd, cfg=cfg, caller=caller, on_progress=on_progress
    )
    return _annotate_failover(
        fb_result, primary_agent=primary_agent, primary_result=primary_result, target=target
    )
