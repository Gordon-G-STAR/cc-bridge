from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cc_bridge import cli
from cc_bridge.bridge.executor import ExecutionResult
from cc_bridge.checklist import (
    ChecklistItem,
    ItemResult,
    check_item,
    parse_checklist,
    render_report,
)


def test_parse_checklist_supports_plain_checkbox_star_and_command_items():
    text = """
# 标题

这行不是列表
- a
- [ ] b
* c
- [cmd] pytest -q
"""

    assert parse_checklist(text) == [
        ChecklistItem(text="a", kind="semantic"),
        ChecklistItem(text="b", kind="semantic"),
        ChecklistItem(text="c", kind="semantic"),
        ChecklistItem(text="pytest -q", kind="command"),
    ]


def test_render_report_groups_counts_high_risk_and_deduplicates_files():
    results = [
        ItemResult(
            item=ChecklistItem("done item", "semantic"),
            status="done",
            risk="low",
            files=["a.py"],
            note="done note",
        ),
        ItemResult(
            item=ChecklistItem("partial item", "semantic"),
            status="partial",
            risk="high",
            files=["b.py", "a.py"],
            note="partial note",
        ),
        ItemResult(
            item=ChecklistItem("missing item", "semantic"),
            status="missing",
            risk="low",
            files=[],
            note="missing note",
        ),
        ItemResult(
            item=ChecklistItem("error item", "semantic"),
            status="error",
            risk="low",
            files=["b.py"],
            note="error note",
        ),
    ]

    report = render_report("C:/repo", results)

    assert "项目：C:/repo" in report
    assert "总数：4" in report
    assert "done=1" in report
    assert "partial=1" in report
    assert "missing=1" in report
    assert "error=1" in report
    assert "## ✅ 已完成" in report
    assert "## ⚠️ 部分" in report
    assert "## ❌ 未完成" in report
    assert "## ⛔ 错误" in report
    assert "## 🔴 高风险" in report
    assert "- partial item — partial note" in report
    assert "## 建议关注的文件" in report
    assert report.count("- a.py") == 1
    assert report.count("- b.py") == 1


async def test_check_item_semantic_parses_json_from_agent_output(monkeypatch):
    async def fake_run_claude(self, prompt, cwd, permission_override=None, timeout=None):
        assert permission_override == "plan"
        assert "只输出一个 JSON 对象" in prompt
        return ExecutionResult(
            success=True,
            output='前言 {"status":"partial","risk":"high","files":["a.py"],"note":"x"} 后语',
        )

    monkeypatch.setattr("cc_bridge.checklist.AgentExecutor.run_claude", fake_run_claude)

    result = await check_item(ChecklistItem("检查验收项", "semantic"), "C:/repo")

    assert result.status == "partial"
    assert result.risk == "high"
    assert result.files == ["a.py"]
    assert result.note == "x"


async def test_check_item_semantic_returns_error_when_json_missing(monkeypatch):
    async def fake_run_claude(self, prompt, cwd, permission_override=None, timeout=None):
        return ExecutionResult(success=True, output="没有 JSON")

    monkeypatch.setattr("cc_bridge.checklist.AgentExecutor.run_claude", fake_run_claude)

    result = await check_item(ChecklistItem("检查验收项", "semantic"), "C:/repo")

    assert result.status == "error"
    assert result.risk == "high"
    assert "没有 JSON" in result.note


async def test_check_item_command_reports_done_and_missing():
    py = Path(sys.executable).as_posix()

    ok = await check_item(ChecklistItem(f'{py} -c "print(1)"', "command"), ".", timeout=300)
    fail = await check_item(
        ChecklistItem(f'{py} -c "import sys;sys.exit(1)"', "command"),
        ".",
        timeout=300,
    )

    assert ok.status == "done"
    assert ok.risk == "low"
    assert "1" in ok.note
    assert fail.status == "missing"
    assert fail.risk == "low"


def test_cmd_checklist_run_writes_report_and_returns_failure_for_missing(
    monkeypatch,
    tmp_path,
):
    checklist_file = tmp_path / "checklist.md"
    checklist_file.write_text("- a\n", encoding="utf-8")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    report_file = tmp_path / "report.md"

    async def fake_run_checklist(items, cwd, *, agent, timeout):
        assert items == [ChecklistItem("a", "semantic")]
        assert cwd == str(project_dir)
        assert agent == "claude"
        assert timeout == 123
        return [
            ItemResult(
                item=items[0],
                status="missing",
                risk="low",
                files=[],
                note="not found",
            )
        ]

    monkeypatch.setattr("cc_bridge.checklist.run_checklist", fake_run_checklist)

    rc = cli.cmd_checklist_run(
        argparse.Namespace(
            checklist=str(checklist_file),
            project_dir=str(project_dir),
            agent="claude",
            report=str(report_file),
            timeout=123,
        )
    )

    assert rc == 1
    report = report_file.read_text(encoding="utf-8")
    assert "项目：" in report
    assert "- a — not found" in report
