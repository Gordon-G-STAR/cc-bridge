# cc-bridge

[![CI](https://github.com/Gordon-G-STAR/cc-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/Gordon-G-STAR/cc-bridge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

让 Claude 和 Codex 互相干活。你 Claude Max 和 ChatGPT 都付了钱,cc-bridge 让 Claude 把活丢给 Codex、反过来也行,走各自的 CLI,不用 API key。

*(English below)*

## 装

```bash
pip install -e .
cc-bridge install      # 然后重启 Claude 和 Codex
```

也可以用打包好的图形安装器 `cc-bridge-installer`,装完同样重启两个 app。

## 用

在 Claude 里直接说,比如:「重构这块,然后让 Codex 跑测试修一下」——它背后会调 `codex_execute`。
在 Codex 里反过来:用 `claude_analyze` 让 Claude 评审或定位问题。

- `codex_execute(task, project_dir)` — 让 Codex 在项目里干活(改文件、跑测试)
- `claude_analyze(task, project_dir)` — 让 Claude 分析、评审
- `project_dir` 要传**绝对路径**;`codex_execute` 可带 `continue_session=True` 接着上次的会话。

## 注意

默认对方能在那个目录里自动改文件、跑命令,**不会问你**。不信任的代码就把 `CC_BRIDGE_CODEX_SANDBOX` 设成 `read-only`,或用 `CC_BRIDGE_ALLOWED_ROOTS` 限定能动的目录。

配置和用法细节见 [`docs/CONFIG.md`](docs/CONFIG.md)。

---

## English

Let Claude and Codex do each other's work. You already pay for Claude Max and ChatGPT — cc-bridge lets Claude hand coding tasks to Codex and vice versa, through their own CLIs, no API keys.

### Install

```bash
pip install -e .
cc-bridge install      # then restart Claude and Codex
```

Or run the packaged GUI installer `cc-bridge-installer`, then restart both apps.

### Use

In Claude: "refactor this, then have Codex run the tests and fix them" — it calls `codex_execute`. In Codex: use `claude_analyze` to have Claude review or debug.

- `codex_execute(task, project_dir)` — Codex does the work (edits files, runs tests)
- `claude_analyze(task, project_dir)` — Claude analyzes / reviews
- `project_dir` must be absolute; `codex_execute` takes `continue_session=True` to resume the last session.

### Heads up

By default the other agent edits files and runs commands in that directory without asking. For untrusted code, set `CC_BRIDGE_CODEX_SANDBOX=read-only` or `CC_BRIDGE_ALLOWED_ROOTS`.

Config & usage details: [`docs/CONFIG.md`](docs/CONFIG.md).

## License

MIT
