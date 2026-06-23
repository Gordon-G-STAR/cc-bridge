# 配置参考 / Configuration

README 只放最常用的,完整参考在这里。

## 工具

| 工具 | 装在 | 参数 |
| --- | --- | --- |
| `codex_execute` | Claude | `task`、`project_dir`(绝对路径,必填)、`continue_session`(续接上次会话)、`dry_run`(预演,只分析不改文件) |
| `codex_status` | Claude | 无 |
| `claude_analyze` | Codex | `task`、`project_dir`(绝对路径,必填)、`continue_session`、`dry_run` |
| `claude_status` | Codex | 无 |

`project_dir` 必须是存在的绝对路径,缺失 / 相对路径 / 不存在都会被拒绝。

## 命令

```bash
cc-bridge status     # 检测两端是否就绪
cc-bridge selftest   # 启动自检:拉起 <launcher> --mcp-server 做一次 MCP 握手
cc-bridge test       # 实际各调一次,验证双向连通
cc-bridge install    # 写配置(--no-test / --force 可选)
cc-bridge uninstall  # 移除配置
cc-bridge version
```

## 环境变量

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `CC_BRIDGE_TIMEOUT` | `300` | 单次调用超时(秒) |
| `CC_BRIDGE_MAX_OUTPUT` | `4000` | 回传摘要最大字符数 |
| `CC_BRIDGE_CODEX_SANDBOX` | `workspace-write` | Codex 沙箱(`read-only` / `workspace-write` / `danger-full-access`) |
| `CC_BRIDGE_CLAUDE_PERMISSION` | `bypassPermissions` | Claude 非交互权限模式 |
| `CC_BRIDGE_ALLOWED_ROOTS` | 未设 | 允许的工作区根(`os.pathsep` 分隔的绝对路径);设了之后越界的 `project_dir` 一律拒绝 |
| `CC_BRIDGE_INJECT_CONTEXT` | 开 | 设为 `0/false/no` 时不把仓库关键文件正文拼进 prompt(防间接注入) |
| `CC_BRIDGE_AUDIT_LOG` | 未设 | 设为文件路径时,每次调用追加一条 JSONL 审计记录 |
| `CC_BRIDGE_DEBUG` | 未设 | 非空时把各阶段进度打到 stderr(进宿主 MCP 日志) |
| `CC_BRIDGE_CODEX_MODEL` / `CC_BRIDGE_CLAUDE_MODEL` | 未设 | 指定模型 |
| `CODEX_HOME` | `~/.codex` | Codex 配置目录 |

## v0.2 委派策略 / 重授权(`*_handoff` + legacy 同一地板)

结构化委派 `codex_handoff` / `claude_handoff` 把 `requested_scope` 当**申请**,每次都重新计算
生效权限:`effective = requested ∩ 父链 ∩ 本地策略 ∩ 引擎上限`。下面这些变量是**唯一的真授权来源**,
只从宿主环境读——仓库内容 / README 改不动它们。旧的 `codex_execute` / `claude_analyze` 走**同一地板**
(关闭开关 / 链深上限 / 引擎钳制都一致),没有绕过策略的满权后门。

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `CC_BRIDGE_POLICY_WRITABLE_PATHS` | 未设(=整个项目根) | `os.pathsep` 分隔的相对路径,限定**可授予写**的子树;申请越出即被收窄。分量级匹配:`src/auth` 不会命中 `src/auth_secrets` |
| `CC_BRIDGE_POLICY_READONLY` | 关 | 置真 => 一律不授予写(覆盖 `WRITABLE_PATHS`);申请写入降级为只读 |
| `CC_BRIDGE_POLICY_ALLOW_NETWORK` | 关 | 置真才会把 `requested_scope.network=request` 兑现 |
| `CC_BRIDGE_POLICY_MAX_DEPTH` | `3` | 跨 agent 链路最大深度;`depth ≥ max` 一律拒绝(防 A→B→A→… 无界再入) |
| `CC_BRIDGE_POLICY_REQUIRE_APPROVAL` | 关 | 置真 => 授予写入前需人工审批;headless(无审批者)下返回 `approval_required` 并 fail-closed,不执行 |
| `CC_BRIDGE_LEGACY_TOOLS` | 开 | 设为 `0/false/no` 时关闭 `codex_execute` / `claude_analyze`,只保留结构化 `*_handoff` |

链路 `depth` 与已授权 scope 通过子进程环境变量 `CC_BRIDGE_CHAIN_DEPTH` / `CC_BRIDGE_CHAIN_SCOPE`
**自动下传**(非 LLM 通道),用于跨进程再入时的"子只能收窄"与链深计数——**无需手动设置**。

## 配置文件位置

- Claude 桌面版 `claude_desktop_config.json`:Windows `%APPDATA%\Claude\`,macOS `~/Library/Application Support/Claude/`,Linux `~/.config/Claude/`
- Codex `~/.codex/config.toml`(可被 `CODEX_HOME` 覆盖)
