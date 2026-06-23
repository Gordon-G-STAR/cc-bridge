# Windows 安全验证报告

roadmap 列了一批 Windows 专属的对抗形态(硬链接 / junction / ADS / 保留名 / skip-worktree / detached 孙进程等)。这份表把它们的**实机验证现状**讲清楚,避免「写进 roadmap = 已验证」的误解。

**怎么读这张表:** 大多数 Windows 专属测试用 `@pytest.mark.skipif(sys.platform != "win32")` 标记 —— 它们在 GitHub Actions 的 **`windows-latest` runner(py3.11/3.12/3.13)上真实执行**,所以 CI 绿就代表它们在真实 Windows 上跑过、过了。跨平台词法检测则在所有平台跑。

跑法:
```bash
pytest -m "not integration"           # 全平台;Windows 专属项在 windows-latest job 实跑
```

## 对抗形态 × 验证现状

| 风险 / 形态 | 测试 | 测法 | CI 实跑 | 结论 |
| --- | --- | --- | --- | --- |
| 硬链接别名 | `test_scope.py::test_hardlink_detected_on_windows`、`::test_directory_not_flagged_hardlink_aliased` | `os.link` 真实 NTFS 硬链接 + 目录不误报回归 | ✅ windows-latest | 已验证:别名文件标 `hardlink_aliased`;目录(`st_nlink>=2`)不误报 |
| NTFS ADS 流 | `test_scope.py::test_real_ads_stream_on_windows`、`::test_ads_detection`;`test_contracts.py`(词法拒绝) | Windows 真实建 `:hidden` 流 + 跨平台词法 | ✅ windows-latest + 全平台词法 | 已验证 |
| junction(reparse) | `test_scope.py::test_junction_detected_on_windows`、`::test_missing_leaf_under_junction_inherits_ancestor_taint` | `mklink /J` 真实 junction | ✅ windows-latest | 已验证:junction 及其下不存在的叶子都带 `junction` taint |
| 保留设备名(NUL/CON/COM…、尾部点空格) | `test_scope.py::test_reserved_name_detection` | 词法(无需真实设备) | ✅ 全平台 | 已验证(词法层) |
| skip-worktree / assume-unchanged 隐写 | `test_snapshot.py::test_unverifiable_paths_reports_skip_worktree_file` | 真实 `git update-index --skip-worktree` | ✅ 全平台 | 已验证:标 unverifiable,不冒充已验证 |
| clean-filter / autocrlf 哈希分歧 | `test_snapshot.py::test_sha256_file_hashes_raw_bytes_without_newline_normalization` | 哈希磁盘原始字节,绕开 filter / 行尾归一 | ✅ 全平台 | 已验证:哈希不做行尾归一 |
| 进程树静默 + 全链杀(Job Object) | `test_jobobject.py::test_jobobject_kills_assigned_child_on_exit` | Windows Job Object 真实 | ✅ windows-latest | 已验证:assigned child 在 job 关闭时被杀 |
| 符号链接逃逸 | `test_scope.py`(POSIX 分支) | POSIX 真实符号链接(Windows 建符号链接需管理员,改由 junction 覆盖 reparse) | ✅ ubuntu / macos | 已验证(POSIX);Windows reparse 由 junction 用例覆盖 |
| **detached 孙进程「快照后写」时序** | — | 机制(进程树静默 + Job Object 全链杀)已测;但「孙进程在快照取完后再写盘」这一**端到端时序竞态**尚无专门 PoC | ⚠️ **部分** | **未单独验证**:杀进程树的机制在位,但该竞态的端到端 PoC 待补 |

## 仍待补(诚实清单)

- **detached-孙进程「快照后写」端到端 PoC** —— 见上表最后一行。
- **PR4 的 WAL + 逐文件回滚** —— 未做;有改动的成功 handoff 一律标 `detected_but_not_reverted`(见 [`v0.2-roadmap.md`](v0.2-roadmap.md) 的「实现状态」)。

> 一句话:Windows 路径形态(硬链接 / junction / ADS / 保留名)与 skip-worktree / filter 哈希、Job Object 全链杀都已在 `windows-latest` CI 实跑验证;唯一未单独验证的是 detached-孙进程的「快照后写」时序竞态,以及 PR4 回滚。
