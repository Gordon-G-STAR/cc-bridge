# cc-bridge checklist-run 报告

> 这是 `checklist.md` 在 `sample_project/` 上跑出的真实报告(项目路径已用占位)。整体退出码 **1**(有未完成项)。

项目：<...>/examples/acceptance_check_demo/sample_project
总数：3
状态计数：done=2 partial=0 missing=1 error=0

## ✅ 已完成
- python -c "import calc; assert calc.add(2,3)==5" — 退出码 0
- python -c "import os,sys; sys.exit(0 if os.path.exists('calc.py') else 1)" — 退出码 0

## ⚠️ 部分
- 无

## ❌ 未完成
- python -c "import os,sys; sys.exit(0 if os.path.exists('CHANGELOG.md') else 1)" — 退出码 1

## ⛔ 错误
- 无

## 🔴 高风险
- 无

## 建议关注的文件
- 无
