import json
import os
from pathlib import Path

from cc_bridge.bridge import policy, scope
from cc_bridge.bridge.contracts import HandoffRequest, RequestedScope


HERE = Path(__file__).resolve().parent
SAMPLE = HERE / "sample_project"


class DemoApprovalProvider:
    def approve_writes(
        self, *, handoff_id: str, project_root: str, writable_paths: tuple[str, ...]
    ) -> bool:
        # 这个 demo 不要求人工审批；保留 provider 是为了适配真实 decide_scope 签名。
        return True


def reset_demo_env() -> None:
    # 本 demo 自己制造根调用环境，避免父进程残留的链路或只读策略影响结果。
    for name in (
        policy.READONLY_ENV,
        policy.ALLOW_NETWORK_ENV,
        policy.REQUIRE_APPROVAL_ENV,
        policy.MAX_DEPTH_ENV,
        policy.CHAIN_DEPTH_ENV,
        policy.CHAIN_SCOPE_ENV,
    ):
        os.environ.pop(name, None)
    os.environ[policy.WRITABLE_PATHS_ENV] = "tests"


def decide(requested: RequestedScope, chain: policy.ChainContext) -> policy.ScopeDecision:
    return policy.decide_scope(
        requested,
        policy=policy.LocalPolicy.from_env(),
        chain=chain,
        handoff_id="contracted-handoff-demo",
        project_root=str(SAMPLE),
        provider=DemoApprovalProvider(),
    )


def main() -> None:
    reset_demo_env()

    # 1) 加载并校验合同：requested_scope 必填，路径词法由 HandoffRequest 校验。
    req = HandoffRequest.model_validate(
        json.loads((HERE / "handoff_request.json").read_text(encoding="utf-8"))
    )

    # 2) 本地策略：只允许写 tests/，env 是唯一真授权来源。
    chain = policy.ChainContext.from_env()

    # 场景 A：申请写 tests/ -> 授权。
    a = decide(req.requested_scope, chain)
    print("A 申请 tests/ ->", "授权" if a.write_granted else "拒绝", "| effective:", a.effective_writable)

    # 场景 B：同策略下申请写 src/ -> 被收窄为只读。
    b = decide(RequestedScope(writable_paths=["src"]), chain)
    print("B 申请 src/  ->", "授权" if b.write_granted else "拒绝(越界)", "| effective:", b.effective_writable)

    # 场景 C：containment 路径解析，越界路径被挡下。
    c1 = scope.resolve_within_root("../secrets.txt", str(SAMPLE))
    c2 = scope.resolve_within_root("tests/test_app.py", str(SAMPLE))
    print(
        "C ../secrets.txt within_root:",
        c1.within_root,
        "| tests/test_app.py within_root:",
        c2.within_root,
    )

    # 断言：申请不等于授权，越界被拒。
    assert a.write_granted and a.effective_writable == ("tests",)
    assert not b.write_granted
    assert (not c1.within_root) and c2.within_root
    print("\nOK: 申请只写 tests/ 被授权;申请 src/ 被拒;越界路径被 containment 挡下。")


if __name__ == "__main__":
    main()
