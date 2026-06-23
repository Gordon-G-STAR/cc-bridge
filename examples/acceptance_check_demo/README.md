# acceptance_check_demo — 用 checklist-run 做项目验收

演示 `cc-bridge checklist-run` 怎么把一份验收清单逐项跑成结构化报告 —— 用于**合同功能验收 / Demo 前检查 / PPT 与代码一致性核查**。

## 结构
- `sample_project/` —— 被验收的示例项目(`calc.py` + `test_calc.py`;故意**没有** `CHANGELOG.md`,也故意没给 `divide` 做除零处理)。
- `checklist.md` —— 验收清单。
- `report.example.md` —— 跑出来的报告样例。

## 怎么跑
```bash
cc-bridge checklist-run \
  --checklist examples/acceptance_check_demo/checklist.md \
  --project-dir <绝对路径>/examples/acceptance_check_demo/sample_project
```
每个 `- [cmd] 命令` 在 `project_dir` 里执行,退出码 0=通过。本示例的预期:核心功能自检**通过**、`calc.py` 存在**通过**、`CHANGELOG.md` **缺失→未完成**,所以整体退出码 **1**(有未完成项),报告把它们分到「✅ 已完成 / ❌ 未完成」。

## 两类验收项
| 写法 | 谁来判 | 适合 |
| --- | --- | --- |
| `- [cmd] 命令` | cc-bridge 直接跑命令(退出码 0=过) | 可机检:测试通过?关键文件/交付物在?`/health` 200?接口可访问? |
| `- 自然语言描述` | claude/codex 只读判断(`--agent claude\|codex`) | 需判断:错误处理是否完善?PPT 描述的功能代码里有没有?异常路径覆盖了吗? |

**语义项示例**(加进 checklist 后用 `--agent claude` 跑,输出 done/partial/missing + 风险 + 涉及文件,取决于 agent):
```
- divide() 是否对除零等边界做了错误处理?
- README/PPT 承诺的功能是否都在代码里实现了?
```

## 典型验收场景(配合 `git_mode="safe"` / 异步 handoff)
- **合同功能差距**:cmd 项查交付物 + 语义项核对需求清单逐条实现没。
- **Demo 前健康检查**:cmd 项跑「服务启动 / `/health` / demo_data / 核心 API」。
- **PPT 与代码一致性**:语义项逐条核对 PPT 功能点是否真在代码里。
- 发现缺口后,再用 `codex_execute(..., git_mode="safe")` 让 Codex 补齐(改动隔离到临时分支),长任务用 `codex_handoff_async`。
