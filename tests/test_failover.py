"""PR7 —— 透明 failover 单元测试。

不变量:仅当主 agent【可证明零副作用】(agent_unavailable + 工作区证据为空)且合同
allow_fallback=True 时,才切到另一个 agent;切换在 orchestrator、route_reason 透明、
显式选择优先。
"""

from __future__ import annotations

from cc_bridge.bridge import handoff
from cc_bridge.bridge.config import BridgeConfig
from cc_bridge.bridge.contracts import (
    FailureKind,
    HandoffRequest,
    HandoffResult,
    RequestedScope,
    SideEffects,
)


def _result(
    failure_kind=None, *, verified=None, violations=None, worktree="none", status="failed"
) -> HandoffResult:
    return HandoffResult(
        contract_version="1",
        handoff_id="h",
        status=status,
        agent_used="codex",
        failure_kind=failure_kind,
        verified_files_changed=verified or [],
        scope_violations=violations or [],
        side_effects=SideEffects(worktree_files=worktree),
    )


def _req(allow_fallback=False) -> HandoffRequest:
    return HandoffRequest(
        contract_version="1",
        goal="g",
        requested_scope=RequestedScope(),
        allow_fallback=allow_fallback,
    )


# ---------------------------------------------------------------------------
# is_failover_safe —— 严格的"可证明零副作用"
# ---------------------------------------------------------------------------

def test_failover_safe_only_for_agent_unavailable():
    assert handoff.is_failover_safe(_result(FailureKind.agent_unavailable)) is True
    assert handoff.is_failover_safe(_result(FailureKind.timeout)) is False
    assert handoff.is_failover_safe(_result(FailureKind.crashed)) is False
    assert handoff.is_failover_safe(_result(FailureKind.rate_limited)) is False
    assert handoff.is_failover_safe(_result(FailureKind.scope_violation)) is False
    assert handoff.is_failover_safe(_result(None)) is False


def test_failover_unsafe_if_any_effect_evidence():
    assert handoff.is_failover_safe(
        _result(FailureKind.agent_unavailable, verified=["a.py"])
    ) is False
    assert handoff.is_failover_safe(
        _result(FailureKind.agent_unavailable, violations=["b.py"])
    ) is False
    assert handoff.is_failover_safe(
        _result(FailureKind.agent_unavailable, worktree="detected_but_not_reverted")
    ) is False


def test_failover_unsafe_if_any_nonworktree_category_detected():
    """非 worktree 类别(如 processes / git_refs)检出副作用也不切——双保险。"""
    r = HandoffResult(
        contract_version="1",
        handoff_id="h",
        status="failed",
        agent_used="codex",
        failure_kind=FailureKind.agent_unavailable,
        side_effects=SideEffects(
            worktree_files="none", processes="detected_but_not_reverted"
        ),
    )
    assert handoff.is_failover_safe(r) is False


# ---------------------------------------------------------------------------
# maybe_failover —— 编排层切换
# ---------------------------------------------------------------------------

async def test_maybe_failover_disabled_without_allow_fallback(monkeypatch):
    primary = _result(FailureKind.agent_unavailable)

    async def _boom(*a, **k):
        raise AssertionError("未开启 allow_fallback 不应 failover")

    monkeypatch.setattr(handoff, "execute_fallback", _boom)
    out = await handoff.maybe_failover(
        primary, primary_agent="codex", request=_req(False),
        cwd="x", cfg=BridgeConfig(), caller="claude",
    )
    assert out is primary


async def test_maybe_failover_triggers_when_safe(monkeypatch):
    primary = _result(FailureKind.agent_unavailable)
    fb = HandoffResult(
        contract_version="1", handoff_id="h2", status="completed",
        agent_used="claude", route_reason="claude 接手完成",
    )
    captured = {}

    async def _fb(target, request, cwd, *, cfg, caller, on_progress=None):
        captured["target"] = target
        return fb

    monkeypatch.setattr(handoff, "execute_fallback", _fb)
    out = await handoff.maybe_failover(
        primary, primary_agent="codex", request=_req(True),
        cwd="x", cfg=BridgeConfig(), caller="claude",
    )
    assert captured["target"] == "claude"          # 切到【另一个】 agent
    assert out.agent_used == "claude"
    assert "failover" in out.route_reason.lower()   # route_reason 透明
    assert "claude 接手完成" in out.route_reason     # 保留 fallback 自身的说明


async def test_maybe_failover_skips_when_unsafe(monkeypatch):
    primary = _result(FailureKind.crashed)           # 非"可证明零副作用"

    async def _boom(*a, **k):
        raise AssertionError("不安全失败不应切")

    monkeypatch.setattr(handoff, "execute_fallback", _boom)
    out = await handoff.maybe_failover(
        primary, primary_agent="codex", request=_req(True),
        cwd="x", cfg=BridgeConfig(), caller="claude",
    )
    assert out is primary
