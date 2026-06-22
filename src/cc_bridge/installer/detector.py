"""环境检测：判断 Claude / Codex 的桌面版、CLI、登录状态是否就绪.

bridge 真正依赖的是两个 **CLI**（``claude`` / ``codex``）——它们随桌面版一起安装，
并继承桌面版的登录态。所以这里以「CLI 是否可用 + 是否已登录」作为功能就绪的核心判据，
桌面 App 是否安装作为辅助信息一并报告。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cc_bridge.bridge import config
from cc_bridge.bridge import status as bridge_status


@dataclass
class CheckResult:
    """单个 agent 的检测结果，字段直接用于安装界面展示。"""

    name: str                  # "Claude" / "Codex"
    installed: bool            # 桌面版或 CLI 任一存在
    cli_available: bool        # CLI 命令是否可用
    logged_in: bool            # 是否【确认】已登录
    version: str | None
    message: str               # 给用户看的提示文字
    cli_path: str | None = None
    login_known: bool = True   # 登录态是否可确认（Claude 登录可能在 keychain，无法读时为 False）

    @property
    def ready(self) -> bool:
        """功能就绪 = CLI 可用，且「已确认登录」或「登录态无法确认」（不臆断未登录而阻塞用户）。

        只有在「确认未登录」（login_known 且 not logged_in，如 Codex 缺 auth.json）时才判为未就绪。
        """
        return self.cli_available and (self.logged_in or not self.login_known)


@dataclass
class EnvironmentStatus:
    claude: CheckResult
    codex: CheckResult

    @property
    def all_ready(self) -> bool:
        return self.claude.ready and self.codex.ready

    def as_dict(self) -> dict:
        return {
            "claude": self.claude.__dict__,
            "codex": self.codex.__dict__,
            "all_ready": self.all_ready,
        }


class EnvironmentDetector:
    """检测安装条件，在 :mod:`cc_bridge.bridge.status` 的运行期就绪探测之上，叠加
    「桌面应用是否安装」与面向用户的提示文案，供 GUI / CLI 使用。"""

    def check_all(self) -> EnvironmentStatus:
        return EnvironmentStatus(
            claude=self.check_claude_desktop(),
            codex=self.check_codex_desktop(),
        )

    # -- Claude -----------------------------------------------------------
    def check_claude_desktop(self) -> CheckResult:
        r = bridge_status.check_claude()
        desktop = self._claude_desktop_installed()
        installed = r.cli_available or desktop
        message = self._message("Claude", installed, r.cli_available, r.logged_in, r.login_known,
                                 install_hint="请安装 Claude 桌面版并登录 Max 账号")
        return CheckResult(
            name="Claude", installed=installed, cli_available=r.cli_available,
            logged_in=r.logged_in, version=r.version, message=message, cli_path=r.cli_path,
            login_known=r.login_known,
        )

    def _claude_desktop_installed(self) -> bool:
        # 桌面版配置目录存在通常意味着 App 至少运行过一次。
        if config.claude_desktop_config_path().parent.exists():
            return True
        candidates: list[Path] = []
        if config.IS_WINDOWS:
            import os
            local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            candidates += [local / "AnthropicClaude", local / "Programs" / "claude"]
        elif config.IS_MACOS:
            candidates += [Path("/Applications/Claude.app")]
        return any(p.exists() for p in candidates)

    # -- Codex ------------------------------------------------------------
    def check_codex_desktop(self) -> CheckResult:
        r = bridge_status.check_codex()
        desktop = self._codex_desktop_installed()
        installed = r.cli_available or desktop
        message = self._message("Codex", installed, r.cli_available, r.logged_in, r.login_known,
                                install_hint="请安装 Codex 并用 ChatGPT 账号登录（codex login）")
        return CheckResult(
            name="Codex", installed=installed, cli_available=r.cli_available,
            logged_in=r.logged_in, version=r.version, message=message, cli_path=r.cli_path,
            login_known=r.login_known,
        )

    def _codex_desktop_installed(self) -> bool:
        if config.codex_home().exists():
            return True
        if config.IS_MACOS and Path("/Applications/Codex.app").exists():
            return True
        return False

    # -- 文案 -------------------------------------------------------------
    @staticmethod
    def _message(name: str, installed: bool, cli: bool, logged_in: bool,
                 login_known: bool, install_hint: str) -> str:
        if not installed and not cli:
            return f"未检测到 {name}。{install_hint}。"
        if not cli:
            return f"检测到 {name} 桌面版，但命令行不可用。请确认安装完整后重启。"
        if logged_in:
            return f"{name} 已就绪。"
        if not login_known:
            return (
                f"{name} 命令行可用，但无法确认是否已登录；"
                f"若尚未登录，请打开 {name} 桌面版登录后再使用。"
            )
        return f"{name} 命令行可用，但似乎未登录。{install_hint}。"
