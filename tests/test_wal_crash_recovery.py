"""PR4c: WAL 崩溃恢复测试 — project_lock acquire 时续完未完回滚。"""

from __future__ import annotations

import json

from cc_bridge.bridge import config, wal
from cc_bridge.bridge.locks import project_lock


def test_project_lock_resumes_pending_rollback(monkeypatch, tmp_path):
    """lock acquire → 扫到 reverting 残留 → 续完回滚 → 文件被恢复。"""
    app_dir = tmp_path / "app"
    monkeypatch.setattr(config, "stable_app_dir", lambda: app_dir)

    root = tmp_path / "project"
    root.mkdir()
    target = root / "file.txt"
    target.write_bytes(b"original")

    wal.record_baseline("crash-h", root, ["file.txt"])
    target.write_bytes(b"agent wrote this")

    wal_dir = app_dir / "wal" / "crash-h"
    manifest_path = wal_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["state"] = "reverting"
    manifest["to_revert"] = ["file.txt"]
    manifest["reverted"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert wal.pending_rollbacks() == ["crash-h"]

    with project_lock(root):
        pass

    assert target.read_bytes() == b"original"
    assert wal.pending_rollbacks() == []
