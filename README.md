# cc-bridge

[![CI](https://github.com/Gordon-G-STAR/cc-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/Gordon-G-STAR/cc-bridge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

cc-bridge 把 Claude 和 Codex 接成一条本地双 agent 流水线:**Claude 规划、审查、定位问题,Codex 实现、跑测试、修复**。你 Claude Max 和 ChatGPT 都付了钱,cc-bridge 让它们各走自己的 CLI 协作,不用 API key。(默认分工如此,反过来也行——Claude 也能实现、Codex 也能审查。)

*(English below)*

## 装

```bash
pip install -e .
cc-bridge install      # 然后重启 Claude 和 Codex
```

也可以用打包好的图形安装器 `cc-bridge-installer`,装完同样重启两个 app。

## 用

在 Claude 里直接说,比如:「设计好这个改动,然后让 Codex 实现并跑测试修到通过」——Claude 规划、Codex 落地,背后会调 `codex_execute`。
在 Codex 里反过来:用 `claude_analyze` 让 Claude 评审改动、定位疑难 bug、做架构权衡。

- `codex_execute(task, project_dir)` — 让 Codex 在项目里干活(改文件、跑测试、修复)
- `claude_analyze(task, project_dir)` — 让 Claude 分析、评审、定位
- `project_dir` 要传**绝对路径**;两个工具都可带 `continue_session=True` 接着上一轮会话。
- 每次调用都回一份**标准化报告**:调用方向、项目、任务、改动文件、下一步建议、耗时。
- 想让改动可控:传 `git_mode="safe"`,改动会被隔离到临时分支、跑完提交并切回原分支,原分支不受影响。
- 需要结构化委派(申请权限范围 / 验收标准)时用 v0.2 的 `codex_handoff` / `claude_handoff`。

## 注意

默认对方能在那个目录里自动改文件、跑命令,**不会问你**。几道安全带:

- **隔离改动** → 调用时传 `git_mode="safe"`(要求干净的 git 仓库),改动只落在临时分支,原分支不动。
- **不信任的代码** → `CC_BRIDGE_CODEX_SANDBOX=read-only`,或 `CC_BRIDGE_ALLOWED_ROOTS` 限定能动的目录。
- **防递归互调** → v0.2 本地策略默认 `CC_BRIDGE_POLICY_MAX_DEPTH=3`,跨 agent 链路超深一律拒绝。
- **装的时候顺手设好** → `cc-bridge install --allowed-roots <路径> --codex-sandbox read-only --audit-log <文件>`。

配置和用法细节见 [`docs/CONFIG.md`](docs/CONFIG.md);真实场景示例见 [`docs/COOKBOOK.md`](docs/COOKBOOK.md)。

---

## English

cc-bridge wires Claude and Codex into a local two-agent pipeline: **Claude plans, reviews, and diagnoses; Codex implements, runs tests, and fixes.** You already pay for Claude Max and ChatGPT — cc-bridge lets them collaborate through their own CLIs, no API keys. (That's the default split; it works the other way too — Claude can implement, Codex can review.)

### Install

```bash
pip install -e .
cc-bridge install      # then restart Claude and Codex
```

Or run the packaged GUI installer `cc-bridge-installer`, then restart both apps.

### Use

In Claude: "design this change, then have Codex implement it and run the tests until they pass" — Claude plans, Codex lands it via `codex_execute`. In Codex: use `claude_analyze` to have Claude review changes, debug, or weigh architecture trade-offs.

- `codex_execute(task, project_dir)` — Codex does the work (edits files, runs tests, fixes)
- `claude_analyze(task, project_dir)` — Claude analyzes / reviews / diagnoses
- `project_dir` must be absolute; both tools take `continue_session=True` to resume the last session.
- Every call returns a **standardized report**: direction, project, task, files changed, next-step suggestion, duration.
- Want changes contained? Pass `git_mode="safe"` — work is isolated on a temp branch, committed there, then the original branch is restored untouched.
- For structured delegation (requested scope / acceptance criteria) use v0.2's `codex_handoff` / `claude_handoff`.

### Heads up

By default the other agent edits files and runs commands in that directory without asking. A few safety belts:

- **Isolate changes** → pass `git_mode="safe"` (needs a clean git repo); changes land only on a temp branch, original branch untouched.
- **Untrusted code** → `CC_BRIDGE_CODEX_SANDBOX=read-only`, or `CC_BRIDGE_ALLOWED_ROOTS` to fence the reachable dirs.
- **No recursion** → v0.2's local policy defaults to `CC_BRIDGE_POLICY_MAX_DEPTH=3`; over-deep cross-agent chains are refused.
- **Set it at install time** → `cc-bridge install --allowed-roots <path> --codex-sandbox read-only --audit-log <file>`.

Config & usage details: [`docs/CONFIG.md`](docs/CONFIG.md). Real-world recipes: [`docs/COOKBOOK.md`](docs/COOKBOOK.md).

## License

MIT
