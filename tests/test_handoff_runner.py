from __future__ import annotations

import subprocess

import pytest

from cc_bridge.bridge import config, handoff, handoff_runner, handoff_store
from cc_bridge.bridge.contracts import (
    FailureKind,
    HandoffRequest,
    RequestedScope,
    fail_closed_result,
)
from cc_bridge.bridge.executor import AgentExecutor, ExecutionResult


@pytest.fixture(autouse=True)
def clean_policy_env(monkeypatch):
    for name in (
        "CC_BRIDGE_POLICY_WRITABLE_PATHS",
        "CC_BRIDGE_POLICY_READONLY",
        "CC_BRIDGE_POLICY_ALLOW_NETWORK",
        "CC_BRIDGE_POLICY_MAX_DEPTH",
        "CC_BRIDGE_POLICY_REQUIRE_APPROVAL",
        "CC_BRIDGE_CHAIN_DEPTH",
        "CC_BRIDGE_CHAIN_SCOPE",
        "CC_BRIDGE_CODEX_SANDBOX",
        "CC_BRIDGE_CLAUDE_PERMISSION",
    ):
        monkeypatch.delenv(name, raising=False)


def _req(writable=None) -> HandoffRequest:
    return HandoffRequest(
        contract_version="1",
        goal="make async runner finish",
        acceptance_criteria=["result status is completed"],
        requested_scope=RequestedScope(writable_paths=writable or []),
    )


def _run_git(repo, *args) -> None:
    git = config.resolve_cli("git")
    if git is None:
        pytest.skip("git is required for runner evidence tests")
    subprocess.run(
        [git, *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _init_git_repo(repo) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init")
    _run_git(repo, "config", "user.name", "cc-bridge test")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "init")


async def test_run_spec_codex_success_writes_completed_result(monkeypatch, tmp_path):
    app_dir = tmp_path / "app"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(config, "stable_app_dir", lambda: app_dir)
    calls: list[dict] = []

    async def _fake_run_codex(self, prompt, cwd, **kwargs):
        calls.append({"prompt": prompt, "cwd": cwd, **kwargs})
        return ExecutionResult(
            success=True,
            output="done",
            files_changed=[],
            duration_seconds=0.1,
            exit_code=0,
        )

    monkeypatch.setattr(AgentExecutor, "run_codex", _fake_run_codex)

    handoff_id = handoff_store.init_handoff(
        _req(), str(repo), agent="codex", caller="claude"
    )
    await handoff_runner.run_spec(handoff_id)

    assert handoff_store.read_status(handoff_id)["state"] == "completed"
    result = handoff_store.read_result(handoff_id)
    assert result is not None
    assert result.status == "completed"
    assert result.agent_used == "codex"
    assert calls[0]["cwd"] == str(repo)
    assert calls[0]["timeout"] == 300
    assert calls[0]["sandbox_override"] == "read-only"


async def test_run_spec_policy_denied_writes_terminal_result_without_executor(
    monkeypatch, tmp_path
):
    app_dir = tmp_path / "app"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(config, "stable_app_dir", lambda: app_dir)
    handoff_id = handoff_store.init_handoff(
        _req(writable=["src"]), str(repo), agent="codex", caller="claude"
    )

    def _deny(*args, **kwargs):
        return fail_closed_result(
            handoff_id,
            failure_kind=FailureKind.policy_denied,
            reason="denied by test",
            status="policy_denied",
        )

    async def _must_not_run(self, *args, **kwargs):
        raise AssertionError("executor must not run after policy denial")

    monkeypatch.setattr(handoff, "authorize", _deny)
    monkeypatch.setattr(AgentExecutor, "run_codex", _must_not_run)

    await handoff_runner.run_spec(handoff_id)

    assert handoff_store.read_status(handoff_id)["state"] == "policy_denied"
    result = handoff_store.read_result(handoff_id)
    assert result is not None
    assert result.status == "policy_denied"
    assert result.failure_kind is FailureKind.policy_denied
