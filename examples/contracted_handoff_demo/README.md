# Contracted Handoff Demo

这个最小 demo 演示 cc-bridge v0.2 的三个点：

- 结构化委派：`handoff_request.json` 按 `HandoffRequest` 校验，`requested_scope` 必填。
- 本地策略重授权：`requested_scope` 只是申请，实际可写范围来自 `CC_BRIDGE_POLICY_WRITABLE_PATHS`。
- 路径 containment：`scope.resolve_within_root()` 会识别越出项目根的路径。

它不依赖 handoff 的同步 MCP，也不会调用真实 Codex/Claude，因此不会触发同步 MCP 长任务的 `-32001` 超时问题。`demo.py` 直接调用 `cc_bridge.bridge.contracts`、`cc_bridge.bridge.policy`、`cc_bridge.bridge.scope`。

## 怎么跑

```powershell
$env:PYTHONPATH="C:/Users/31173/Desktop/oo/.claude/worktrees/festive-heyrovsky-51b7de/src"
C:/Users/31173/Desktop/oo/.venv/Scripts/python.exe examples/contracted_handoff_demo/demo.py
```

## 预期输出

```text
A 申请 tests/ -> 授权 | effective: ('tests',)
B 申请 src/  -> 拒绝(越界) | effective: ()
C ../secrets.txt within_root: False | tests/test_app.py within_root: True

OK: 申请只写 tests/ 被授权;申请 src/ 被拒;越界路径被 containment 挡下。
```
