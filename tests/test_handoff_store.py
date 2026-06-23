from __future__ import annotations

import os

from cc_bridge.bridge import config, handoff_store
from cc_bridge.bridge.contracts import (
    FailureKind,
    HandoffRequest,
    HandoffResult,
    RequestedScope,
    fail_closed_result,
)


def _req() -> HandoffRequest:
    return HandoffRequest(
        contract_version="1",
        goal="run async handoff",
        acceptance_criteria=["writes terminal result"],
        requested_scope=RequestedScope(writable_paths=["src"]),
    )


def test_init_handoff_and_read_spec_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "stable_app_dir", lambda: tmp_path / "app")
    request = _req()

    handoff_id = handoff_store.init_handoff(
        request, "C:/work/project", agent="codex", caller="claude"
    )

    spec = handoff_store.read_spec(handoff_id)
    assert spec is not None
    assert spec["handoff_id"] == handoff_id
    assert spec["request"] == request
    assert isinstance(spec["request"], HandoffRequest)
    assert spec["cwd"] == "C:/work/project"
    assert spec["agent"] == "codex"
    assert spec["caller"] == "claude"
    assert handoff_store.read_status(handoff_id)["state"] == "pending"


def test_status_result_and_pid_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "stable_app_dir", lambda: tmp_path / "app")
    handoff_id = "abc123"
    result = HandoffResult(
        contract_version="1",
        handoff_id=handoff_id,
        status="completed",
        agent_used="codex",
        summary="done",
    )

    handoff_store.write_status(handoff_id, "running", note="started")
    handoff_store.write_result(handoff_id, result)
    handoff_store.write_pid(handoff_id, 12345)

    status = handoff_store.read_status(handoff_id)
    assert status is not None
    assert status["state"] == "running"
    assert status["note"] == "started"
    assert "updated_at" in status
    assert handoff_store.read_result(handoff_id) == result
    assert handoff_store.read_pid(handoff_id) == 12345


def test_missing_and_bad_reads_return_none(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "stable_app_dir", lambda: tmp_path / "app")
    missing = "missing"
    assert handoff_store.read_spec(missing) is None
    assert handoff_store.read_status(missing) is None
    assert handoff_store.read_result(missing) is None
    assert handoff_store.read_pid(missing) is None

    bad = "bad"
    d = handoff_store.handoff_dir(bad)
    d.mkdir(parents=True)
    (d / "request.json").write_text("{bad json", encoding="utf-8")
    (d / "status.json").write_text("[]", encoding="utf-8")
    (d / "result.json").write_text('{"status":"not-valid"}', encoding="utf-8")
    (d / "runner.pid").write_text("not-a-pid", encoding="utf-8")

    assert handoff_store.read_spec(bad) is None
    assert handoff_store.read_status(bad) is None
    assert handoff_store.read_result(bad) is None
    assert handoff_store.read_pid(bad) is None


def test_prune_keeps_latest_terminal_handoffs(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "stable_app_dir", lambda: tmp_path / "app")
    for idx in range(4):
        handoff_id = f"done{idx}"
        handoff_store.write_status(handoff_id, "completed")
        path = handoff_store.handoff_dir(handoff_id)
        os.utime(path, (idx + 1, idx + 1))

    handoff_store.write_status("running-old", "running")
    os.utime(handoff_store.handoff_dir("running-old"), (0, 0))

    handoff_store.prune(keep=2)

    assert set(handoff_store.list_handoffs()) == {"done2", "done3", "running-old"}
    assert handoff_store.read_status("done0") is None
    assert handoff_store.read_status("done1") is None


def test_read_result_returns_none_for_bad_but_valid_json(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "stable_app_dir", lambda: tmp_path / "app")
    handoff_id = "bad-result"
    handoff_store.write_result(
        handoff_id,
        fail_closed_result(
            handoff_id,
            failure_kind=FailureKind.crashed,
            reason="boom",
            status="failed",
        ),
    )
    (handoff_store.handoff_dir(handoff_id) / "result.json").write_text(
        '{"contract_version":"1","handoff_id":"x","status":"bogus"}',
        encoding="utf-8",
    )

    assert handoff_store.read_result(handoff_id) is None
