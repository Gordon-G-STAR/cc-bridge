"""safe 模式的 git 分支隔离辅助函数。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from . import config


@dataclass
class SafePrep:
    ok: bool
    message: str = ""
    original_branch: str | None = None
    temp_branch: str | None = None


@dataclass
class SafeFinish:
    committed: bool = False
    note: str = ""
    diff_summary: str = ""


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").strip()


def _git_error(result: config.CapturedRun) -> str:
    text = _decode(result.stderr) or _decode(result.stdout)
    if result.timed_out:
        return "git 命令超时"
    return text or "git 命令失败"


def _run_git(git: str, cwd: str, args: list[str], *, timeout: int = 30) -> config.CapturedRun:
    return config.git_capture(git, cwd, args, timeout=timeout)


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _commit_message(task: str) -> str:
    prefix = "cc-bridge: "
    body = _one_line(task) or "agent changes"
    limit = 72 - len(prefix)
    if len(body) > limit:
        body = f"{body[: limit - 1]}…"
    return f"{prefix}{body}"


def _diff_summary(text: str) -> str:
    if not text:
        return ""
    text = text[:1200]
    return f"改动 diffstat：\n{text}"


def prepare_safe_branch(cwd: str) -> SafePrep:
    """校验干净 git 仓库，并切到隔离临时分支。"""
    try:
        git = config.resolve_cli("git")
        if not git:
            return SafePrep(False, "未找到 git，无法启用 safe 模式")

        inside = _run_git(git, cwd, ["rev-parse", "--is-inside-work-tree"], timeout=15)
        if inside.returncode != 0 or _decode(inside.stdout) != "true":
            return SafePrep(False, "当前目录不是 git 仓库，safe 模式需要 git 仓库")

        branch = _run_git(git, cwd, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=15)
        branch_name = _decode(branch.stdout)
        if branch.returncode != 0:
            return SafePrep(False, f"无法读取当前分支：{_git_error(branch)}")
        if branch_name == "HEAD":
            return SafePrep(False, "HEAD 处于游离状态，请先切到一个分支")

        head = _run_git(git, cwd, ["rev-parse", "--verify", "HEAD"], timeout=15)
        if head.returncode != 0:
            return SafePrep(False, "仓库还没有任何提交，无法建立隔离基线；请先至少提交一次")

        status = _run_git(git, cwd, ["status", "--porcelain"], timeout=15)
        if status.returncode != 0:
            return SafePrep(False, f"无法检查工作区状态：{_git_error(status)}")
        if _decode(status.stdout):
            return SafePrep(False, "工作区有未提交改动；safe 模式要求干净工作区，请先提交或 git stash")

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        temp = f"cc-bridge/{timestamp}-{os.getpid()}"
        switched = _run_git(git, cwd, ["switch", "-c", temp], timeout=30)
        if switched.returncode != 0:
            return SafePrep(False, f"无法创建 safe 临时分支：{_git_error(switched)}")
        return SafePrep(True, original_branch=branch_name, temp_branch=temp)
    except Exception as exc:  # noqa: BLE001 - safe 前置失败只报告，不抛给 MCP。
        return SafePrep(False, f"safe 模式前置检查遇到内部错误：{exc}")


def finish_safe_branch(
    cwd: str,
    original_branch: str | None,
    temp_branch: str | None,
    task: str,
) -> SafeFinish:
    """收尾 safe 临时分支；任何失败都保留现场，避免丢失改动。"""
    committed = False
    original = original_branch or ""
    temp = temp_branch or ""
    try:
        git = config.resolve_cli("git")
        if not git:
            return SafeFinish(False, "⚠️ safe 模式：未找到 git，safe 收尾无法继续，请手动检查 git 状态。")
        if not original or not temp:
            return SafeFinish(False, "⚠️ safe 模式：safe 收尾缺少分支信息，请手动检查 git 状态。")

        status = _run_git(git, cwd, ["status", "--porcelain"], timeout=15)
        if status.returncode != 0:
            return SafeFinish(
                False,
                f"⚠️ safe 模式：safe 收尾无法读取工作区状态（{_git_error(status)}），请手动检查 git 状态。",
            )

        if not _decode(status.stdout):
            switched = _run_git(git, cwd, ["switch", original], timeout=30)
            if switched.returncode != 0:
                return SafeFinish(
                    False,
                    f"⚠️ safe 模式：对方未产生任何改动，但切回原分支 {original} 失败；你当前仍在 {temp} 上。",
                )
            deleted = _run_git(git, cwd, ["branch", "-D", temp], timeout=30)
            if deleted.returncode != 0:
                return SafeFinish(
                    False,
                    f"⚠️ safe 模式：对方未产生任何改动，已切回 {original}，但清理临时分支 {temp} 失败。",
                )
            return SafeFinish(
                False,
                "🔒 safe 模式：对方未产生任何改动，已切回原分支并清理临时分支。",
            )

        added = _run_git(git, cwd, ["add", "-A"], timeout=30)
        if added.returncode != 0:
            return SafeFinish(
                False,
                f"⚠️ safe 模式：在临时分支 {temp} 上暂存改动失败。对方的改动仍保留在临时分支 {temp} 的工作区里，请手动处理；你当前仍在 {temp} 上。",
            )

        committed_result = _run_git(
            git,
            cwd,
            [
                "-c",
                "user.name=cc-bridge",
                "-c",
                "user.email=cc-bridge@local",
                "commit",
                "-m",
                _commit_message(task),
            ],
            timeout=60,
        )
        if committed_result.returncode != 0:
            return SafeFinish(
                False,
                f"⚠️ safe 模式：在临时分支 {temp} 上提交失败（可能被 pre-commit 钩子拦截）。对方的改动仍保留在临时分支 {temp} 的工作区里，请手动处理；你当前仍在 {temp} 上。",
            )
        committed = True

        diff = _run_git(git, cwd, ["diff", "--stat", f"{original}..{temp}"], timeout=30)
        diff_summary = _diff_summary(_decode(diff.stdout)) if diff.returncode == 0 else ""

        switched = _run_git(git, cwd, ["switch", original], timeout=30)
        if switched.returncode != 0:
            return SafeFinish(
                True,
                f"⚠️ safe 模式：已在临时分支 {temp} 提交改动，但切回原分支 {original} 失败；你当前仍在 {temp} 上。",
                diff_summary,
            )
        return SafeFinish(
            True,
            (
                f"🔒 safe 模式：已把改动提交到临时分支 {temp} 并切回 {original}"
                f"（{original} 未受影响）。查看：git switch {temp}｜对比：git diff {original}..{temp}"
                f"｜合并：git merge {temp}｜丢弃：git branch -D {temp}。"
            ),
            diff_summary,
        )
    except Exception as exc:  # noqa: BLE001 - 收尾失败保留现场，交给用户手动处理。
        return SafeFinish(
            committed,
            f"⚠️ safe 模式：safe 收尾遇到内部错误，请手动检查 git 状态。错误：{exc}",
        )
