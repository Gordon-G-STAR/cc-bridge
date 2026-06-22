from __future__ import annotations

import json

from cc_bridge.bridge.audit import append_audit_record


def test_append_audit_record_writes_json_line_when_enabled(tmp_path, monkeypatch):
    log_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("CC_BRIDGE_AUDIT_LOG", str(log_path))
    task = "x" * 260

    append_audit_record(
        direction="codex",
        cwd=str(tmp_path),
        task=task,
        success=True,
        files_changed=["src/foo.py", "tests/test_foo.py"],
    )

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["direction"] == "codex"
    assert record["cwd"] == str(tmp_path)
    assert record["success"] is True
    assert record["files_changed"] == ["src/foo.py", "tests/test_foo.py"]
    assert record["task_summary"].startswith("x" * 100)
    assert len(record["task_summary"]) < len(task)
    assert record["timestamp"]


def test_append_audit_record_is_noop_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("CC_BRIDGE_AUDIT_LOG", raising=False)
    monkeypatch.chdir(tmp_path)

    append_audit_record(
        direction="claude",
        cwd=str(tmp_path),
        task="do work",
        success=False,
        files_changed=[],
    )

    assert list(tmp_path.iterdir()) == []


def test_append_audit_record_swallows_write_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_BRIDGE_AUDIT_LOG", str(tmp_path))

    append_audit_record(
        direction="codex",
        cwd=str(tmp_path),
        task="do work",
        success=True,
        files_changed=[],
    )
