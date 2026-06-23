"""验收清单逐项短调用检查。

这里刻意不走 MCP：每个清单项都在本进程内直接拉起一次短 AgentExecutor 调用，
避免同步 MCP 客户端长任务超时影响整份验收。
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field

from cc_bridge.bridge import config
from cc_bridge.bridge.executor import AgentExecutor

_STATUSES = {"done", "partial", "missing", "error"}
_RISKS = {"low", "high"}
_SUMMARY_LIMIT = 200


@dataclass(frozen=True)
class ChecklistItem:
    text: str
    kind: str


@dataclass
class ItemResult:
    item: ChecklistItem
    status: str
    risk: str
    files: list[str] = field(default_factory=list)
    note: str = ""


def parse_checklist(text: str) -> list[ChecklistItem]:
    """从 Markdown 风格列表里提取验收项；非列表内容只当说明，忽略。"""
    items: list[ChecklistItem] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):
            body = line[2:].strip()
        elif line.startswith("* "):
            body = line[2:].strip()
        else:
            continue

        if body.startswith("[ ] "):
            body = body[4:].strip()
        if not body:
            continue

        if body.startswith("[cmd] "):
            items.append(ChecklistItem(text=body[6:].strip(), kind="command"))
        else:
            items.append(ChecklistItem(text=body, kind="semantic"))
    return items


def _shorten(text: str, limit: int = _SUMMARY_LIMIT) -> str:
    text = str(text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _decode_run_output(stdout: bytes, stderr: bytes) -> str:
    parts = [
        chunk.decode("utf-8", errors="replace").strip()
        for chunk in (stdout, stderr)
        if chunk
    ]
    return _shorten("\n".join(part for part in parts if part))


def _error_result(item: ChecklistItem, output: str) -> ItemResult:
    return ItemResult(
        item=item,
        status="error",
        risk="high",
        files=[],
        note=_shorten(output),
    )


def _extract_json_object(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("agent output does not contain a JSON object")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("agent JSON output is not an object")
    return data


def _item_result_from_agent_json(item: ChecklistItem, data: dict) -> ItemResult:
    status = data.get("status")
    if status not in {"done", "partial", "missing"}:
        raise ValueError(f"invalid checklist status: {status!r}")

    risk = data.get("risk", "low")
    if risk not in _RISKS:
        risk = "low"

    raw_files = data.get("files", [])
    files = [str(path) for path in raw_files] if isinstance(raw_files, list) else []

    note = data.get("note", "")
    return ItemResult(
        item=item,
        status=status,
        risk=risk,
        files=files,
        note=_shorten(str(note)),
    )


def _semantic_prompt(item: ChecklistItem) -> str:
    return (
        "请对下面这个验收清单项做只读检查。\n"
        "不要修改文件，不要运行破坏性命令。\n"
        "只输出一个 JSON 对象，格式必须是："
        '{"status":"done|partial|missing","risk":"low|high","files":[...],"note":"一句话"}\n'
        f"验收项：{item.text}"
    )


async def check_item(
    item: ChecklistItem,
    cwd: str,
    *,
    agent: str = "claude",
    timeout: int = 300,
) -> ItemResult:
    """顺序调用方使用的单项检查入口；调用者负责不要并发调度。"""
    if item.kind == "command":
        run = config.run_capture(shlex.split(item.text), timeout=min(timeout, 120), cwd=cwd)
        note = _decode_run_output(run.stdout, run.stderr)
        if not note:
            if run.timed_out:
                note = "命令超时"
            elif run.returncode is None:
                note = "命令启动失败"
            else:
                note = f"退出码 {run.returncode}"
        return ItemResult(
            item=item,
            status="done" if run.returncode == 0 else "missing",
            risk="low",
            files=[],
            note=note,
        )

    if item.kind != "semantic":
        return _error_result(item, f"未知清单项类型：{item.kind}")

    executor = AgentExecutor()
    prompt = _semantic_prompt(item)
    if agent == "codex":
        result = await executor.run_codex(
            prompt,
            cwd,
            timeout=timeout,
            sandbox_override="read-only",
        )
    else:
        result = await executor.run_claude(
            prompt,
            cwd,
            timeout=timeout,
            permission_override="plan",
        )

    output = result.output or result.error or ""
    if not result.success:
        return _error_result(item, output)

    try:
        return _item_result_from_agent_json(item, _extract_json_object(output))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return _error_result(item, output or str(exc))


async def run_checklist(
    items: list[ChecklistItem],
    cwd: str,
    *,
    agent: str,
    timeout: int,
) -> list[ItemResult]:
    """严格顺序执行，避免多个 agent 子进程互相争用或拉长单项窗口。"""
    results: list[ItemResult] = []
    for item in items:
        results.append(await check_item(item, cwd, agent=agent, timeout=timeout))
    return results


def _items_for_status(results: list[ItemResult], status: str) -> list[ItemResult]:
    return [result for result in results if result.status == status]


def _format_items(results: list[ItemResult]) -> list[str]:
    if not results:
        return ["- 无"]
    return [f"- {result.item.text} — {result.note}" for result in results]


def render_report(project_dir: str, results: list[ItemResult]) -> str:
    counts = {status: 0 for status in _STATUSES}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1

    lines = [
        "# cc-bridge checklist-run 报告",
        "",
        f"项目：{project_dir}",
        f"总数：{len(results)}",
        (
            "状态计数："
            f"done={counts.get('done', 0)} "
            f"partial={counts.get('partial', 0)} "
            f"missing={counts.get('missing', 0)} "
            f"error={counts.get('error', 0)}"
        ),
    ]

    sections = [
        ("## ✅ 已完成", _items_for_status(results, "done")),
        ("## ⚠️ 部分", _items_for_status(results, "partial")),
        ("## ❌ 未完成", _items_for_status(results, "missing")),
        ("## ⛔ 错误", _items_for_status(results, "error")),
        ("## 🔴 高风险", [result for result in results if result.risk == "high"]),
    ]
    for title, section_results in sections:
        lines.extend(["", title, *_format_items(section_results)])

    seen_files: set[str] = set()
    unique_files: list[str] = []
    for result in results:
        for path in result.files:
            if path not in seen_files:
                seen_files.add(path)
                unique_files.append(path)

    lines.extend(["", "## 建议关注的文件"])
    if unique_files:
        lines.extend(f"- {path}" for path in unique_files)
    else:
        lines.append("- 无")

    return "\n".join(lines) + "\n"
