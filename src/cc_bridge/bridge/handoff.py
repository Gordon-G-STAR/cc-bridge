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

from .contracts import CONTRACT_VERSION, FailureKind, HandoffRequest, HandoffResult
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

    route = f"{agent}(只读骨架)"
    if request.requested_scope.writable_paths:
        route = f"{route};{READONLY_SKELETON_NOTE}"

    return HandoffResult(
        contract_version=CONTRACT_VERSION,
        handoff_id=handoff_id,
        status="completed" if result.success else "failed",
        agent_used=agent,            # "codex" / "claude"
        route_reason=route,
        summary=summary,
        verified_files_changed=[],   # 骨架只读 + 无证据子系统 => 绝不声称 verified
        scope_violations=[],
        checks=[],
        failure_kind=failure_kind,
        duration_seconds=result.duration_seconds,
        token_usage=result.token_usage,
        evidence_level="unknown",    # PR4 证据未接入
    )
