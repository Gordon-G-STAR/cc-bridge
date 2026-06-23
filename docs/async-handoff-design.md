# 异步 Handoff 设计 — detached runner(方案 B)

**状态:已实现(2026-06-23,detached runner / 方案 B;见 PR-a/b/c)。** 解决「同步 MCP 长任务撞客户端超时 `-32001`」这一 v0.2 根本短板,且 server/宿主中途死掉也能把 handoff 跑完(状态落盘 + PID 存活检测标 `interrupted`)。仍是 MCP SDK v2 Tasks 之前的【临时方案】——SDK v2 出来后改用协议级 task。

## 1. 根因

同步 `codex_handoff` 流程:`authorize`(快)→ baseline →【`lock + run_codex` 阻塞等子进程,可能几分钟】→ evidence → result。MCP 客户端在 `run_codex` 阻塞期间等不到工具响应 → `-32001`。**根因是「一次 MCP 工具调用 = 一次完整长执行」这个形状**,要把执行从调用生命周期里解耦。

## 2. 方案 B:把执行搬到 detached runner 进程

- `*_handoff_async`(MCP 工具,server 进程):`authorize` 早拒越权 → 写任务 spec → **spawn 一个 detached runner 进程** → 立即返回 `handoff_id`。
- **runner**(独立进程,新入口):读 spec → **重新 authorize(权威门禁)** → baseline → lock → exec → evidence → 写 `result.json`。独立于 server,宿主关掉也跑完。
- `*_handoff_status(id)` / `*_handoff_result(id)`(MCP 工具):读状态/结果文件 + 检测 runner 存活。

与方案 A(server 进程内 asyncio task)的区别:A 简单但宿主一关就中断;B 用独立进程换来「宿主死也跑完」的健壮性,代价是 detached spawn 的跨平台复杂度。

## 3. detach 机制(跨平台,最大技术点)

runner 必须不随 server/宿主死而死:
- **Windows**:`creationflags = DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB | CREATE_NEW_PROCESS_GROUP`。`CREATE_BREAKAWAY_FROM_JOB` 是关键 —— 宿主(Claude Desktop / Codex)可能把 MCP server 放进一个 kill-on-close job,不 breakaway 的话 server 一死 job 关闭会连带杀 runner。stdin/stdout/stderr 重定向到 `DEVNULL`/日志文件(不继承 MCP stdio 管道)。
- **POSIX**:`start_new_session=True`(setsid,脱离 server 的进程组/会话);std fds → `DEVNULL`/日志。
- 注:现有 `jobobject.kill_on_close_job()` 是 **per-exec**(只在 `executor._run` 内包裹 codex/claude 子进程),不是 server 级,所以 runner 内部跑 exec 照常用它来做「进程树静默屏障」,不受影响。

## 4. 状态 / 结果存储(文件,跨进程)

`stable_app_dir()/handoffs/<id>/`:
- `request.json` — spec:`{handoff_id, request(序列化), cwd, agent, caller, created_at}`。async 工具写,runner 读。
- `runner.pid` — runner 进程 PID(+ 启动时间戳,防 PID 复用误判)。
- `status.json` — `{state, updated_at, note}`;`state ∈ {pending, running, completed, failed, scope_violation, policy_denied, interrupted}`。
- `result.json` — 完整 `HandoffResult`(终态时写)。

POSIX 上目录与文件收紧到 `0700/0600`(照搬 `parser._save_full_output` / evidence 的私有目录做法);有界清理(完成的 handoff 目录超过 N 个删最旧,照搬 `parser._prune_saved_outputs`)。

## 5. 一条强制路径(不绕过 policy)

spec **只传 `request + cwd + caller + 链路 env**,绝不传授权结论。runner **自己重新 `authorize`**(`policy.decide_scope`)—— policy 仍是唯一门禁。async 工具里的 `authorize` 只是「早拒优化」(免得越权请求还白 spawn 一个 runner),权威授权在 runner。

## 6. 锁

runner 持 `async_project_lock(cwd)` 跨自己的 exec 全程;同项目已有 runner 在跑 → 新 runner `LockBusy` → 写 `failed(project_busy)`。锁是跨进程 OS 文件锁,runner 进程持 fd,崩溃由 OS 释放。

## 7. 存活检测 / interrupted

`*_handoff_status` 读 `status.json`;若 `state==running` 但 `runner.pid` 对应进程不存在(或 PID 被复用成别的进程,用启动时间戳核对)→ 判定 runner 已死 → 状态报 `interrupted`(**绝不假装 completed**)。

## 8. API(两方向对称)

| 工具 | 入参 | 返回 |
| --- | --- | --- |
| `codex_handoff_async` | `request, project_dir, continue_session` | `{handoff_id, state}`(authorize 拒→立即终态;授权→`running`) |
| `codex_handoff_status` | `handoff_id` | `{state, note, updated_at}` |
| `codex_handoff_result` | `handoff_id` | 完整 `HandoffResult`(终态)或 `{state: running}` |
| `codex_handoff_cancel`(可选) | `handoff_id` | 杀 runner 进程树 + 标 `interrupted` |

`claude_handoff_async/status/result` 对称。

## 9. 并发 / 清理

- 全局并发上限(同时 running 的 runner 数,env 可配,默认如 4)—— 超限时 async 工具拒绝(`project_busy`/`too_many`)而非无限 spawn。
- `handoffs/` 目录有界清理:终态目录超过 N 个删最旧。

## 10. PR 拆分(每个可测、可审、可发)

- **PR-a:状态存储 + runner 核心**。`handoff_store.py`(读写 `handoffs/<id>/` 的 spec/status/result/pid)+ `handoff_runner.py`(读 spec → authorize → baseline → lock → exec → evidence → execution_to_handoff → 写 result;全程更新 status)。用 stub executor 测 runner 跑通、写终态。**不含 detached spawn / MCP 工具**(runner 先能被直接 `python -m ... <id>` 跑起来)。
- **PR-b:detached spawn + MCP 工具**。`*_handoff_async/_status/_result` 三工具 × 两方向;跨平台 detached spawn(breakaway/setsid + DEVNULL);async 工具 authorize 早拒 + 写 spec + spawn + 返回 id。测试:authorize 拒→立即终态、spawn 后 status=running、result 读取。
- **PR-c:健壮性 + 文档**。PID 存活检测 → interrupted;并发上限;有界清理;`cancel`;README / CONFIG / roadmap 写明(含「临时方案,SDK v2 Tasks 出来替换」)。

## 11. 坑 / 诚实标注

- **临时方案**:MCP SDK v2 的 Tasks 扩展出来后,改用协议级 task(客户端原生 poll task),比自管 detached 进程干净 —— README/roadmap 要写明这点。
- detached spawn 跨平台差异大(尤其 Windows job breakaway),**必须在 Windows 与 POSIX 都实测**(CI 两边跑)。
- spec/result 落盘在用户私有 `stable_app_dir`,权限收紧。
- runner 崩溃 → `interrupted`,不假装完成;回滚仍是 PR4 顺延的「检测不补」,异步不改变这点。
- 链路深度经 spec 的 env 传给 runner,再由 runner 给 exec 子进程下传,递归上限(`CC_BRIDGE_POLICY_MAX_DEPTH`)照旧生效。
