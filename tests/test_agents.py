from __future__ import annotations

import json

import pytest

from cc_bridge.bridge.agents import AGENTS, ClaudeAgent, CodexAgent, get_agent


def test_agent_registry_returns_codex_and_claude_agents():
    assert set(AGENTS) == {"codex", "claude"}
    assert get_agent("codex") is AGENTS["codex"]
    assert get_agent("claude") is AGENTS["claude"]


def test_get_agent_unknown_name_raises_clear_error():
    with pytest.raises(ValueError, match="未知的 agent"):
        get_agent("gemini")


def test_codex_build_args_default_new_session_requires_final_file():
    agent = CodexAgent()

    args = agent.build_args(final_file="C:/tmp/final.txt")

    assert args == [
        "exec",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "--json",
        "-o",
        "C:/tmp/final.txt",
    ]


def test_codex_build_args_resume_dry_run_and_model():
    agent = CodexAgent()

    args = agent.build_args(
        mode_override="read-only",
        model="o3",
        resume_session_id="sid-123",
        final_file="C:/tmp/final.txt",
    )

    assert args == [
        "exec",
        "resume",
        "-c",
        "sandbox_mode=read-only",
        "sid-123",
        "--skip-git-repo-check",
        "--json",
        "-o",
        "C:/tmp/final.txt",
        "-m",
        "o3",
        "-",
    ]


def test_codex_build_args_requires_final_file():
    with pytest.raises(ValueError, match="final_file"):
        CodexAgent().build_args()


def test_claude_build_args_default_new_session():
    agent = ClaudeAgent()

    args = agent.build_args()

    assert args == [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
    ]


def test_claude_build_args_resume_dry_run_and_model():
    agent = ClaudeAgent()

    args = agent.build_args(
        mode_override="plan",
        model="opus",
        resume_session_id="sid-claude",
    )

    assert args == [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "plan",
        "--model",
        "opus",
        "--resume",
        "sid-claude",
    ]


def test_codex_extract_prefers_final_file_and_collects_usage_and_session(tmp_path):
    final_file = tmp_path / "final.txt"
    final_file.write_text("final answer\n", encoding="utf-8")
    stdout = "\n".join(
        [
            json.dumps({"type": "session.created", "session_id": "sid-jsonl"}),
            json.dumps({"type": "token_count", "usage": {"input_tokens": 7}}),
            json.dumps({"type": "agent_message", "text": "stdout answer"}),
        ]
    )

    text, usage, is_error, session_id = CodexAgent().extract(stdout, str(final_file))

    assert text == "final answer"
    assert usage == {"input_tokens": 7}
    assert is_error is False
    assert session_id == "sid-jsonl"


def test_codex_extract_falls_back_to_jsonl_message_then_stdout():
    stdout = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "jsonl answer"},
        }
    )

    text, usage, is_error, session_id = CodexAgent().extract(stdout, None)

    assert text == "jsonl answer"
    assert usage is None
    assert is_error is False
    assert session_id is None


def test_claude_extract_single_json_result_usage_cost_and_session():
    stdout = json.dumps(
        {
            "result": "claude answer",
            "usage": {"input_tokens": 10},
            "total_cost_usd": 0.02,
            "is_error": False,
            "session_id": "sid-claude",
        }
    )

    text, usage, is_error, session_id = ClaudeAgent().extract(stdout, None)

    assert text == "claude answer"
    assert usage == {"input_tokens": 10, "total_cost_usd": 0.02}
    assert is_error is False
    assert session_id == "sid-claude"


def test_claude_extract_stream_json_uses_result_and_stream_session_fallback():
    stdout = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "session_id": "sid-init"}),
            json.dumps({"type": "assistant", "message": {"content": "not final"}}),
            json.dumps({"type": "result", "result": "final", "is_error": False}),
        ]
    )

    text, usage, is_error, session_id = ClaudeAgent().extract(stdout, None)

    assert text == "final"
    assert usage is None
    assert is_error is False
    assert session_id == "sid-init"
