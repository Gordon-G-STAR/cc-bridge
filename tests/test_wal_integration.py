"""PR4b: WAL 接入 handoff 的集成测试。"""

from __future__ import annotations

from cc_bridge.bridge import config, wal
from cc_bridge.bridge.contracts import HandoffRequest, RequestedScope, SideEffectStatus
from cc_bridge.bridge.evidence import EvidenceResult
from cc_bridge.bridge.executor import ExecutionResult
from cc_bridge.bridge.handoff import HandoffPlan, execution_to_handoff


def _req() -> HandoffRequest:
    return HandoffRequest(
        contract_version="1",
        goal="test",
        requested_scope=RequestedScope(writable_paths=["src"]),
    )


def _plan() -> HandoffPlan:
    return HandoffPlan(
        agent="codex",
        write_granted=True,
        effective_writable=("src",),
        network_granted=False,
        engine_mode="workspace-write",
        child_env={},
        depth=1,
        route_note="test",
    )


def _exec_result(success=True) -> ExecutionResult:
    return ExecutionResult(
        success=success,
        output="ok",
        raw_stdout="ok",
        exit_code=0 if success else 1,
        timed_out=False,
        duration_seconds=1.0,
    )


def test_scope_violation_triggers_rollback_and_marks_reverted(monkeypatch, tmp_path):
    """越界改动 → WAL 回滚 → detected_and_reverted。"""
    app_dir = tmp_path / "app"
    monkeypatch.setattr(config, "stable_app_dir", lambda: app_dir)

    root = tmp_path / "project"
    root.mkdir()
    secret = root / "secret.txt"
    secret.write_bytes(b"original content")

    wal.record_baseline("h-test", root, ["secret.txt"])
    secret.write_bytes(b"tampered by agent")

    ev = EvidenceResult(
        verified_files=[],
        scope_violations=["secret.txt"],
        unverifiable=[],
        evidence_level="verified",
        reasons={"secret.txt": "outside granted writable scope"},
    )
    result = execution_to_handoff(
        "h-test", _req(), _exec_result(), "summary", "codex",
        evidence=ev, plan=_plan(), project_root=str(root),
    )

    assert result.status == "scope_violation"
    assert result.side_effects.worktree_files == SideEffectStatus.detected_and_reverted
    assert secret.read_bytes() == b"original content"


def test_scope_violation_without_wal_falls_back_to_not_reverted(monkeypatch, tmp_path):
    """无 WAL baseline → 回滚失败 → detected_but_not_reverted(诚实)。"""
    app_dir = tmp_path / "app"
    monkeypatch.setattr(config, "stable_app_dir", lambda: app_dir)

    root = tmp_path / "project"
    root.mkdir()

    ev = EvidenceResult(
        verified_files=[],
        scope_violations=["rogue.txt"],
        unverifiable=[],
        evidence_level="verified",
        reasons={"rogue.txt": "outside granted writable scope"},
    )
    result = execution_to_handoff(
        "no-wal", _req(), _exec_result(), "summary", "codex",
        evidence=ev, plan=_plan(), project_root=str(root),
    )

    assert result.status == "scope_violation"
    assert result.side_effects.worktree_files == SideEffectStatus.detected_but_not_reverted


def test_no_violation_with_verified_files_marks_reverted(monkeypatch, tmp_path):
    """合法改动 → detected_and_reverted(合法文件已由 WAL 确认)。"""
    app_dir = tmp_path / "app"
    monkeypatch.setattr(config, "stable_app_dir", lambda: app_dir)

    ev = EvidenceResult(
        verified_files=["src/main.py"],
        scope_violations=[],
        unverifiable=[],
        evidence_level="verified",
        reasons={},
    )
    result = execution_to_handoff(
        "h-ok", _req(), _exec_result(), "summary", "codex",
        evidence=ev, plan=_plan(),
    )

    assert result.status == "completed"
    assert result.side_effects.worktree_files == SideEffectStatus.detected_and_reverted


def test_no_changes_marks_none():
    """无改动 → none。"""
    ev = EvidenceResult(
        verified_files=[],
        scope_violations=[],
        unverifiable=[],
        evidence_level="verified",
        reasons={},
    )
    result = execution_to_handoff(
        "h-noop", _req(), _exec_result(), "summary", "codex",
        evidence=ev, plan=_plan(),
    )

    assert result.status == "completed"
    assert result.side_effects.worktree_files == SideEffectStatus.none
