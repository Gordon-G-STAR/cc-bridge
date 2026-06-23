# 合同验收清单（示例）

`- [cmd] 命令` 项由 cc-bridge 直接在 `project_dir` 里跑命令(退出码 0=通过、非 0=未完成),
确定性、不需 agent。`- 自然语言描述` 项交给 claude/codex 只读判断(见 README「语义项」)。

本清单全用 cmd 项,跑出的报告见 [`report.example.md`](report.example.md)。

- [cmd] python -c "import calc; assert calc.add(2,3)==5"
- [cmd] python -c "import os,sys; sys.exit(0 if os.path.exists('calc.py') else 1)"
- [cmd] python -c "import os,sys; sys.exit(0 if os.path.exists('CHANGELOG.md') else 1)"
