"""PR5 —— 本地策略重授权(policy.py)单元测试。

覆盖:effective = requested ∩ inherited ∩ local_user_policy ∩ engine_limits、
链深上限、headless 审批 fail-closed、子只能收窄、引擎上限钳制、legacy 同一地板。
都是纯函数,可对抗测;绝不真正起子进程。
"""

from __future__ import annotations

import json

import pytest

from cc_bridge.bridge import policy
from cc_bridge.bridge.contracts import FailureKind, RequestedScope

_ENV_NAMES = (
    policy.WRITABLE_PATHS_ENV,
    policy.READONLY_ENV,
    policy.ALLOW_NETWORK_ENV,
    policy.MAX_DEPTH_ENV,
    policy.REQUIRE_APPROVAL_ENV,
    policy.LEGACY_TOOLS_ENV,
    policy.CHAIN_DEPTH_ENV,
    policy.CHAIN_SCOPE_ENV,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _scope(writable=(), network="deny") -> RequestedScope:
    return RequestedScope(writable_paths=list(writable), network=network)


def _decide(scope, *, pol=None, chain=None, provider=None):
    pol = pol or policy.LocalPolicy.from_env()
    chain = chain or policy.ChainContext.from_env()
    provider = provider or policy.get_approval_provider()
    return policy.decide_scope(
        scope,
        policy=pol,
        chain=chain,
        handoff_id="h",
        project_root="/proj",
        provider=provider,
    )


class _AllowProvider:
    def approve_writes(self, **_kwargs) -> bool:
        return True


# ---------------------------------------------------------------------------
# LocalPolicy.from_env
# ---------------------------------------------------------------------------

def test_default_policy_allows_project_root_writes():
    pol = policy.LocalPolicy.from_env()
    assert pol.writable_paths == (".",)
    assert pol.allow_network is False
    assert pol.max_chain_depth == 3
    assert pol.require_approval_for_writes is False
    assert pol.legacy_tools_enabled is True


def test_readonly_env_overrides_writable(monkeypatch):
    monkeypatch.setenv(policy.READONLY_ENV, "1")
    monkeypatch.setenv(policy.WRITABLE_PATHS_ENV, "src")  # 被 READONLY 覆盖
    assert policy.LocalPolicy.from_env().writable_paths == ()


def test_writable_paths_env_parsed(monkeypatch):
    import os
    monkeypatch.setenv(policy.WRITABLE_PATHS_ENV, f"src{os.pathsep}docs/api")
    assert policy.LocalPolicy.from_env().writable_paths == ("src", "docs/api")


# ---------------------------------------------------------------------------
# decide_scope —— 核心收窄
# ---------------------------------------------------------------------------

def test_default_grants_project_writes():
    d = _decide(_scope(["src/auth"]))
    assert d.decision is policy.Decision.grant
    assert d.write_granted is True
    assert d.effective_writable == ("src/auth",)


def test_empty_request_is_readonly_grant():
    d = _decide(_scope())
    assert d.decision is policy.Decision.grant
    assert d.write_granted is False
    assert d.effective_writable == ()


def test_policy_narrows_to_allowed_subtree(monkeypatch):
    monkeypatch.setenv(policy.WRITABLE_PATHS_ENV, "src")
    d = _decide(_scope(["src/auth", "docs/x"]))
    assert d.effective_writable == ("src/auth",)  # docs/x 被本地策略剔除
    assert d.write_granted is True


def test_component_level_not_prefix(monkeypatch):
    """src/auth 不得命中 src/auth_secrets(分量级,不是字符串前缀)。"""
    monkeypatch.setenv(policy.WRITABLE_PATHS_ENV, "src/auth")
    d = _decide(_scope(["src/auth_secrets/leak"]))
    assert d.effective_writable == ()
    assert d.write_granted is False


def test_readonly_policy_downgrades_requested_writes(monkeypatch):
    monkeypatch.setenv(policy.READONLY_ENV, "1")
    d = _decide(_scope(["src/auth"]))
    assert d.decision is policy.Decision.grant
    assert d.write_granted is False
    assert "降级为只读" in d.reason


# ---------------------------------------------------------------------------
# 链路 depth + 父链收窄
# ---------------------------------------------------------------------------

def test_depth_at_limit_denies(monkeypatch):
    monkeypatch.setenv(policy.MAX_DEPTH_ENV, "2")
    monkeypatch.setenv(policy.CHAIN_DEPTH_ENV, "2")
    d = _decide(_scope(["src"]))
    assert d.decision is policy.Decision.deny
    assert d.failure_kind is FailureKind.policy_denied
    assert "无界再入" in d.reason


def test_depth_below_limit_proceeds(monkeypatch):
    monkeypatch.setenv(policy.MAX_DEPTH_ENV, "3")
    monkeypatch.setenv(policy.CHAIN_DEPTH_ENV, "1")
    d = _decide(_scope(["src"]))
    assert d.decision is policy.Decision.grant
    assert d.depth == 1


def test_child_can_only_narrow(monkeypatch):
    """父链授予 src;子申请 src/auth + lib/y => 只剩 src/auth。"""
    monkeypatch.setenv(
        policy.CHAIN_SCOPE_ENV,
        json.dumps({"writable_paths": ["src"], "network": "deny"}),
    )
    d = _decide(_scope(["src/auth", "lib/y"]))
    assert d.effective_writable == ("src/auth",)


def test_inherited_empty_scope_blocks_all_writes(monkeypatch):
    monkeypatch.setenv(
        policy.CHAIN_SCOPE_ENV,
        json.dumps({"writable_paths": [], "network": "deny"}),
    )
    d = _decide(_scope(["src/auth"]))
    assert d.effective_writable == ()
    assert d.write_granted is False


def test_malformed_chain_scope_fails_closed(monkeypatch):
    monkeypatch.setenv(policy.CHAIN_SCOPE_ENV, "this is not json {")
    chain = policy.ChainContext.from_env()
    assert chain.inherited_writable == ()  # 解析失败 => 最严收窄
    d = _decide(_scope(["src/auth"]), chain=chain)
    assert d.effective_writable == ()


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_present_but_blank_chain_scope_fails_closed(monkeypatch, blank):
    """env 存在但为空 / 空白 => 视为父链声明过 scope,fail-closed 收窄(不得退回"无父链")。"""
    monkeypatch.setenv(policy.CHAIN_SCOPE_ENV, blank)
    chain = policy.ChainContext.from_env()
    assert chain.inherited_writable == ()   # 关键:不是 None
    d = _decide(_scope(["src/auth"]), chain=chain)
    assert d.effective_writable == ()
    assert d.write_granted is False


def test_absent_chain_scope_is_root_no_narrowing(monkeypatch):
    """env 完全缺失(None)=> 根调用,不施加继承收窄,本地策略说了算。"""
    monkeypatch.delenv(policy.CHAIN_SCOPE_ENV, raising=False)
    chain = policy.ChainContext.from_env()
    assert chain.inherited_writable is None
    d = _decide(_scope(["src/auth"]), chain=chain)
    assert d.effective_writable == ("src/auth",)


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------

def test_network_denied_by_default():
    d = _decide(_scope(network="request"))
    assert d.network_granted is False


def test_network_granted_when_policy_allows(monkeypatch):
    monkeypatch.setenv(policy.ALLOW_NETWORK_ENV, "1")
    d = _decide(_scope(network="request"))
    assert d.network_granted is True


def test_network_blocked_by_inherited(monkeypatch):
    monkeypatch.setenv(policy.ALLOW_NETWORK_ENV, "1")
    monkeypatch.setenv(
        policy.CHAIN_SCOPE_ENV,
        json.dumps({"writable_paths": ["."], "network": "deny"}),
    )
    d = _decide(_scope(network="request"))
    assert d.network_granted is False


# ---------------------------------------------------------------------------
# 审批
# ---------------------------------------------------------------------------

def test_approval_required_headless_fails_closed(monkeypatch):
    monkeypatch.setenv(policy.REQUIRE_APPROVAL_ENV, "1")
    d = _decide(_scope(["src/auth"]))  # 默认 DenyAll provider
    assert d.decision is policy.Decision.approval_required
    assert d.write_granted is False


def test_approval_granted_by_provider(monkeypatch):
    monkeypatch.setenv(policy.REQUIRE_APPROVAL_ENV, "1")
    d = _decide(_scope(["src/auth"]), provider=_AllowProvider())
    assert d.decision is policy.Decision.grant
    assert d.write_granted is True


def test_approval_not_needed_for_readonly(monkeypatch):
    monkeypatch.setenv(policy.REQUIRE_APPROVAL_ENV, "1")
    d = _decide(_scope())  # 没有写申请 => 不触发审批
    assert d.decision is policy.Decision.grant


# ---------------------------------------------------------------------------
# 引擎上限钳制
# ---------------------------------------------------------------------------

def test_codex_sandbox_clamps():
    assert policy.effective_codex_sandbox(True, "workspace-write") == "workspace-write"
    assert policy.effective_codex_sandbox(False, "workspace-write") == "read-only"
    assert policy.effective_codex_sandbox(True, "read-only") == "read-only"
    # 绝不返回 danger-full-access
    assert policy.effective_codex_sandbox(True, "danger-full-access") == "workspace-write"


def test_claude_permission_clamps():
    assert policy.effective_claude_permission(False, "bypassPermissions") == "plan"
    assert policy.effective_claude_permission(True, "plan") == "plan"
    assert policy.effective_claude_permission(True, "bypassPermissions") == "bypassPermissions"
    assert policy.effective_claude_permission(True, None) == "bypassPermissions"


# ---------------------------------------------------------------------------
# ChainContext.child_env(producer)
# ---------------------------------------------------------------------------

def test_child_env_bumps_depth_and_carries_scope():
    chain = policy.ChainContext(depth=1, inherited_writable=None, inherited_network=None)
    env = chain.child_env(("src/auth",), "deny")
    assert env[policy.CHAIN_DEPTH_ENV] == "2"
    data = json.loads(env[policy.CHAIN_SCOPE_ENV])
    assert data["writable_paths"] == ["src/auth"]
    assert data["network"] == "deny"


# ---------------------------------------------------------------------------
# decide_legacy —— 同一 policy 地板
# ---------------------------------------------------------------------------

def test_legacy_default_keeps_workspace_write():
    pol = policy.LocalPolicy.from_env()
    chain = policy.ChainContext.from_env()
    d = policy.decide_legacy(
        agent="codex", policy=pol, chain=chain,
        codex_cap="workspace-write", claude_cap="bypassPermissions",
    )
    assert d.refusal is None
    assert d.engine_mode == "workspace-write"
    assert d.write_granted is True
    assert d.child_env[policy.CHAIN_DEPTH_ENV] == "1"


def test_legacy_disabled_refuses(monkeypatch):
    monkeypatch.setenv(policy.LEGACY_TOOLS_ENV, "0")
    pol = policy.LocalPolicy.from_env()
    chain = policy.ChainContext.from_env()
    d = policy.decide_legacy(
        agent="codex", policy=pol, chain=chain,
        codex_cap="workspace-write", claude_cap="bypassPermissions",
    )
    assert d.refusal is not None
    assert "禁用" in d.refusal


def test_legacy_depth_exceeded_refuses(monkeypatch):
    monkeypatch.setenv(policy.MAX_DEPTH_ENV, "1")
    monkeypatch.setenv(policy.CHAIN_DEPTH_ENV, "1")
    pol = policy.LocalPolicy.from_env()
    chain = policy.ChainContext.from_env()
    d = policy.decide_legacy(
        agent="codex", policy=pol, chain=chain,
        codex_cap="workspace-write", claude_cap="bypassPermissions",
    )
    assert d.refusal is not None
    assert "链路深度" in d.refusal


def test_legacy_readonly_policy_clamps(monkeypatch):
    monkeypatch.setenv(policy.READONLY_ENV, "1")
    pol = policy.LocalPolicy.from_env()
    chain = policy.ChainContext.from_env()
    d = policy.decide_legacy(
        agent="codex", policy=pol, chain=chain,
        codex_cap="workspace-write", claude_cap="bypassPermissions",
    )
    assert d.refusal is None
    assert d.engine_mode == "read-only"
    assert d.write_granted is False


def test_legacy_claude_uses_permission_mode():
    pol = policy.LocalPolicy.from_env()
    chain = policy.ChainContext.from_env()
    d = policy.decide_legacy(
        agent="claude", policy=pol, chain=chain,
        codex_cap="workspace-write", claude_cap="bypassPermissions",
    )
    assert d.engine_mode == "bypassPermissions"
    assert d.write_granted is True
