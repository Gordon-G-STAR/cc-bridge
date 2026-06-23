# COOKBOOK — 真实场景

cc-bridge 的常用姿势:**Claude 规划/审查、Codex 实现/验证**。下面每个场景给出在哪个 agent 里怎么说、背后调哪个工具、关键参数。`project_dir` 一律传**绝对路径**。

> 想要"改动可控、可回退"的,几乎所有场景都建议加 `git_mode="safe"`(要求干净的 git 仓库):改动会被隔离到临时分支 `cc-bridge/<时间戳>`,跑完提交到该分支、再切回原分支,**原分支不受影响**;报告末尾会给出查看 / 对比 / 合并 / 丢弃该分支的命令。

---

## 1. 重构一个模块,并保持测试通过

在 **Claude** 里(你和 Claude 先把方案定清楚,再把落地交给 Codex):

> 帮我把 `src/limiter.py` 的限流器从"固定窗口"改成"滑动窗口",保持对外接口不变。方案定了之后,让 Codex 在 `C:/proj` 实现,并跑 `pytest tests/test_limiter.py`,有失败就修到全绿。用 `git_mode="safe"`。

Claude 会先给设计,再调:

```
codex_execute(
  task="把 src/limiter.py 改成滑动窗口限流,接口不变;跑 pytest tests/test_limiter.py,失败就修到全绿",
  project_dir="C:/proj",
  git_mode="safe",
)
```

返回的报告里有改动文件、测试结论和临时分支导航。你 `git diff <原分支>..cc-bridge/...` 复核,满意就 `git merge`,不满意就 `git branch -D`。

## 2. 给一个旧项目补单元测试

在 **Claude** 里:

> `C:/legacy` 这个项目几乎没测试。让 Codex 先给 `app/payments.py` 补一套单测(覆盖正常 + 边界 + 异常路径),用项目现有的测试框架,跑通。

```
codex_execute(
  task="为 app/payments.py 写单元测试,覆盖正常/边界/异常,沿用项目现有测试框架并跑通",
  project_dir="C:/legacy",
  git_mode="safe",
)
```

要分批推进(先脚手架、再补用例),第二次起带 `continue_session=True`,Codex 记得上一轮的上下文:

```
codex_execute(task="再补上并发与超时的用例", project_dir="C:/legacy", continue_session=True, git_mode="safe")
```

## 3. 按一个 issue 实现功能

在 **Claude** 里(先让 Claude 拆规格,再让 Codex 实现):

> issue #142:导出 CSV 时要支持自定义分隔符和 UTF-8 BOM。先帮我拆成验收标准,然后让 Codex 在 `C:/proj` 实现并补测试。

Claude 把 issue 拆成清单后,把"带验收标准的规格"交给 Codex:

```
codex_execute(
  task="实现 CSV 导出的自定义分隔符 + 可选 UTF-8 BOM。验收:1) delimiter 参数生效;2) bom=True 时输出带 BOM;3) 默认行为不变;4) 新增测试覆盖这三点并通过",
  project_dir="C:/proj",
  git_mode="safe",
)
```

## 4. Codex 改完,让 Claude 做安全/质量审查

在 **Codex** 里(Codex 刚实现完,反手让 Claude 审一遍):

> 用 claude_analyze 审一下 `C:/proj` 这次的改动有没有注入 / 路径穿越 / 资源泄漏 / 并发问题,只指出风险和修法,先别动手。

```
claude_analyze(
  task="审查当前工作区改动的安全与质量风险(注入/路径穿越/资源泄漏/并发竞态),给出风险点与修法,先不要改代码",
  project_dir="C:/proj",
  dry_run=True,
)
```

`dry_run=True` 让 Claude 走 plan 模式:只分析、不落盘。看完结论再决定要不要让它动手(去掉 `dry_run`)。

## 5. Claude 规划、Codex 执行(一来一回)

最常见的双 agent 流水线:

1. 在 **Claude** 里把"做什么、怎么验收"想清楚(必要时让 Claude 读代码定位)。
2. Claude 调 `codex_execute(..., git_mode="safe")` 把实现交给 Codex。
3. Codex 跑完回标准化报告(改动文件 / 下一步建议 / 耗时)。
4. 在 **Codex** 里用 `claude_analyze` 让 Claude 复核改动。
5. 你看临时分支的 diff,决定合并还是丢弃。

> 防呆:Claude 调 Codex、Codex 又回头调 Claude 这种递归,由 v0.2 本地策略兜底——默认 `CC_BRIDGE_POLICY_MAX_DEPTH=3`,跨 agent 链路超深一律拒绝,不会无限互调烧额度。

## 6. 不信任的代码 / 先看不动手

```
CC_BRIDGE_CODEX_SANDBOX=read-only        # Codex 能读能跑,不能写
CC_BRIDGE_ALLOWED_ROOTS=C:\Users\me\safe # project_dir 越界一律拒绝
```

或者单次调用传 `dry_run=True` 让对方只分析、不落盘。

## 7. 安装时一次设好保守默认

不想每次设环境变量,装的时候写进两端 MCP 配置(持久化):

```bash
cc-bridge install \
  --allowed-roots "C:/Users/me/work" \
  --codex-sandbox read-only \
  --audit-log "C:/Users/me/cc-bridge-audit.jsonl"
```

之后每次桌面应用拉起桥接 server 都自动带上这些配置。审计日志里每次调用一条 JSONL(方向、目录、任务摘要、成功与否、改动文件)。

## 8. 需要权限可控的结构化委派(v0.2)

当你想把"目标 + 验收标准 + 申请的权限范围"作为结构化合同交出去,用 `codex_handoff` / `claude_handoff`(而非 legacy 的 execute/analyze):

> 申请的 `requested_scope` 只是【申请】——最终生效权限由本地策略重算:`effective = requested ∩ 父链 ∩ 本地策略 ∩ 引擎上限`。申请超出策略会被收窄 / 降级为只读 / 拒绝;链深超限或需审批(headless)一律 fail-closed。

策略变量(只从宿主环境读,仓库内容改不动)见 [`CONFIG.md`](CONFIG.md) 的"v0.2 委派策略"一节。

---

更多变量与工具参数见 [`CONFIG.md`](CONFIG.md)。
