# 配置参考 / Configuration

README 只放最常用的,完整参考在这里。

## 工具

| 工具 | 装在 | 参数 |
| --- | --- | --- |
| `codex_execute` | Claude | `task`、`project_dir`(绝对路径,必填)、`continue_session`(可选,续接上次 Codex 会话) |
| `codex_status` | Claude | 无 |
| `claude_analyze` | Codex | `task`、`project_dir`(绝对路径,必填) |
| `claude_status` | Codex | 无 |

`project_dir` 必须是存在的绝对路径,缺失 / 相对路径 / 不存在都会被拒绝。

## 命令

```bash
cc-bridge status     # 检测两端是否就绪
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
| `CC_BRIDGE_CODEX_MODEL` / `CC_BRIDGE_CLAUDE_MODEL` | 未设 | 指定模型 |
| `CODEX_HOME` | `~/.codex` | Codex 配置目录 |

## 配置文件位置

- Claude 桌面版 `claude_desktop_config.json`:Windows `%APPDATA%\Claude\`,macOS `~/Library/Application Support/Claude/`,Linux `~/.config/Claude/`
- Codex `~/.codex/config.toml`(可被 `CODEX_HOME` 覆盖)
