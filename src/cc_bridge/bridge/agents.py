"""Agent adapters for MCP-able CLI agents.

To add another agent such as Gemini CLI:
1. Implement an ``Agent`` subclass with CLI args, extraction, and progress labels.
2. Register one instance in ``AGENTS``.
3. Keep executor orchestration generic: prompts still go through stdin.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path


class Agent(ABC):
    """Adapter boundary for one MCP-able CLI agent."""

    name: str
    cli_name: str
    default_mode: str | None = None
    requires_final_file: bool = False
    final_file_prefix: str = "cc_bridge_agent_"
    unavailable_name: str = "Agent CLI"

    @abstractmethod
    def build_args(
        self,
        *,
        mode_override: str | None = None,
        model: str | None = None,
        resume_session_id: str | None = None,
        final_file: str | None = None,
    ) -> list[str]:
        """Build CLI arguments, excluding the executable and prompt."""

    @abstractmethod
    def extract(
        self, stdout: str, final_file: str | None
    ) -> tuple[str, dict | None, bool, str | None]:
        """Return ``(text, usage, is_error, session_id)`` from agent output."""

    @abstractmethod
    def progress_label(self, event: dict) -> str | None:
        """Return a human-readable one-line progress label for a JSON event."""


class CodexAgent(Agent):
    name = "codex"
    cli_name = "codex"
    default_mode = "workspace-write"
    requires_final_file = True
    final_file_prefix = "cc_bridge_codex_"
    unavailable_name = "Codex CLI"

    def build_args(
        self,
        *,
        mode_override: str | None = None,
        model: str | None = None,
        resume_session_id: str | None = None,
        final_file: str | None = None,
    ) -> list[str]:
        if not final_file:
            raise ValueError("CodexAgent.build_args requires final_file")

        sandbox = mode_override if mode_override is not None else self.default_mode
        if resume_session_id:
            args = [
                "exec",
                "resume",
                "-c",
                f"sandbox_mode={sandbox}",
                resume_session_id,
                "--skip-git-repo-check",
                "--json",
                "-o",
                final_file,
            ]
        else:
            args = [
                "exec",
                "--sandbox",
                sandbox,
                "--skip-git-repo-check",
                "--json",
                "-o",
                final_file,
            ]
        if model:
            args += ["-m", model]
        if resume_session_id:
            args.append("-")
        return args

    def extract(
        self, stdout: str, final_file: str | None
    ) -> tuple[str, dict | None, bool, str | None]:
        final_message = ""
        if final_file:
            try:
                final_message = Path(final_file).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                final_message = ""
        text, usage = _extract_codex(final_message, stdout)
        if not text:
            text = stdout.strip()
        return text, usage, False, _extract_codex_session_id(stdout)

    def progress_label(self, event: dict) -> str | None:
        return _codex_progress_label(event)


class ClaudeAgent(Agent):
    name = "claude"
    cli_name = "claude"
    default_mode = "bypassPermissions"
    unavailable_name = "Claude Code CLI"

    def build_args(
        self,
        *,
        mode_override: str | None = None,
        model: str | None = None,
        resume_session_id: str | None = None,
        final_file: str | None = None,
    ) -> list[str]:
        permission_mode = (
            mode_override if mode_override is not None else self.default_mode
        )
        args = [
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            permission_mode,
        ]
        if model:
            args += ["--model", model]
        if resume_session_id:
            args += ["--resume", resume_session_id]
        return args

    def extract(
        self, stdout: str, final_file: str | None
    ) -> tuple[str, dict | None, bool, str | None]:
        return _extract_claude(stdout)

    def progress_label(self, event: dict) -> str | None:
        return _claude_progress_label(event)


def _extract_claude_event(data: dict) -> tuple[str, dict | None, bool, str | None]:
    text = data.get("result") or data.get("text") or ""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = None
    if data.get("total_cost_usd") is not None:
        usage = {**(usage or {}), "total_cost_usd": data["total_cost_usd"]}
    is_error = bool(data.get("is_error"))
    session_id = _find_session_id(data)
    return (text if isinstance(text, str) else json.dumps(text)), usage, is_error, session_id


def _extract_claude(stdout: str) -> tuple[str, dict | None, bool, str | None]:
    stdout = stdout.strip()
    if not stdout:
        return "", None, False, None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        return _extract_claude_event(data)

    result_event: dict | None = None
    stream_session_id: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if stream_session_id is None:
            stream_session_id = _find_session_id(event)
        if event.get("type") == "result":
            result_event = event

    if result_event is not None:
        text, usage, is_error, session_id = _extract_claude_event(result_event)
        return text, usage, is_error, session_id or stream_session_id
    return stdout, None, False, stream_session_id


def _extract_codex(final_message: str, stdout_jsonl: str) -> tuple[str, dict | None]:
    text = final_message.strip()
    usage: dict | None = None
    for line in stdout_jsonl.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        for key in ("usage", "token_usage", "tokens"):
            if isinstance(event.get(key), dict):
                usage = event[key]
        if not text:
            item = event.get("item") if isinstance(event.get("item"), dict) else None
            candidate = None
            if item and item.get("type") in {
                "agent_message",
                "assistant_message",
                "message",
            }:
                candidate = item.get("text") or item.get("content")
            elif event.get("type") in {"agent_message", "assistant_message"}:
                candidate = event.get("text") or event.get("message")
            if isinstance(candidate, str) and candidate.strip():
                text = candidate.strip()
    return text, usage


_SESSION_ID_KEYS = (
    "session_id",
    "sessionId",
    "conversation_id",
    "conversationId",
    "thread_id",
    "threadId",
)


def _short_progress(text: str, limit: int = 240) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _string_value(value) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("text", "content", "message", "result"):
            nested = _string_value(value.get(key))
            if nested:
                return nested
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                nested = _string_value(item.get("text") or item.get("content"))
                if nested:
                    parts.append(nested)
        return " ".join(parts).strip() or None
    return None


def _event_text(data: dict) -> str | None:
    for key in ("text", "message", "content", "result"):
        value = _string_value(data.get(key))
        if value:
            return value
    return None


def _event_command(data: dict) -> str | None:
    for key in ("command", "cmd", "shell_command"):
        value = _string_value(data.get(key))
        if value:
            return value
    argv = data.get("argv") or data.get("args")
    if isinstance(argv, list) and all(isinstance(part, str) for part in argv):
        return " ".join(argv).strip() or None
    return None


def _codex_progress_label(event: dict) -> str | None:
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    command = _event_command(item) or _event_command(event)
    if command:
        return f"Codex 正在执行: {_short_progress(command)}"

    event_type = str(event.get("type") or "")
    item_type = str(item.get("type") or "")
    text = _event_text(item) or _event_text(event)
    if text and (
        "message" in event_type
        or "message" in item_type
        or item_type in {"agent_message", "assistant_message"}
    ):
        return f"Codex: {_short_progress(text)}"

    if event_type in {"turn.started", "task.started", "session.created"}:
        return "Codex 已开始处理"
    return None


def _claude_tool_progress_label(event: dict) -> str | None:
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    content = message.get("content") or event.get("content")
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return None

    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_use":
            continue
        tool_name = _string_value(item.get("name")) or "tool"
        tool_input = item.get("input") if isinstance(item.get("input"), dict) else {}
        command = _event_command(tool_input) or _event_command(item)
        if command:
            return f"Claude 正在执行: {_short_progress(command)}"
        return f"Claude 正在使用工具: {_short_progress(tool_name)}"
    return None


def _claude_progress_label(event: dict) -> str | None:
    event_type = str(event.get("type") or "")
    subtype = str(event.get("subtype") or "")

    if event_type == "result":
        if event.get("is_error"):
            return "Claude 返回错误"
        return "Claude 已完成处理"

    tool_label = _claude_tool_progress_label(event)
    if tool_label:
        return tool_label

    text = _event_text(event)
    if text and (event_type == "assistant" or "message" in event_type):
        return f"Claude: {_short_progress(text)}"

    if event_type == "system" and subtype == "init":
        return "Claude 已开始处理"
    return None


def _find_session_id(data: dict) -> str | None:
    for key in _SESSION_ID_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for container_key in ("session", "conversation", "thread"):
        nested = data.get(container_key)
        if not isinstance(nested, dict):
            continue
        for key in ("id", *_SESSION_ID_KEYS):
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    event_type = str(data.get("type") or "")
    value = data.get("id")
    if event_type.startswith("session") and isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _extract_codex_session_id(stdout_jsonl: str) -> str | None:
    for line in stdout_jsonl.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        session_id = _find_session_id(event)
        if session_id:
            return session_id
    return None


AGENTS: dict[str, Agent] = {"codex": CodexAgent(), "claude": ClaudeAgent()}


def get_agent(name: str) -> Agent:
    try:
        return AGENTS[name]
    except KeyError:
        raise ValueError(
            f"未知的 agent：{name!r}（可用：{', '.join(sorted(AGENTS))}）。"
        ) from None
