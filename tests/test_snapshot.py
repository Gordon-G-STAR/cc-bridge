from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
import subprocess

import pytest

from cc_bridge.bridge import config


def _snapshot_module():
    return importlib.import_module("cc_bridge.bridge.snapshot")


def _git_or_skip() -> str:
    git = config.resolve_cli("git")
    if not git:
        pytest.skip("git is not available")
    return git


def _run_git(git: str, cwd, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [git, *args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        text=True,
    )


def _hide_parent_git_repo(monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(project_root))


def _make_git_repo(tmp_path):
    git = _git_or_skip()
    repo = tmp_path / "repo"
    repo.mkdir()

    _run_git(git, repo, ["init"])
    _run_git(git, repo, ["config", "user.email", "test@example.com"])
    _run_git(git, repo, ["config", "user.name", "Test User"])

    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("ignored\n", encoding="utf-8")

    _run_git(git, repo, ["add", "tracked.txt", ".gitignore"])
    _run_git(git, repo, ["commit", "-m", "initial"])
    return git, repo


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


def test_list_snapshot_targets_includes_tracked_and_untracked_files(tmp_path):
    snapshot = _snapshot_module()
    _git, repo = _make_git_repo(tmp_path)

    targets = snapshot.list_snapshot_targets(repo)

    assert "tracked.txt" in targets
    assert "untracked.txt" in targets
    assert ".gitignore" in targets
    assert "ignored.txt" not in targets
    assert all(not path.startswith(".git/") for path in targets)


def test_is_git_repo_detects_repo_and_plain_directory(tmp_path, monkeypatch):
    _hide_parent_git_repo(monkeypatch)
    snapshot = _snapshot_module()
    _git, repo = _make_git_repo(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()

    assert snapshot.is_git_repo(repo) is True
    assert snapshot.is_git_repo(plain) is False


def test_unverifiable_paths_reports_skip_worktree_file(tmp_path):
    snapshot = _snapshot_module()
    git, repo = _make_git_repo(tmp_path)

    _run_git(git, repo, ["update-index", "--skip-worktree", "tracked.txt"])

    assert "tracked.txt" in snapshot.unverifiable_paths(repo)


def test_git_snapshot_helpers_return_empty_for_non_repo(tmp_path):
    snapshot = _snapshot_module()
    non_repo = tmp_path / "non_repo"
    non_repo.mkdir()

    assert snapshot.list_snapshot_targets(non_repo) == []
    assert snapshot.unverifiable_paths(non_repo) == set()
