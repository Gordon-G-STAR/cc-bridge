"""结构化委派合同(Contracted Handoff)的数据模型.

v0.2 把"一个 agent 给另一个 agent 发一段自由文本"升级成"提交一份可验证、
权限不可扩张的委派合同":

- 输入 :class:`HandoffRequest` —— 调用方只能【申请】权限(``requested_scope``),
  不能自签授权。
- 输出 :class:`HandoffResult` —— 版本化、结构化,带证据等级与按类别的副作用向量。

安全不变量(详见 ``docs/v0.2-roadmap.md``):

- ``requested_scope`` 是【必填、无默认】字段;缺失/非法 => 校验失败 => 由调用层
  fail-closed,**绝不**回退到 :class:`config.BridgeConfig` 的宽松默认
  (``workspace-write`` / ``bypassPermissions``)。
- 本模块只做【词法层】校验(相对路径、无 ``..``、非空)。真正的 containment
  (resolve + inode / reparse / 流 / 大小写)在执行期的 ``ResolvedPathIdentity``
  (PR2)里做——这里只挡最廉价、最明确的越界形态。
- ``contract_version`` 用 ``Literal`` 钉死;未知版本天然触发校验失败 => 调用层
  fail-closed,而不是静默强转。
- ``failure_kind`` 是枚举,不是自由字符串:orchestrator 的 failover 在它上面分支,
  绝不解析自然语言。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 当前合同版本。新增破坏性字段时升它,旧版 server 见到未知版本即 fail-closed。
CONTRACT_VERSION = "1"

# writable_path 词法层的单条长度上限(资源边界,fail-closed)。
_MAX_WRITABLE_PATH_LEN = 1024


class FailureKind(str, Enum):
    """结构化失败原因。failover / 审计在它上面分支,绝不解析自由文本。"""

    timeout = "timeout"
    rate_limited = "rate_limited"
    auth = "auth"
    agent_unavailable = "agent_unavailable"
    crashed = "crashed"
    scope_violation = "scope_violation"
    check_failed = "check_failed"
    policy_denied = "policy_denied"
    invalid_contract = "invalid_contract"
    partial_rollback = "partial_rollback"


def _validate_writable_path(raw: str) -> str:
    """词法层校验单条 ``writable_path`` 并归一化分隔符。

    v1 只允许仓库根下的相对文件 / 目录前缀:

    - 非空;
    - 不是绝对路径(POSIX ``/...`` / Windows 盘符 ``C:...`` / UNC ``//...``);
    - 不含 ``..`` 分量。

    更深的检查(ADS ``:`` 流、保留设备名、reparse、硬链接、大小写)留给 PR2 的
    ``ResolvedPathIdentity`` 在执行期做——本函数只挡最廉价、最明确的越界形态,
    不假装自己是 containment 边界。
    """
    if not isinstance(raw, str):
        raise ValueError("writable_path 必须是字符串")
    text = raw.strip()
    if not text:
        raise ValueError("writable_path 不能为空")
    if len(text) > _MAX_WRITABLE_PATH_LEN:
        raise ValueError(f"writable_path 过长(>{_MAX_WRITABLE_PATH_LEN}):{raw!r}")
    normalized = text.replace("\\", "/")
    # 绝对路径:POSIX '/...'、UNC '//...'、Windows 盘符 'C:...'。
    if normalized.startswith("/"):
        raise ValueError(f"writable_path 必须是相对路径,收到绝对/UNC 路径:{raw!r}")
    # 拒绝任何 ':'——既挡 Windows 盘符(C:...),也挡 NTFS ADS 流(foo:bar)。
    # 词法层一律拒;更深的流枚举留给 PR2 的 ResolvedPathIdentity。
    if ":" in normalized:
        raise ValueError(f"writable_path 不允许 ':'(盘符或 NTFS ADS 流):{raw!r}")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"writable_path 不允许 '..' 分量:{raw!r}")
    if not parts:
        raise ValueError(f"writable_path 归一化后为空:{raw!r}")
    return "/".join(parts)


class RequestedScope(BaseModel):
    """调用方【申请】的权限——注意:是申请,不是授权。

    最终生效范围由本地策略计算(PR5)::

        effective = requested ∩ 父继承scope ∩ local_user_policy ∩ engine_limits
    """

    model_config = ConfigDict(extra="forbid")

    writable_paths: list[str] = Field(default_factory=list, max_length=256)
    network: Literal["deny", "request"] = "deny"
    check_ids: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("writable_paths")
    @classmethod
    def _check_writable_paths(cls, value: list[str]) -> list[str]:
        return [_validate_writable_path(p) for p in value]


class HandoffRequest(BaseModel):
    """一次结构化委派的输入合同。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1"]
    goal: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(default_factory=list)
    # 【必填、无默认】:漏填即校验失败 => 调用层 fail-closed,
    # 绝不回退到引擎宽松默认。空 RequestedScope 合法,但代表"无可写路径"=只读。
    requested_scope: RequestedScope
    timeout_seconds: int = Field(default=300, ge=10, le=1800)
    max_output_chars: int = Field(default=4000, ge=500, le=50000)
    # 选 agent 由工具名(codex_handoff / claude_handoff)决定;此处只控制是否允许
    # 在"证明无副作用"后回退到另一个 agent(PR7)。
    allow_fallback: bool = False


class SideEffectStatus(str, Enum):
    """单一副作用类别的状态。"""

    none = "none"
    detected_and_reverted = "detected_and_reverted"
    detected_but_not_reverted = "detected_but_not_reverted"
    unknown = "unknown"
    unsupported = "unsupported"


class SideEffects(BaseModel):
    """按类别的副作用向量。

    一次 handoff 可【同时】是多种状态(例如 ``worktree_files`` 已回滚、而
    ``network`` 未知),所以这是 per-class 向量,不是单一标量。默认一律保守:
    无法证明的类别标 ``unknown``;无法强制的(网络)标 ``unsupported``。
    """

    model_config = ConfigDict(extra="forbid")

    worktree_files: SideEffectStatus = SideEffectStatus.unknown
    outside_project_files: SideEffectStatus = SideEffectStatus.unknown
    git_index: SideEffectStatus = SideEffectStatus.unknown
    git_refs: SideEffectStatus = SideEffectStatus.unknown
    processes: SideEffectStatus = SideEffectStatus.unknown
    network: SideEffectStatus = SideEffectStatus.unsupported
    external_services: SideEffectStatus = SideEffectStatus.unknown


class CheckResult(BaseModel):
    """一条验收检查(check_id 映射到本地命令数组,绝不经 shell)的结果。"""

    model_config = ConfigDict(extra="forbid")

    check_id: str
    exit_code: int | None = None
    passed: bool = False
    output_summary: str = ""
    # 校验输入的可信分级:authoritative=用户预注册且未被 agent 改(可当门禁);
    # candidate=agent 新增/改的测试(仅补充证据);tainted=命令/配置/关键依赖被
    # agent 改(不可当门禁)。默认保守取 tainted。
    evidence_class: Literal["authoritative", "candidate", "tainted"] = "tainted"
    definition_hash: str | None = None
    touched_inputs: list[str] = Field(default_factory=list)


class HandoffResult(BaseModel):
    """一次结构化委派的输出合同(版本化、结构化)。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1"]
    handoff_id: str
    status: Literal[
        "completed",
        "failed",
        "approval_required",
        "policy_denied",
        "scope_violation",
    ]
    agent_used: Literal["claude", "codex"] | None = None
    route_reason: str = ""
    summary: str = ""
    verified_files_changed: list[str] = Field(default_factory=list)
    scope_violations: list[str] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    failure_kind: FailureKind | None = None
    side_effects: SideEffects = Field(default_factory=SideEffects)
    duration_seconds: float = 0.0
    token_usage: dict | None = None
    # verified 只在"静默后、外部存证、原始字节"可证时给;否则 best_effort / unknown。
    evidence_level: Literal["verified", "best_effort", "unknown"] = "unknown"


def fail_closed_result(
    handoff_id: str,
    *,
    failure_kind: FailureKind,
    reason: str,
    status: Literal["failed", "policy_denied"] = "policy_denied",
) -> HandoffResult:
    """构造一个 fail-closed 的结果:不执行、不授权、说明原因。

    用于策略拒绝、未知版本、非法合同等所有"宁可不做"的路径——保证它们返回
    一个结构化、可机读的拒绝,而不是异常或一个看起来成功的自由文本。
    """
    return HandoffResult(
        contract_version=CONTRACT_VERSION,
        handoff_id=handoff_id,
        status=status,
        route_reason=reason,
        summary=reason,
        failure_kind=failure_kind,
        evidence_level="unknown",
    )
