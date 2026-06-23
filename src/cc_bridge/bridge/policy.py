"""本地策略重授权 —— v0.2 安全三脚架的第一脚。

每次 handoff(以及 legacy 自由文本工具)都【无条件】重新计算生效权限::

    effective = requested ∩ inherited(父链) ∩ local_user_policy ∩ engine_limits

不变量(见 ``docs/v0.2-roadmap.md``):

- ``requested`` 只是【申请】、永不授权;缺省 = deny。
- 子只能收窄:``effective ⊆ inherited``(父链通过 env 下传的已授权 scope)。
- 链路 ``depth`` 从 env 消费(父进程下传、**非 LLM 通道**):``depth ≥ max`` => deny,
  挡住无界再入(A→B→A→…)。
- token / 链路自述 ≠ 授权;唯一真授权来源是 ``local_user_policy ∩ engine_limits``,二者
  **只从宿主环境读**,绝不从仓库内容 / README / 合同自述里取——这样 README 改不了有效策略。
- 需要审批却处于 headless(无审批者)=> ``approval_required``,fail-closed(不执行)。
- 只有一条强制路径:legacy 自由文本工具与 handoff 共用同一 policy 地板,绝无满权后门。

本模块只做【授权决策】(纯函数、可对抗测);真正的越界【检测 + 补救】在执行后由
``evidence`` / ``scope`` 负责(containment 是检测 + 补救,不是阻断沙箱)。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from . import config
from .contracts import FailureKind, RequestedScope

# ---------------------------------------------------------------------------
# 环境变量名(全部只从宿主环境读;仓库内容无法触达)
# ---------------------------------------------------------------------------

# 本地策略(local_user_policy):
WRITABLE_PATHS_ENV = "CC_BRIDGE_POLICY_WRITABLE_PATHS"   # os.pathsep 分隔的相对路径,限定可授予写的子树
READONLY_ENV = "CC_BRIDGE_POLICY_READONLY"               # 置真 => 一律不授予写(覆盖 WRITABLE_PATHS)
ALLOW_NETWORK_ENV = "CC_BRIDGE_POLICY_ALLOW_NETWORK"     # 置真 => 允许把 network=request 兑现
MAX_DEPTH_ENV = "CC_BRIDGE_POLICY_MAX_DEPTH"             # 链路最大深度;depth >= max => deny
REQUIRE_APPROVAL_ENV = "CC_BRIDGE_POLICY_REQUIRE_APPROVAL"  # 置真 => 写入需审批(headless => approval_required)
LEGACY_TOOLS_ENV = "CC_BRIDGE_LEGACY_TOOLS"             # 置假 => 关闭 codex_execute / claude_analyze

# 链路上下文(由父进程通过子进程 env 下传,非 LLM 通道):
CHAIN_DEPTH_ENV = "CC_BRIDGE_CHAIN_DEPTH"
CHAIN_SCOPE_ENV = "CC_BRIDGE_CHAIN_SCOPE"  # JSON: {"writable_paths": [...], "network": "deny"|"request"}

# 缺省:单机单用户的 trusted 开发场景下,默认允许在项目根内写、链深上限 3、写入免审批。
# 想收紧的部署用上面的 CC_BRIDGE_POLICY_* 一行覆盖(README/仓库内容改不动它们)。
_DEFAULT_MAX_DEPTH = 3
_DEFAULT_WRITABLE: tuple[str, ...] = (".",)  # "." = 整个项目根都可作为可写候选

# Codex sandbox 由弱到强的秩;handoff/legacy 绝不使用 danger-full-access(只当上限钳制)。
_CODEX_RANK = {"read-only": 0, "workspace-write": 1, "danger-full-access": 2}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _norm_rel(path: str) -> str:
    """把一条相对路径归一成 ``a/b`` 形式(去分隔符差异、去 ``.`` 与空分量)。

    ``contracts`` 词法层已保证申请路径相对、无 ``..``、无 ``:``;这里只做最后的形态归一,
    好让分量级包含判断稳定。``"."`` / ``""`` 归一为 ``""``(代表项目根)。
    """
    parts = [c for c in str(path).replace("\\", "/").split("/") if c not in ("", ".")]
    return "/".join(parts)


def _within_rel(child: str, parent: str) -> bool:
    """``child``(相对)是否落在 ``parent``(相对)之内,**分量级**判断(非字符串前缀)。

    ``parent == ""``(项目根)包含一切。Windows 上分量大小写不敏感。
    ``src/auth`` 不会命中 ``src/auth_secrets``。
    """
    c = _norm_rel(child)
    p = _norm_rel(parent)
    if p == "":
        return True
    if c == "":
        return False
    cparts = c.split("/")
    pparts = p.split("/")
    if len(cparts) < len(pparts):
        return False
    if config.IS_WINDOWS:
        cparts = [x.casefold() for x in cparts]
        pparts = [x.casefold() for x in pparts]
    return cparts[: len(pparts)] == pparts


def _within_any(child: str, allowed: tuple[str, ...]) -> bool:
    return any(_within_rel(child, a) for a in allowed)


# ---------------------------------------------------------------------------
# 本地策略 / 链路上下文
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LocalPolicy:
    """本地用户策略 —— 唯一真授权来源(连同 engine_limits)。只从宿主环境读。"""

    writable_paths: tuple[str, ...]
    allow_network: bool
    max_chain_depth: int
    require_approval_for_writes: bool
    legacy_tools_enabled: bool

    @classmethod
    def from_env(cls) -> "LocalPolicy":
        if config.env_bool(READONLY_ENV, default=False):
            writable: tuple[str, ...] = ()
        else:
            raw = os.environ.get(WRITABLE_PATHS_ENV)
            if raw is None:
                writable = _DEFAULT_WRITABLE
            else:
                writable = tuple(
                    _norm_rel(p) or "."
                    for p in raw.split(os.pathsep)
                    if p.strip()
                )
        return cls(
            writable_paths=writable,
            allow_network=config.env_bool(ALLOW_NETWORK_ENV, default=False),
            max_chain_depth=_env_int(MAX_DEPTH_ENV, _DEFAULT_MAX_DEPTH),
            require_approval_for_writes=config.env_bool(
                REQUIRE_APPROVAL_ENV, default=False
            ),
            legacy_tools_enabled=config.env_bool(LEGACY_TOOLS_ENV, default=True),
        )


@dataclass(frozen=True)
class ChainContext:
    """跨进程链路上下文。父进程把 ``depth`` 与已授权 scope 写进子进程 env;本进程消费它。

    ``inherited_writable is None`` 表示【无父链】(根调用),不施加继承收窄;非 None(含空元组)
    表示有父链,子只能在其内收窄(空 => 子不得写)。
    """

    depth: int
    inherited_writable: tuple[str, ...] | None
    inherited_network: str | None

    @classmethod
    def from_env(cls) -> "ChainContext":
        depth = max(0, _env_int(CHAIN_DEPTH_ENV, 0))
        inherited_writable: tuple[str, ...] | None = None
        inherited_network: str | None = None
        raw = os.environ.get(CHAIN_SCOPE_ENV)
        if raw is not None:
            # env 一旦【存在】(哪怕空串 / 空白 / 坏 JSON / 缺字段),就代表父链声明过 scope =>
            # 必须 fail-closed 收窄(空元组 = 子不得写),绝不退回"无父链"。只有 env 完全缺失
            # (None,根调用)才不施加继承收窄。
            inherited_writable = ()
            inherited_network = "deny"
            if raw.strip():
                try:
                    data = json.loads(raw)
                except (ValueError, TypeError):
                    data = None
                if isinstance(data, dict):
                    wp = data.get("writable_paths")
                    if isinstance(wp, list):
                        inherited_writable = tuple(
                            _norm_rel(p) for p in wp if isinstance(p, str)
                        )
                    nw = data.get("network")
                    if nw in ("deny", "request", "granted"):
                        inherited_network = nw
        return cls(
            depth=depth,
            inherited_writable=inherited_writable,
            inherited_network=inherited_network,
        )

    def child_env(
        self, granted_writable: tuple[str, ...], granted_network: str
    ) -> dict[str, str]:
        """构造下传给子进程的链路 env:depth+1 + 本次实际授予的 scope。"""
        scope = json.dumps(
            {"writable_paths": list(granted_writable), "network": granted_network},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return {CHAIN_DEPTH_ENV: str(self.depth + 1), CHAIN_SCOPE_ENV: scope}


# ---------------------------------------------------------------------------
# 审批者(headless 默认拒绝)
# ---------------------------------------------------------------------------

@runtime_checkable
class ApprovalProvider(Protocol):
    """写入授权的审批接口。返回 True 才放行写入。"""

    def approve_writes(
        self, *, handoff_id: str, project_root: str, writable_paths: tuple[str, ...]
    ) -> bool: ...


class DenyAllApprovalProvider:
    """Headless 默认:无人类可审批 => 一律拒绝(=> approval_required,fail-closed)。"""

    def approve_writes(self, **_kwargs) -> bool:
        return False


_default_provider: ApprovalProvider = DenyAllApprovalProvider()


def get_approval_provider() -> ApprovalProvider:
    return _default_provider


def set_approval_provider(provider: ApprovalProvider) -> None:
    """注入审批者(GUI / 集成测试用)。默认是 headless 拒绝者。"""
    global _default_provider
    _default_provider = provider


# ---------------------------------------------------------------------------
# 引擎模式钳制(engine_limits 当上限)
# ---------------------------------------------------------------------------

def effective_codex_sandbox(write_granted: bool, cap: str | None) -> str:
    """据是否授予写 + 引擎上限(``CC_BRIDGE_CODEX_SANDBOX``)算出 Codex sandbox。

    上限只下钳、不上调:``cap=read-only`` 时即便授予写也强制只读。绝不返回 danger-full-access。
    """
    desired = "workspace-write" if write_granted else "read-only"
    cap_rank = _CODEX_RANK.get(cap or "", _CODEX_RANK["workspace-write"])
    if cap_rank < _CODEX_RANK[desired]:
        # 上限低于期望 => 钳到上限(但绝不超过 workspace-write)。
        for name, rank in _CODEX_RANK.items():
            if rank == min(cap_rank, _CODEX_RANK["workspace-write"]):
                return name
    return desired


def effective_claude_permission(write_granted: bool, cap: str | None) -> str:
    """据是否授予写 + 引擎上限(``CC_BRIDGE_CLAUDE_PERMISSION``)算出 Claude 权限模式。

    不授予写 => ``plan``(只读);上限本就是 ``plan`` => 强制只读。否则用上限配置的写模式
    (默认 ``bypassPermissions`` —— headless 下唯一能真正落盘的模式)。
    """
    if not write_granted or cap == "plan":
        return "plan"
    return cap or "bypassPermissions"


# ---------------------------------------------------------------------------
# 决策结果
# ---------------------------------------------------------------------------

class Decision(str, Enum):
    grant = "grant"
    deny = "deny"
    approval_required = "approval_required"


@dataclass(frozen=True)
class ScopeDecision:
    decision: Decision
    write_granted: bool
    effective_writable: tuple[str, ...]
    network_granted: bool
    depth: int
    reason: str
    failure_kind: FailureKind | None = None


def decide_scope(
    requested: RequestedScope,
    *,
    policy: LocalPolicy,
    chain: ChainContext,
    handoff_id: str,
    project_root: str,
    provider: ApprovalProvider,
) -> ScopeDecision:
    """重授权核心:把【申请】收窄成【生效】,或拒绝 / 要求审批。纯函数,可对抗测。"""
    depth = chain.depth
    if depth >= policy.max_chain_depth:
        return ScopeDecision(
            decision=Decision.deny,
            write_granted=False,
            effective_writable=(),
            network_granted=False,
            depth=depth,
            reason=(
                f"链路深度 {depth} 已达上限 {policy.max_chain_depth};"
                "为防无界再入(A→B→A→…),拒绝本次委派。"
            ),
            failure_kind=FailureKind.policy_denied,
        )

    requested_writable = tuple(dict.fromkeys(_norm_rel(p) for p in requested.writable_paths))
    after_policy = tuple(r for r in requested_writable if _within_any(r, policy.writable_paths))
    if chain.inherited_writable is None:
        effective = after_policy
    else:
        effective = tuple(r for r in after_policy if _within_any(r, chain.inherited_writable))

    network_granted = (
        requested.network == "request"
        and policy.allow_network
        and (chain.inherited_network in (None, "request", "granted"))
    )

    write_granted = bool(effective)
    notes: list[str] = []
    if requested_writable and not effective:
        notes.append("申请的写入不在本地策略 / 父链允许范围内,已降级为只读")
    if requested.network == "request" and not network_granted:
        notes.append("申请的网络未被本地策略 / 父链授予")

    if write_granted and policy.require_approval_for_writes:
        try:
            approved = bool(
                provider.approve_writes(
                    handoff_id=handoff_id,
                    project_root=project_root,
                    writable_paths=effective,
                )
            )
        except Exception:
            approved = False
        if not approved:
            return ScopeDecision(
                decision=Decision.approval_required,
                write_granted=False,
                effective_writable=effective,
                network_granted=False,
                depth=depth,
                reason=(
                    "本地策略要求对写入授权进行人工审批,但当前为 headless(无审批者);"
                    "已 fail-closed,未执行。"
                ),
                failure_kind=None,
            )

    reason = "授权:" + (f"可写 {list(effective)}" if write_granted else "只读")
    if network_granted:
        reason += ";网络=request"
    if notes:
        reason += ";" + ";".join(notes)
    return ScopeDecision(
        decision=Decision.grant,
        write_granted=write_granted,
        effective_writable=effective,
        network_granted=network_granted,
        depth=depth,
        reason=reason,
        failure_kind=None,
    )


# ---------------------------------------------------------------------------
# legacy 自由文本工具(codex_execute / claude_analyze)的同一 policy 地板
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LegacyDecision:
    """legacy 工具的执行决策:或拒绝(``refusal`` 非空),或给出引擎模式 + 链路 env。"""

    refusal: str | None
    engine_mode: str
    child_env: dict[str, str]
    write_granted: bool
    note: str


def decide_legacy(
    *,
    agent: str,
    policy: LocalPolicy,
    chain: ChainContext,
    codex_cap: str | None,
    claude_cap: str | None,
) -> LegacyDecision:
    """legacy 工具走与 handoff 同一条 policy 地板:关闭开关 / 链深上限 / 引擎钳制都一致。

    没有 ``requested_scope``,所以写入能力 = 本地策略是否允许写 ∩ 父链 ∩ 引擎上限。
    默认(单机、未收紧)仍是可写,保持 v0.1 行为;一旦收紧策略,legacy 同样被收窄——
    不存在绕过 policy 的满权后门。
    """
    if not policy.legacy_tools_enabled:
        return LegacyDecision(
            refusal=(
                "旧版自由文本工具已被本地策略禁用(CC_BRIDGE_LEGACY_TOOLS=0);"
                "请改用结构化的 *_handoff 工具(可申请、被重授权的 requested_scope)。"
            ),
            engine_mode="",
            child_env={},
            write_granted=False,
            note="disabled",
        )
    if chain.depth >= policy.max_chain_depth:
        return LegacyDecision(
            refusal=(
                f"链路深度 {chain.depth} 已达上限 {policy.max_chain_depth};"
                "为防无界再入,本次跨 agent 调用被拒绝。"
            ),
            engine_mode="",
            child_env={},
            write_granted=False,
            note="depth_exceeded",
        )

    inherited_allows = chain.inherited_writable is None or len(chain.inherited_writable) > 0
    want_write = bool(policy.writable_paths) and inherited_allows
    if agent == "codex":
        engine_mode = effective_codex_sandbox(want_write, codex_cap)
        write_granted = engine_mode != "read-only"
    else:
        engine_mode = effective_claude_permission(want_write, claude_cap)
        write_granted = engine_mode != "plan"

    granted_scope = policy.writable_paths if write_granted else ()
    child_env = chain.child_env(granted_scope, "deny")
    return LegacyDecision(
        refusal=None,
        engine_mode=engine_mode,
        child_env=child_env,
        write_granted=write_granted,
        note="可写" if write_granted else "只读",
    )


__all__ = [
    "ApprovalProvider",
    "ChainContext",
    "CHAIN_DEPTH_ENV",
    "CHAIN_SCOPE_ENV",
    "Decision",
    "DenyAllApprovalProvider",
    "LegacyDecision",
    "LocalPolicy",
    "ScopeDecision",
    "decide_legacy",
    "decide_scope",
    "effective_claude_permission",
    "effective_codex_sandbox",
    "get_approval_provider",
    "set_approval_provider",
]
