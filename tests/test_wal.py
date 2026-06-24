from __future__ import annotations

import json

from cc_bridge.bridge import config, wal


def _use_app_dir(monkeypatch, tmp_path):
    app_dir = tmp_path / "app"
    monkeypatch.setattr(config, "stable_app_dir", lambda: app_dir)
    return app_dir


def _read_manifest(app_dir, handoff_id):
    return json.loads(
        (app_dir / "wal" / handoff_id / "manifest.json").read_text(encoding="utf-8")
    )


def test_record_baseline_stores_manifest_and_deduplicates_blobs(monkeypatch, tmp_path):
    app_dir = _use_app_dir(monkeypatch, tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    (root / "nested").mkdir()
    (root / "a.txt").write_bytes(b"same content")
    (root / "nested" / "b.txt").write_bytes(b"same content")

    state = wal.record_baseline("h1", root, ["a.txt", "nested/b.txt"])

    assert state == "ready"
    manifest = _read_manifest(app_dir, "h1")
    assert manifest["state"] == "ready"
    assert manifest["baseline"]["a.txt"]["existed"] is True
    assert manifest["baseline"]["nested/b.txt"]["existed"] is True
    assert (
        manifest["baseline"]["a.txt"]["sha"]
        == manifest["baseline"]["nested/b.txt"]["sha"]
    )
    blobs = list((app_dir / "wal" / "h1" / "blobs").iterdir())
    assert len(blobs) == 1
    assert blobs[0].read_bytes() == b"same content"


def test_rollback_restores_modified_file(monkeypatch, tmp_path):
    _use_app_dir(monkeypatch, tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    target = root / "tracked.txt"
    target.write_bytes(b"original")
    wal.record_baseline("h2", root, ["tracked.txt"])
    target.write_bytes(b"changed")

    result = wal.rollback("h2", root, ["tracked.txt"])

    assert result.reverted == ["tracked.txt"]
    assert result.failed == []
    assert result.missing_baseline == []
    assert target.read_bytes() == b"original"


def test_rollback_deletes_out_of_scope_new_files(monkeypatch, tmp_path):
    _use_app_dir(monkeypatch, tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    wal.record_baseline("h3", root, ["known-new.txt"])
    known_new = root / "known-new.txt"
    unknown_new = root / "unknown-new.txt"
    known_new.write_bytes(b"created after baseline")
    unknown_new.write_bytes(b"created outside baseline")

    result = wal.rollback("h3", root, ["known-new.txt", "unknown-new.txt"])

    assert result.reverted == ["known-new.txt", "unknown-new.txt"]
    assert result.failed == []
    assert result.missing_baseline == ["unknown-new.txt"]
    assert not known_new.exists()
    assert not unknown_new.exists()


def test_rollback_only_touches_requested_paths(monkeypatch, tmp_path):
    _use_app_dir(monkeypatch, tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    a = root / "a.txt"
    b = root / "b.txt"
    a.write_bytes(b"a original")
    b.write_bytes(b"b original")
    wal.record_baseline("h4", root, ["a.txt", "b.txt"])
    a.write_bytes(b"a changed")
    b.write_bytes(b"b changed")

    result = wal.rollback("h4", root, ["a.txt"])

    assert result.reverted == ["a.txt"]
    assert result.failed == []
    assert a.read_bytes() == b"a original"
    assert b.read_bytes() == b"b changed"


def test_record_baseline_too_large_is_honest_and_rollback_skips(monkeypatch, tmp_path):
    _use_app_dir(monkeypatch, tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    target = root / "large.txt"
    target.write_bytes(b"larger than ten bytes")

    state = wal.record_baseline("h5", root, ["large.txt"], max_bytes=10)
    target.write_bytes(b"changed after skipped baseline")
    result = wal.rollback("h5", root, ["large.txt"])

    assert state == "skipped_too_large"
    assert result.skipped is True
    assert result.reverted == []
    assert result.failed == ["large.txt"]
    assert result.missing_baseline == []
    assert target.read_bytes() == b"changed after skipped baseline"


def test_rollback_missing_manifest_fails_all_without_raising(monkeypatch, tmp_path):
    _use_app_dir(monkeypatch, tmp_path)
    root = tmp_path / "project"
    root.mkdir()

    result = wal.rollback("missing", root, ["a.txt", "b.txt"])

    assert result.reverted == []
    assert result.failed == ["a.txt", "b.txt"]
    assert result.missing_baseline == []
    assert result.skipped is False


def test_pending_rollbacks_lists_only_incomplete_reverting_manifests(
    monkeypatch, tmp_path
):
    app_dir = _use_app_dir(monkeypatch, tmp_path)
    wal_root = app_dir / "wal"
    pending = wal_root / "pending"
    done = wal_root / "done"
    complete_reverting = wal_root / "complete-reverting"
    pending.mkdir(parents=True)
    done.mkdir(parents=True)
    complete_reverting.mkdir(parents=True)
    (pending / "manifest.json").write_text(
        json.dumps(
            {
                "state": "reverting",
                "baseline": {},
                "to_revert": ["a.txt", "b.txt"],
                "reverted": ["a.txt"],
            }
        ),
        encoding="utf-8",
    )
    (done / "manifest.json").write_text(
        json.dumps(
            {
                "state": "reverted",
                "baseline": {},
                "to_revert": ["a.txt"],
                "reverted": ["a.txt"],
            }
        ),
        encoding="utf-8",
    )
    (complete_reverting / "manifest.json").write_text(
        json.dumps(
            {
                "state": "reverting",
                "baseline": {},
                "to_revert": ["a.txt"],
                "reverted": ["a.txt"],
            }
        ),
        encoding="utf-8",
    )

    assert wal.pending_rollbacks() == ["pending"]
