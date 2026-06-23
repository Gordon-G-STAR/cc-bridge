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

from dataclasses import dataclass

from . import policy as policy_mod
from .config import BridgeConfig
from .contracts import (
    CONTRACT_VERSION,
    FailureKind,
    HandoffRequest,
    HandoffResult,
    SideEffects,
    fail_closed_result,
)
from .evidence import EvidenceResult
from .executor import ExecutionResult


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
