# WAL + 逐文件回滚设计 (PR4)

**状态:设计稿(2026-06-23)。** 目标:把现在的「检测不补」(`detected_but_not_reverted`)升级成真回滚 —— 越界改动不仅被**检测**,还被**撤销**,让"权限不可扩张"真正闭环。

## 1. 现状

`handoff_runner` / `execute_fallback`:`evidence.baseline`(存路径→sha256)→ exec → `evidence.gather`(diff + classify)→ 有越界(`scope_violations`)就标 `worktree_files=detected_but_not_reverted`。**只哈希、不存内容,所以无法回滚。**

## 2. 范围(MVP)

- **只回滚越界改动**(`scope_violations`):被越界改的文件恢复 baseline 内容、越界新建的文件删除。
- **授权范围内改动(`verified`)保留** —— 那是 agent 的合法成果。
- 失败/拒绝场景的"全回滚"留后续。

## 3. 方案:content-WAL(存内容,不依赖 git)

不用 git 回滚:untracked 文件、index 状态、stash 的 untracked 处理都易错。content-WAL 确定、可控:

- **baseline**:对 `snapshot.list_snapshot_targets` 的文件存**内容**到 WAL(`stable_app_dir/wal/<handoff_id>/blobs/<sha256>`,按 hash 去重)+ `manifest.json`(`{path: {sha, existed}}`)。**有界**:总大小/文件数超阈值 → 标该 handoff `unverifiable`、不承诺回滚(诚实,不假装)。
- **rollback(越界文件集)**:对每个越界 path 查 manifest 的 baseline:`existed=True` → 从 blob 恢复内容;`existed=False`(越界新建) → 删除该文件。

## 4. WAL 格式

`stable_app_dir/wal/<handoff_id>/`:
- `manifest.json`:`{state, baseline:{path:{sha,existed}}, to_revert:[...], reverted:[...]}`;`state ∈ {recording, ready, reverting, reverted, skipped_too_large}`。
- `blobs/<sha256>`:baseline 文件原始字节(去重)。

## 5. 原子性 + 崩溃恢复(WAL 的意义)

- 回滚整段包 `asyncio.shield` —— 取消(关窗/skip)不打断已开始的回滚。
- **逐文件推进**:每恢复/删一个文件,把它加进 `manifest.reverted` 并落盘 → 崩溃可续。
- **acquire 扫 WAL**:下次该项目拿 `project_lock` 时,扫 `wal/` 发现 `state=reverting` 且 `reverted ⊊ to_revert` 的残留 → **先续完回滚再放行**新 handoff(roadmap 的"acquire-scan,拒绝脏 handoff")。

## 6. 集成(不绕过现有保障)

`handoff_runner` / `execute_fallback`:`baseline` 之外加 `wal.record_baseline`;`gather` 后若有 `scope_violations` → `wal.rollback(scope_violations)` → `execution_to_handoff` 标 `detected_and_reverted`。一条强制路径、project_lock、policy 全不变。

## 7. PR 拆分

- **PR4a**:`wal.py` —— `record_baseline`(存内容+manifest+blobs,有界)、`rollback(paths)`(恢复/删,逐文件推进 manifest)、`pending_rollbacks()`(扫残留)。测试用真实文件:越界改→恢复内容、越界新建→删除、授权改动→不动、超界→skipped。
- **PR4b**:接进 `handoff_runner` / `execute_fallback`(scope_violation → rollback → `detected_and_reverted`),`SideEffectStatus` 相应更新。测试:端到端 handoff 越界 → 工作区被还原。
- **PR4c**:崩溃恢复(`project_lock` acquire 时 `pending_rollbacks` 续完)+ 文档(README/CONFIG/roadmap 把 PR4 标 done)+ roadmap「实现状态」更新。

## 8. 坑 / 诚实标注

- **大项目成本**:baseline 存内容有界;超界 → `unverifiable`,**不假装能回滚**。
- **只回滚越界**,保留授权改动(别误删 agent 合法成果)。
- **特殊文件**(符号链接/reparse/设备名):回滚只碰普通文件,其余标 `unverifiable`。
- **与 `git_mode="safe"` 区分**:safe 是 legacy 工具把改动隔离到临时分支(改前隔离);WAL 回滚针对 handoff 的 in-place 改动(改后撤销)。两者机制不同、不冲突。
- 回滚本身失败(IO 错)→ 诚实退回 `detected_but_not_reverted` + 列未能回滚的文件,绝不谎报已还原。
