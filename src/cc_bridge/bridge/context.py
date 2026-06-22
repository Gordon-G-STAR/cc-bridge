"""项目上下文收集，以及构造发给对方 agent 的 prompt.

跨 agent 调用时，被调用方是一个全新的会话，对当前项目一无所知。这里把
最关键的项目信息（语言、目录结构、git 状态、关键配置文件）打包进 prompt，
让对方能立刻进入状态，而不会因为缺上下文而瞎猜。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import config

# 允许跨 agent 调用触达的工作区根（os.pathsep 分隔的绝对路径）的环境变量名。
_ALLOWED_ROOTS_ENV = "CC_BRIDGE_ALLOWED_ROOTS"


def _allowed_roots() -> list[Path]:
    """读取 ``CC_BRIDGE_ALLOWED_ROOTS`` 配置的允许工作区根，已 resolve（解符号链接/..）。

    未设置时返回空列表——此时**不施加**白名单限制，行为与历史一致（向后兼容）。
    安全敏感的部署应设置它，把跨 agent 调用能触达、能改文件的目录锁死在指定根内，
    防止调用方 agent 被仓库内容里的 prompt 注入带偏、指向 ``~/.ssh`` 等任意路径。
    """
    raw = os.environ.get(_ALLOWED_ROOTS_ENV, "").strip()
    if not raw:
        return []
    roots: list[Path] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        try:
            roots.append(Path(part).resolve())
        except OSError:
            continue
    return roots


def _within_allowed(resolved: Path, roots: list[Path]) -> bool:
    """``resolved`` 是否落在某个允许根之内（含根本身）。"""
    for root in roots:
        try:
            if resolved == root or resolved.is_relative_to(root):
                return True
        except (OSError, ValueError):
            continue
    return False


def require_project_dir(project_dir: str | None) -> str:
    """校验并返回跨 agent 调用要用的项目目录绝对路径。

    cc-bridge 绝不擅自用进程 cwd 兜底：MCP server 由桌面应用拉起，其工作目录往往
    **不是** 用户的项目目录（多半是 home 或 App 目录）。沉默兜底会让对方在错误目录里
    改文件——在 ``workspace-write`` / ``bypassPermissions`` 默认下尤其危险。

    因此这里强制：必须显式传入、且是存在的【绝对路径】，否则抛 :class:`ValueError`，
    由调用方（MCP 工具）转成清晰提示返回给模型。

    若设置了 ``CC_BRIDGE_ALLOWED_ROOTS``（见 :func:`_allowed_roots`），还会在
    ``resolve()``（解开 ``..`` 与符号链接）后强制目标必须落在允许根之内，越界即拒绝——
    这是针对“调用方被 prompt 注入带偏、指向任意目录”的纵深防御。
    """
    if not project_dir or not str(project_dir).strip():
        raise ValueError(
            "未提供 project_dir。请把【当前项目的绝对路径】作为 project_dir 传入"
            "（如 'C:/Users/me/proj' 或 '/home/me/proj'）。"
            "cc-bridge 不会猜测工作目录，以免对方在错误的目录里改文件。"
        )
    path = Path(project_dir)
    if not path.is_absolute():
        raise ValueError(
            f"project_dir 必须是绝对路径，收到的是相对路径：{project_dir}。"
            "请改传当前项目的绝对路径。"
        )
    if not path.is_dir():
        raise ValueError(f"project_dir 不存在或不是一个目录：{project_dir}。")

    roots = _allowed_roots()
    if roots:
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise ValueError(f"无法解析 project_dir：{project_dir}（{exc}）。") from exc
        if not _within_allowed(resolved, roots):
            allowed = "、".join(str(r) for r in roots)
            raise ValueError(
                f"project_dir 不在允许的工作区范围内：{resolved}。"
                f"cc-bridge 已通过 {_ALLOWED_ROOTS_ENV} 把可操作目录限定为：{allowed}。"
                "如确需放开，请调整该环境变量。"
            )
    return str(path)


# 收集上下文时要跳过的噪音目录。
_IGNORE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".idea", ".vscode",
    "target", ".next", ".turbo", "coverage", ".gradle", "vendor",
}

# 优先展示给对方看的配置文件（存在则截取内容）。
_KEY_FILES = [
    "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "pom.xml",
    "build.gradle", "requirements.txt", "README.md", "tsconfig.json",
]

# 扩展名 -> 语言名，用来推断项目主语言。
_LANG_BY_EXT = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".rs": "Rust", ".go": "Go", ".java": "Java",
    ".rb": "Ruby", ".php": "PHP", ".c": "C", ".cpp": "C++", ".cs": "C#",
    ".swift": "Swift", ".kt": "Kotlin", ".scala": "Scala", ".sh": "Shell",
}

_MAX_KEY_FILE_CHARS = 1500
_INJECT_CONTEXT_ENV = "CC_BRIDGE_INJECT_CONTEXT"


@dataclass
class ProjectContext:
    root: str
    language: str
    tree: str
    git_branch: str | None = None
    git_dirty: bool = False
    key_files: dict[str, str] = field(default_factory=dict)


class ContextBuilder:
    """收集项目上下文，并据此构造跨 agent 调用的完整 prompt。"""

    def build_project_context(self, cwd: str) -> ProjectContext:
        root = Path(cwd).resolve()
        language = self._detect_language(root)
        tree = self._build_tree(root)
        branch, dirty = self._git_info(str(root))
        key_files = self._collect_key_files(root)
        return ProjectContext(
            root=str(root),
            language=language,
            tree=tree,
            git_branch=branch,
            git_dirty=dirty,
            key_files=key_files,
        )

    # -- 内部实现 ---------------------------------------------------------
    def _detect_language(self, root: Path) -> str:
        counts: dict[str, int] = {}
        for path in self._walk(root, max_depth=3):
            if path.is_file():
                lang = _LANG_BY_EXT.get(path.suffix.lower())
                if lang:
                    counts[lang] = counts.get(lang, 0) + 1
        if not counts:
            return "Unknown"
        return max(counts, key=counts.get)

    def _build_tree(self, root: Path, max_depth: int = 2) -> str:
        lines: list[str] = [root.name + "/"]

        def walk(directory: Path, prefix: str, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                entries = sorted(
                    [e for e in directory.iterdir() if e.name not in _IGNORE_DIRS and not e.name.startswith(".")],
                    key=lambda e: (e.is_file(), e.name.lower()),
                )
            except OSError:
                return
            entries = entries[:40]  # 单层最多 40 项，避免巨型目录刷屏
            for entry in entries:
                lines.append(f"{prefix}{entry.name}{'/' if entry.is_dir() else ''}")
                if entry.is_dir():
                    walk(entry, prefix + "  ", depth + 1)

        walk(root, "  ", 1)
        return "\n".join(lines[:120])

    def _walk(self, root: Path, max_depth: int):
        stack = [(root, 0)]
        while stack:
            directory, depth = stack.pop()
            try:
                entries = list(directory.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.name in _IGNORE_DIRS:
                    continue
                if entry.is_dir():
                    if depth < max_depth:
                        stack.append((entry, depth + 1))
                else:
                    yield entry

    def _git_info(self, cwd: str) -> tuple[str | None, bool]:
        # 走 config.git_capture：stdin=DEVNULL + 杀进程树超时 + 禁 fsmonitor/交互凭据。
        # 这两条命令历史上是 codex_execute 卡死的源头（subprocess.run 在 Windows 上超时后
        # 第二次无超时 communicate() 被孙进程持管道拖死）；git_capture 从根上杜绝。
        git = config.resolve_cli("git")
        if not git:
            return None, False
        branch = None
        dirty = False
        rb = config.git_capture(git, cwd, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
        if rb.returncode == 0:
            branch = rb.stdout.decode("utf-8", errors="replace").strip() or None
        rs = config.git_capture(git, cwd, ["status", "--porcelain"], timeout=10)
        if rs.returncode == 0:
            dirty = bool(rs.stdout.decode("utf-8", errors="replace").strip())
        return branch, dirty

    def _collect_key_files(self, root: Path) -> dict[str, str]:
        collected: dict[str, str] = {}
        for name in _KEY_FILES:
            path = root / name
            if path.is_file():
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if len(content) > _MAX_KEY_FILE_CHARS:
                    content = content[:_MAX_KEY_FILE_CHARS] + "\n…（已截断）"
                collected[name] = content
        return collected

    # -- prompt 构造 ------------------------------------------------------
    def build_task_prompt(self, original_prompt: str, ctx: ProjectContext, caller: str) -> str:
        """把项目上下文 + 任务 + 调用方身份组合成完整 prompt。

        Args:
            original_prompt: 用户/调用方原始的任务描述。
            ctx: 项目上下文。
            caller: ``"claude"`` 或 ``"codex"``，告诉对方是谁在调用。
        """
        caller_name = "Claude" if caller == "claude" else "Codex"
        git_line = "（非 git 仓库）"
        if ctx.git_branch is not None:
            git_line = f"分支 {ctx.git_branch}" + ("，有未提交改动" if ctx.git_dirty else "，工作区干净")

        key_files_section = ""
        if ctx.key_files and config.env_bool(_INJECT_CONTEXT_ENV, default=True):
            blocks = []
            for name, content in ctx.key_files.items():
                blocks.append(f"--- {name} ---\n{content}")
            key_files_section = "\n\n关键配置文件：\n" + "\n\n".join(blocks)

        return (
            f"你正在被 {caller_name} 通过 cc-bridge 调用，协作处理同一个项目。\n"
            f"项目根目录：{ctx.root}\n"
            f"主要语言：{ctx.language}\n"
            f"Git 状态：{git_line}\n\n"
            f"项目结构（最多两层）：\n{ctx.tree}\n"
            f"{key_files_section}\n\n"
            f"========== 需要你完成的任务 ==========\n{original_prompt}\n\n"
            f"完成后，请用简洁的自然语言总结你做了什么、改动了哪些文件、结果如何。"
        )
