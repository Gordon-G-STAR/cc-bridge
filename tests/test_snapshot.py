from __future__ import annotations

import hashlib
import importlib


def _snapshot_module():
    return importlib.import_module("cc_bridge.bridge.snapshot")


def test_sha256_file_is_stable_and_changes_with_content(tmp_path):
    snapshot = _snapshot_module()
    path = tmp_path / "data.txt"
    path.write_bytes(b"same")

    first = snapshot.sha256_file(path)
    second = snapshot.sha256_file(path)

    assert first == second
    path.write_bytes(b"different")
    assert snapshot.sha256_file(path) != first
    assert snapshot.sha256_file(tmp_path / "missing.txt") is None


def test_sha256_file_hashes_raw_bytes_without_newline_normalization(tmp_path):
    snapshot = _snapshot_module()
    raw = b"line1\r\nline2\r\n"
    path = tmp_path / "crlf.txt"
    path.write_bytes(raw)

    assert snapshot.sha256_file(path) == hashlib.sha256(raw).hexdigest()


def test_snapshot_files_maps_present_and_absent_files(tmp_path):
    snapshot = _snapshot_module()
    present = tmp_path / "present.txt"
    present.write_bytes(b"content")

    result = snapshot.snapshot_files(tmp_path, ["present.txt", "absent.txt"])

    assert result == {
        "present.txt": hashlib.sha256(b"content").hexdigest(),
        "absent.txt": None,
    }


def test_diff_snapshots_reports_modified_and_added_paths(tmp_path):
    snapshot = _snapshot_module()
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    third = tmp_path / "third.txt"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    before = snapshot.snapshot_files(tmp_path, ["first.txt", "second.txt", "third.txt"])

    first.write_bytes(b"changed")
    third.write_bytes(b"three")
    after = snapshot.snapshot_files(tmp_path, ["first.txt", "second.txt", "third.txt"])

    assert snapshot.diff_snapshots(before, after) == ["first.txt", "third.txt"]


def test_diff_snapshots_detects_second_dirty_file_content_change(tmp_path):
    snapshot = _snapshot_module()
    path = tmp_path / "dirty.txt"
    path.write_bytes(b"first dirty content")
    before = snapshot.snapshot_files(tmp_path, ["dirty.txt"])

    path.write_bytes(b"second dirty content")
    after = snapshot.snapshot_files(tmp_path, ["dirty.txt"])

    assert snapshot.diff_snapshots(before, after) == ["dirty.txt"]
