# Recipes

几种常见用法。`project_dir` 都要传绝对路径。

## 让 Codex 干活(在 Claude 里)
> 把 `src/limiter.py` 的限流器按规格实现,然后让 Codex 在 `C:/proj` 跑测试、有失败就修。

Claude 会调 `codex_execute(task, project_dir="C:/proj")`,Codex 在该目录改文件、跑测试。

## 让 Claude 评审(在 Codex 里)
> 用 claude_analyze 看一下 `C:/proj` 的 worker.py 有没有竞态。

## 接着同一个会话多轮
传 `continue_session=True`,让对方记得这个项目上一轮的上下文:
> 第一次:`codex_execute("搭好测试脚手架", "C:/proj")`
> 之后:`codex_execute("再补上边界输入的用例", "C:/proj", continue_session=True)`

## 不信任的代码 → 只读 + 限定目录
```
CC_BRIDGE_CODEX_SANDBOX=read-only          # Codex 能读能跑,不能写
CC_BRIDGE_ALLOWED_ROOTS=C:\Users\me\safe   # project_dir 越界一律拒绝
```

## 不把仓库文件塞进 prompt
```
CC_BRIDGE_INJECT_CONTEXT=0   # 不再把 README/配置正文拼进发给对方的 prompt
```

## 想看过程 / 留审计
```
CC_BRIDGE_DEBUG=1                      # 各阶段进度打到 stderr(进宿主 MCP 日志)
CC_BRIDGE_AUDIT_LOG=C:\logs\cc.jsonl   # 每次调用追加一条审计记录
```

完整变量见 [`CONFIG.md`](CONFIG.md)。
