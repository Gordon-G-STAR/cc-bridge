"""gitsafe safe 模式的真实 git 仓库测试。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from cc_bridge.bridge import config, gitsafe


def _git() -> str:
    git = shutil.which("git")
    if not git:
        pytest.skip("git not found")
    return git


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_git(), *args],
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )


def _git_text(cwd: Path, *args: str) -> str:
    return _run_git(cwd, *args).stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-b", "main")
    _run_git(repo, "config", "user.email", "tester@example.invalid")
    _run_git(repo, "config", "user.name", "Tester")
    # 固定行尾策略:gitsafe 的 clean 检查走 GIT_OPTIONAL_LOCKS=0(不刷新 index),
    # 若依赖机器全局 autocrlf,LF 写入的文件可能被误判为已改动。显式关掉转换,
    # 让测试在开着 autocrlf 的 Windows 机器上也确定干净(与生产无关:真实 checkout
    # 的工作树由 git smudge 而来,本就一致)。
    _run_git(repo, "config", "core.autocrlf", "false")
    (repo / "demo.txt").write_text("base\n", encoding="utf-8")
    _run_git(repo, "add", "demo.txt")
    _run_git(repo, "commit", "-m", "initial")
    return repo


def _branches(cwd: Path) -> set[str]:
    out = _git_text(cwd, "branch", "--format=%(refname:short)")
    return set(out.splitlines()) if out else set()


def _current_branch(cwd: Path) -> str:
    return _git_text(cwd, "rev-parse", "--abbrev-ref", "HEAD")


def test_prepare_safe_branch_succeeds_on_clean_repo(tmp_path):
    repo = _init_repo(tmp_path)

    prep = gitsafe.prepare_safe_branch(str(repo))

    assert prep.ok is True
    assert prep.original_branch == "main"
    assert prep.temp_branch is not None
    assert prep.temp_branch.startswith("cc-bridge/")
    assert _current_branch(repo) == prep.temp_branch


def test_prepare_safe_branch_rejects_dirty_repo_without_creating_branch(tmp_path):
    repo = _init_repo(tmp_path)
    before = _branches(repo)
    (repo / "demo.txt").write_text("dirty\n", encoding="utf-8")

    prep = gitsafe.prepare_safe_branch(str(repo))

    assert prep.ok is False
    assert "干净" in prep.message
    assert _branches(repo) == before


def test_prepare_safe_branch_rejects_non_git_directory(monkeypatch, tmp_path):
    directory = tmp_path / "plain"
    directory.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    prep = gitsafe.prepare_safe_branch(str(directory))

    assert prep.ok is False
    assert "git 仓库" in prep.message


def test_finish_safe_branch_commits_changes_and_switches_back(tmp_path):
    repo = _init_repo(tmp_path)
    prep = gitsafe.prepare_safe_branch(str(repo))
    assert prep.ok is True
    assert prep.original_branch is not None
    assert prep.temp_branch is not None
    (repo / "demo.txt").write_text("changed\n", encoding="utf-8")

    finish = gitsafe.finish_safe_branch(
        str(repo), prep.original_branch, prep.temp_branch, "修改 demo 文件"
    )

    assert finish.committed is True
    assert "safe 模式" in finish.note
    assert _current_branch(repo) == prep.original_branch
    assert _git_text(repo, "status", "--porcelain") == ""
    assert _git_text(repo, "log", "-1", "--format=%s", prep.temp_branch).startswith(
        "cc-bridge:"
    )
    assert _git_text(repo, "diff", "--stat", f"{prep.original_branch}..{prep.temp_branch}")
    assert "改动 diffstat：" in finish.diff_summary


def test_finish_safe_branch_without_changes_cleans_temp_branch(tmp_path):
    repo = _init_repo(tmp_path)
    prep = gitsafe.prepare_safe_branch(str(repo))
    assert prep.ok is True
    assert prep.original_branch is not None
    assert prep.temp_branch is not None

    finish = gitsafe.finish_safe_branch(
        str(repo), prep.original_branch, prep.temp_branch, "无改动任务"
    )

    assert finish.committed is False
    assert "safe 模式" in finish.note
    assert _current_branch(repo) == prep.original_branch
    assert prep.temp_branch not in _branches(repo)


def test_finish_safe_branch_commit_failure_preserves_work_on_temp_branch(
    monkeypatch, tmp_path
):
    repo = _init_repo(tmp_path)
    prep = gitsafe.prepare_safe_branch(str(repo))
    assert prep.ok is True
    assert prep.original_branch is not None
    assert prep.temp_branch is not None
    (repo / "demo.txt").write_text("blocked by hook\n", encoding="utf-8")
    _orig_git_capture = config.git_capture

    def _fail_commit(g, cwd, args, *, timeout):
        if "commit" in args:
            return config.CapturedRun(
                returncode=1,
                stdout=b"",
                stderr=b"pre-commit hook failed",
                timed_out=False,
            )
        return _orig_git_capture(g, cwd, args, timeout=timeout)

    monkeypatch.setattr(config, "git_capture", _fail_commit)

    finish = gitsafe.finish_safe_branch(
        str(repo), prep.original_branch, prep.temp_branch, "触发 hook 失败"
    )

    assert finish.committed is False
    assert "提交失败" in finish.note
    assert _current_branch(repo) == prep.temp_branch
    assert _git_text(repo, "status", "--porcelain") != ""
