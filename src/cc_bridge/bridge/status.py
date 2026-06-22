"""运行期：探测对端 CLI（claude / codex）是否就绪.

这是常驻的 MCP server 在运行期真正需要的最小信息：CLI 是否可用、版本、登录态。
**刻意放在 bridge 层**，让 MCP server 不必反向依赖 installer（那里夹着 tkinter GUI 与
PyInstaller 打包代码）。installer 的 :class:`~cc_bridge.installer.detector.EnvironmentDetector`
在此之上再叠加「桌面应用是否安装」等安装期信息。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import config
from .agents import get_agent


@dataclass
class AgentReadiness:
    """对端 agent（Claude / Codex）的运行期就绪状态。"""

    name: str                  # "Claude" / "Codex"
    cli_path: str | None
    version: str | None
    logged_in: bool            # 是否【确认】已登录
    login_known: bool = True   # 登录态是否可确认（Claude 可能在 keychain，无法读时为 False）

    @property
    def cli_available(self) -> bool:
        return self.cli_path is not None

    @property
    def ready(self) -> bool:
        """CLI 可用，且「已确认登录」或「登录态无法确认」（不臆断未登录而阻塞）。"""
        return self.cli_available and (self.logged_in or not self.login_known)

    def status_line(self) -> str:
        """给 ``*_status`` MCP 工具用的一行可读状态。"""
        if not self.cli_available:
            return f"{self.name} 命令行不可用（未安装或不在 PATH）。"
        parts = [f"{self.name} 命令行可用"]
        if self.version:
            parts.append(f"版本：{self.version}")
        if self.logged_in:
            parts.append("就绪")
        elif not self.login_known:
            parts.append("登录态未确认")
        else:
            parts.append("尚未就绪（似乎未登录）")
        return " ｜ ".join(parts)


def cli_version(name: str, exe: str | None) -> str | None:
    """运行 ``<cli> --version`` 取版本号；失败返回 None（UTF-8 解码，GBK 安全）。

    走 config.run_capture：stdin=DEVNULL + 杀进程树超时，避免在 MCP server 里因子进程
    读 stdin / 持管道而挂死（与 git 探测同一类硬化）。
    """
    if not exe:
        return None
    res = config.run_capture(config.build_launch_argv(exe, ["--version"]), timeout=20)
    out = (
        res.stdout.decode("utf-8", errors="replace")
        or res.stderr.decode("utf-8", errors="replace")
    ).strip()
    return out.splitlines()[0] if out else None


def claude_login() -> tuple[bool, bool]:
    """返回 (是否确认已登录, 登录态是否可确认)。

    只认真正的凭证文件 ``~/.claude/.credentials.json``；找不到时标记「未知」而非臆断
    已登录（``~/.claude.json`` 是 CLI 状态文件、登出后仍在，不能当登录凭据）。
    """
    if (Path.home() / ".claude" / ".credentials.json").exists():
        return True, True
    return False, False


def check_claude() -> AgentReadiness:
    agent = get_agent("claude")
    exe = config.resolve_cli(agent.cli_name)
    logged_in, login_known = claude_login()
    return AgentReadiness(
        "Claude", exe, cli_version(agent.cli_name, exe), logged_in, login_known
    )


def check_codex() -> AgentReadiness:
    # Codex 登录态可由 ~/.codex/auth.json 可靠判断。
    agent = get_agent("codex")
    exe = config.resolve_cli(agent.cli_name)
    logged_in = config.codex_auth_path().exists()
    return AgentReadiness("Codex", exe, cli_version(agent.cli_name, exe), logged_in, True)
