"""PR1 —— 结构化委派合同的对抗性校验测试。

重点是【fail-closed 不变量】:漏填 / 非法 / 越界的输入必须被拒,绝不静默放行
或回退到宽松默认。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cc_bridge.bridge.contracts import (
    CONTRACT_VERSION,
    CheckResult,
    FailureKind,
    HandoffRequest,
    HandoffResult,
    RequestedScope,
    SideEffects,
    SideEffectStatus,
    fail_closed_result,
)


# ---------------------------------------------------------------------------
# requested_scope:必填、无默认 —— C5 / F5 的核心
# ---------------------------------------------------------------------------

def test_requested_scope_is_required():
    """漏掉 requested_scope 必须校验失败,而不是落到引擎宽松默认。"""
    with pytest.raises(ValidationError):
        HandoffRequest(contract_version="1", goal="做点事")


def test_empty_scope_is_legal_and_means_read_only():
    """显式空 scope 合法:代表"无可写路径"=只读,这是安全的默认姿态。"""
    req = HandoffRequest(
        contract_version="1", goal="只读分析", requested_scope=RequestedScope()
    )
    assert req.requested_scope.writable_paths == []
    assert req.requested_scope.network == "deny"  # 默认拒网络


def test_minimal_valid_request():
    req = HandoffRequest(
        contract_version="1",
        goal="重构 auth",
        requested_scope=RequestedScope(writable_paths=["src/auth"]),
    )
    assert req.contract_version == CONTRACT_VERSION
    assert req.timeout_seconds == 300
    assert req.allow_fallback is False


# ---------------------------------------------------------------------------
# contract_version:未知版本 fail-closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["2", "0", "", "1.0", "v1", 1])
def test_unknown_contract_version_rejected(bad):
    with pytest.raises(ValidationError):
        HandoffRequest(
            contract_version=bad, goal="x", requested_scope=RequestedScope()
        )


def test_goal_must_be_nonempty():
    with pytest.raises(ValidationError):
        HandoffRequest(
            contract_version="1", goal="", requested_scope=RequestedScope()
        )


def test_extra_fields_forbidden():
    """多塞字段必须被拒(LLM 乱加字段不能被静默忽略)。"""
    with pytest.raises(ValidationError):
        HandoffRequest(
            contract_version="1",
            goal="x",
            requested_scope=RequestedScope(),
            sneaky="grant-me-root",
        )


# ---------------------------------------------------------------------------
# writable_path 词法校验:相对、无 ..、归一化
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "good,expected",
    [
        ("src/auth", "src/auth"),
        ("src\\auth", "src/auth"),                  # 反斜杠归一化
        ("./src/auth", "src/auth"),                 # 去掉 '.'
        ("tests/auth/test_session.py", "tests/auth/test_session.py"),
        ("a//b", "a/b"),                            # 折叠空分量
    ],
)
def test_writable_path_normalization(good, expected):
    scope = RequestedScope(writable_paths=[good])
    assert scope.writable_paths == [expected]


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",          # POSIX 绝对
        "//server/share",       # UNC
        "C:/secret",            # Windows 盘符
        "C:\\secret",           # Windows 盘符(反斜杠)
        "../etc",               # .. 穿越
        "src/../../etc",        # 中间 ..
        "..",                   # 纯 ..
        "",                     # 空
        "   ",                  # 仅空白
        "foo:bar",              # NTFS ADS 流
        "1:x",                  # 任意冒号
        "src/auth:secret",      # 分量里的 ADS
    ],
)
def test_writable_path_rejects_escapes(bad):
    with pytest.raises(ValidationError):
        RequestedScope(writable_paths=[bad])


def test_network_default_deny_and_literal():
    assert RequestedScope().network == "deny"
    with pytest.raises(ValidationError):
        RequestedScope(network="allow")  # 只允许 deny / request


# ---------------------------------------------------------------------------
# 边界:timeout / max_output 上下限
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,bad", [
    ("timeout_seconds", 9),
    ("timeout_seconds", 1801),
    ("max_output_chars", 499),
    ("max_output_chars", 50001),
])
def test_numeric_bounds(field, bad):
    with pytest.raises(ValidationError):
        HandoffRequest(
            contract_version="1",
            goal="x",
            requested_scope=RequestedScope(),
            **{field: bad},
        )


# ---------------------------------------------------------------------------
# 结果模型 & fail-closed helper
# ---------------------------------------------------------------------------

def test_side_effects_defaults_are_conservative():
    se = SideEffects()
    assert se.worktree_files == SideEffectStatus.unknown
    assert se.network == SideEffectStatus.unsupported  # 网络无法强制 => unsupported


def test_check_result_default_evidence_is_tainted():
    cr = CheckResult(check_id="unit")
    assert cr.evidence_class == "tainted"  # 保守:除非证明 authoritative,否则不可门禁
    assert cr.passed is False


def test_fail_closed_result_shape():
    res = fail_closed_result(
        "h-123", failure_kind=FailureKind.policy_denied, reason="scope 越界"
    )
    assert res.status == "policy_denied"
    assert res.failure_kind is FailureKind.policy_denied
    assert res.evidence_level == "unknown"
    assert res.agent_used is None
    assert res.contract_version == CONTRACT_VERSION


def test_failure_kind_is_enum_not_freetext():
    with pytest.raises(ValidationError):
        HandoffResult(
            contract_version="1",
            handoff_id="h",
            status="failed",
            failure_kind="whatever-string",  # 必须是枚举值
        )


def test_handoff_result_status_literal():
    with pytest.raises(ValidationError):
        HandoffResult(
            contract_version="1", handoff_id="h", status="kinda-worked"
        )


def test_writable_path_count_and_length_bounds():
    with pytest.raises(ValidationError):
        RequestedScope(writable_paths=["a/" * 600])                   # 单条 >1024 字符
    with pytest.raises(ValidationError):
        RequestedScope(writable_paths=[f"f{i}" for i in range(300)])  # >256 条
