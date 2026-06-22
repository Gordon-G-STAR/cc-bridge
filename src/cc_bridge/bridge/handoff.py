"""结构化委派(Contracted Handoff)在 MCP 工具层的共享装配逻辑。

把 :class:`~cc_bridge.bridge.executor.ExecutionResult` 映射成
:class:`~cc_bridge.bridge.contracts.HandoffResult`,并构造交给对方 agent 的目标文本。
两个方向的 server(``mcp_to_codex`` / ``mcp_to_claude``)共用,避免重复。

**骨架阶段约定**:策略(PR5)与内容证据(PR4)子系统尚未接入,所以:

- 调用方一律【只读】执行(codex ``read-only`` / claude ``plan``);
- 不声称任何 ``verified``——``evidence_level`` 恒为 ``unknown``、
  ``verified_files_changed`` 恒为空;
- ``requested_scope.writable_paths`` 非空时,在 ``route_reason`` 里明确说明"已忽略写入申请"。
"""

from __future__ import annotations

from .contracts import (
    CONTRACT_VERSION,
    FailureKind,
    HandoffRequest,
    HandoffResult,
    SideEffects,
)
from .evidence import EvidenceResult
from .executor import ExecutionResult

READONLY_SKELETON_NOTE = (
    "[v0.2 骨架] 本次只读执行:策略授权(PR5)与内容证据(PR4)尚未接入,"
    "故不授权任何写入,requested_scope.writable_paths 暂被忽略。"
)


def handoff_goal_text(request: HandoffRequest) -> str:
    """把合同的 goal + acceptance_criteria 拼成交给对方 agent 的目标文本。"""
    parts = [request.goal.strip()]
    if request.acceptance_criteria:
        parts.append(
            "验收标准:\n" + "\n".join(f"- {c}" for c in request.acceptance_criteria)
        )
    return "\n\n".join(parts)


def execution_to_handoff(
    handoff_id: str,
    request: HandoffRequest,
    result: ExecutionResult,
    summary: str,
    agent: str,
    evidence: EvidenceResult | None = None,
) -> HandoffResult:
    """把一次只读执行的 ExecutionResult 装配成结构化 HandoffResult。"""
    failure_kind: FailureKind | None = None
    if not result.success:
        if result.timed_out:
            failure_kind = FailureKind.timeout
        elif result.agent_unavailable:
            failure_kind = FailureKind.agent_unavailable
        else:
            failure_kind = FailureKind.crashed

    status = "completed" if result.success else "failed"
    route = f"{agent}(只读骨架)"
    if request.requested_scope.writable_paths:
        route = f"{route};{READONLY_SKELETON_NOTE}"

    if evidence is not None:
        if evidence.scope_violations:
            status = "scope_violation"
            failure_kind = FailureKind.scope_violation
            worktree_files = "detected_but_not_reverted"
        else:
            status = "completed" if result.success else "failed"
            worktree_files = "none"

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
            side_effects=SideEffects(worktree_files=worktree_files),
            duration_seconds=result.duration_seconds,
            token_usage=result.token_usage,
            evidence_level=evidence.evidence_level,
        )

    # 防御性:只读执行【绝不该】报告文件改动。一旦出现(沙箱被绕过 / 底层只读保护
    # 失效),骨架虽无证据子系统去归因,但绝不能静默吞掉——显式标成 scope_violation。
    scope_violations: list[str] = []
    files_changed = list(result.files_changed or [])
    if files_changed:
        status = "scope_violation"
        scope_violations = files_changed
        failure_kind = FailureKind.scope_violation
        route = f"{route};异常:只读执行却检测到文件改动 {files_changed},已标为越界"

    return HandoffResult(
        contract_version=CONTRACT_VERSION,
        handoff_id=handoff_id,
        status=status,
        agent_used=agent,            # "codex" / "claude"
        route_reason=route,
        summary=summary,
        verified_files_changed=[],   # 骨架不声称 verified(PR4 才有证据)
        scope_violations=scope_violations,
        checks=[],
        failure_kind=failure_kind,
        duration_seconds=result.duration_seconds,
        token_usage=result.token_usage,
        evidence_level="unknown",    # PR4 证据未接入
    )
