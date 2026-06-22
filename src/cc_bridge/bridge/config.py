"""配置与平台相关的工具函数.

这里集中处理三件容易出错、且跨平台差异大的事情：

1. **CLI 启动方式**：Windows 上 ``codex`` 实际解析到 ``codex.cmd`` / ``codex.ps1``
   这种 shim 脚本，``asyncio.create_subprocess_exec`` 无法直接拉起，必须用
   ``cmd.exe /c`` 或 ``powershell -File`` 包一层。``claude`` 是真正的 ``.exe``，
   可以直接启动。
2. **隐藏窗口**：从桌面应用后台拉起子进程时，不能弹出黑色控制台窗口。
3. **配置文件路径**：Claude Desktop 和 Codex 的 MCP 配置文件在不同操作系统下位置不同。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量与默认值
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SECONDS = 300          # 单次跨 agent 调用硬上限：5 分钟
DEFAULT_MAX_OUTPUT_CHARS = 4000        # 返回摘要的最大字符数，防止上下文爆炸

# Windows 上隐藏控制台窗口的 creationflag（CREATE_NO_WINDOW）。
_CREATE_NO_WINDOW = 0x08000000

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"


# ---------------------------------------------------------------------------
# CLI 解析与启动
# ---------------------------------------------------------------------------

def _fallback_bin_dirs() -> list[str]:
    """``shutil.which`` 之外再兜底搜索的常见安装目录.

    从 Finder 启动的 macOS ``.app`` 继承到的 PATH 通常很短（只有 /usr/bin 等），
    会漏掉 Homebrew / npm 全局安装的 ``claude`` / ``codex``。这里补上常见目录。
    """
    dirs: list[str] = []
    if not IS_WINDOWS:
        dirs += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
        dirs.append(str(Path.home() / ".local" / "bin"))
        dirs.append(str(Path.home() / ".npm-global" / "bin"))
        dirs.append(str(Path.home() / "node_modules" / ".bin"))
    else:
        appdata = os.environ.get("APPDATA")
        if appdata:
            dirs.append(str(Path(appdata) / "npm"))  # npm 全局（Windows）
    return dirs


def resolve_cli(name: str) -> str | None:
    """返回某个 CLI 可执行文件的绝对路径，找不到则返回 ``None``.

    解析顺序：
    1. 环境变量显式指定（``CC_BRIDGE_CLAUDE_PATH`` / ``CC_BRIDGE_CODEX_PATH`` 等）——
       自动探测失败时的兜底逃生口；
    2. :func:`shutil.which`（Windows 上按 ``PATHEXT`` 补全扩展名，如 ``codex`` -> ``codex.cmd``）；
    3. 常见安装目录兜底（见 :func:`_fallback_bin_dirs`），主要解决 Finder 启动的 macOS app
       PATH 过短、找不到 Homebrew/npm CLI 的问题。
    """
    override = os.environ.get(f"CC_BRIDGE_{name.upper()}_PATH")
    if override and Path(override).is_file():
        return override

    found = shutil.which(name)
    if found:
        return found

    exts = ("", ".cmd", ".exe", ".bat", ".ps1") if IS_WINDOWS else ("",)
    for directory in _fallback_bin_dirs():
        for ext in exts:
            candidate = Path(directory) / (name + ext)
            if candidate.is_file():
                return str(candidate)
    return None


def build_launch_argv(executable: str, args: list[str]) -> list[str]:
    """把「可执行文件 + 参数」转换成真正能交给 ``create_subprocess_exec`` 的 argv.

    - ``.exe`` / 类 Unix 脚本：直接执行。
    - Windows ``.cmd`` / ``.bat``：用 ``cmd.exe /d /s /c`` 包一层
      （npm 生成的 shim 就是设计成这样调用的）。
    - Windows ``.ps1``：用 ``powershell -File`` 包一层。

    **重要约定**：调用方绝不要把不可信的长文本（如用户 prompt）放进 ``args``，
    应当通过 stdin 传递。这样命令行里只剩固定的、安全的参数，彻底规避
    ``cmd.exe`` 元字符注入和命令行长度限制。
    """
    exe = str(executable)
    if IS_WINDOWS:
        low = exe.lower()
        if low.endswith((".cmd", ".bat")):
            return ["cmd.exe", "/d", "/s", "/c", exe, *args]
        if low.endswith(".ps1"):
            return [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                exe,
                *args,
            ]
    return [exe, *args]


def subprocess_creation_kwargs() -> dict:
    """返回隐藏窗口 / 脱离终端所需的平台相关 subprocess 参数."""
    if IS_WINDOWS:
        return {"creationflags": _CREATE_NO_WINDOW}
    # POSIX：脱离父进程的控制终端，避免信号串扰。
    return {"start_new_session": True}


# ---------------------------------------------------------------------------
# 子进程环境白名单（P0.1：不再把父进程整份环境泄给 agent 子进程）
# ---------------------------------------------------------------------------

# 命中即剔除的敏感变量名模式（大小写不敏感、子串匹配）。
# 刻意【不】含会误伤的子串：例如绝不用 "PAT"（会命中 PATH）。
_SENSITIVE_ENV_PATTERNS = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "APIKEY",
)

# 始终透传的、子进程启动/联网【必需】的变量名。Windows 上按大小写不敏感匹配，
# 所以同一变量只列一次即可。清单刻意偏宽——allow-list 太严会让 codex/claude 起不来；
# 还缺什么用 CC_BRIDGE_ENV_PASSTHROUGH 一行补，不必改代码。
_BASE_ENV_ALLOW = (
    # 通用 / POSIX
    "PATH", "PATHEXT", "HOME", "SHELL", "USER", "LOGNAME",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
    "TEMP", "TMP", "TMPDIR", "XDG_RUNTIME_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
    # Windows 启动必需
    "SystemRoot", "windir", "SystemDrive", "ComSpec", "OS", "COMPUTERNAME",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432",
    "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL", "PROCESSOR_REVISION",
    "USERNAME", "USERPROFILE", "USERDOMAIN", "USERDOMAIN_ROAMINGPROFILE",
    "HOMEDRIVE", "HOMEPATH", "LOGONSERVER", "SESSIONNAME", "DriverData",
    "APPDATA", "LOCALAPPDATA", "ProgramData", "ALLUSERSPROFILE", "PUBLIC",
    "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
    "CommonProgramFiles", "CommonProgramFiles(x86)", "CommonProgramW6432",
    # node / npm（codex / claude CLI 多为 node 实现）
    "NODE_OPTIONS", "NODE_PATH", "NODE_EXTRA_CA_CERTS",
    "NPM_CONFIG_PREFIX", "NVM_DIR", "NVM_HOME", "NVM_SYMLINK", "FNM_DIR",
    # 代理（很多环境靠它联网；POSIX 上大小写都可能出现）
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    # agent CLI 的配置目录（是路径，不是密钥）
    "CODEX_HOME",
)

_ENV_PASSTHROUGH_ENV = "CC_BRIDGE_ENV_PASSTHROUGH"


def _is_sensitive_env_name(name: str) -> bool:
    upper = name.upper()
    return any(pat in upper for pat in _SENSITIVE_ENV_PATTERNS)


def _env_passthrough_names() -> set[str]:
    raw = os.environ.get(_ENV_PASSTHROUGH_ENV, "")
    return {p.strip() for p in raw.split(os.pathsep) if p.strip()}


def build_child_env(extra_env: dict | None = None) -> dict:
    """构造交给 agent 子进程的环境：allow-list，而非 ``os.environ.copy()`` 全量（P0.1）。

    被调用的 agent 是潜在的"嫌疑人"（可能被仓库内容里的注入带偏）。把父进程整份环境
    （可能含桌面应用注入的 API key / token）原样交给它，等于把验票章一起递过去。这里改为：

    - 只透传 :data:`_BASE_ENV_ALLOW` 里"启动/联网必需"的变量，外加用户经
      ``CC_BRIDGE_ENV_PASSTHROUGH`` 显式追加的名字；
    - 无论是否在白名单或 passthrough，命中敏感模式（KEY/TOKEN/SECRET/...）的一律剔除；
    - ``extra_env`` 是调用方（桥自身）显式、可信的追加，最后覆盖、且不受敏感剔除约束
      （未来的链路 depth/chain 令牌走这条）。

    注意：这是 allow-list，太严会让子进程起不来。白名单偏宽；真实启动验证属集成测试范畴，
    缺变量时用 ``CC_BRIDGE_ENV_PASSTHROUGH`` 补即可。
    """
    allow = set(_BASE_ENV_ALLOW) | _env_passthrough_names()
    allow_cmp = {n.upper() for n in allow} if IS_WINDOWS else allow

    env: dict[str, str] = {}
    for name, value in os.environ.items():
        key = name.upper() if IS_WINDOWS else name
        if key not in allow_cmp:
            continue
        if _is_sensitive_env_name(name):
            continue
        env[name] = value

    if extra_env:
        env.update(extra_env)
    return env


# ---------------------------------------------------------------------------
# 硬化的短命令执行（在 MCP server 里跑 git / --version 等辅助命令的安全姿势）
# ---------------------------------------------------------------------------

@dataclass
class CapturedRun:
    """一次硬化短命令执行的结果。``stdout/stderr`` 是 bytes，由调用方自行解码。"""

    returncode: int | None     # None = 启动失败 或 超时（配合 timed_out 区分）
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False


# git 辅助命令的硬化环境：杜绝交互卡死。
# - GIT_TERMINAL_PROMPT=0：缺凭据时直接失败，不在终端等输入；
# - GCM_INTERACTIVE=Never：禁掉 Git Credential Manager 弹窗（本机系统 gitconfig 配了 manager）；
# - GIT_OPTIONAL_LOCKS=0：只读探测不抢 index.lock，避免与并发 git 争锁。
_GIT_SAFE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "Never",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_CONFIG_NOSYSTEM": "1",  # 忽略系统级 gitconfig(供应链 / 共享机器硬化)
}

# 桥自己的 git 调用全部叠加这组硬化标志,中和「agent 写 .git/config 或 .gitattributes
# 后,桥的 git 调用替它执行代码」的通道(hooks / pager / 凭据助手 / ssh)。注意:这【不是】
# 完整封堵——repo 本地 .git/config 无法被 git 忽略;真正承重的防御是「任何 .git/ 写入判
# critical」(见 scope.is_dotgit_path)+ PR4 直接哈希磁盘原始字节(绕开会应用 filter 的命令)。
_GIT_HARDENING_FLAGS = [
    "--no-pager",
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=" + os.devnull,
    "-c", "core.askpass=",
    "-c", "core.sshCommand=false",
]


def _kill_proc_tree(proc: "subprocess.Popen") -> None:
    """同步地杀掉整棵进程树（Windows 用 taskkill /T，POSIX 用 killpg）。"""
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=5,
                **subprocess_creation_kwargs(),
            )
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_capture(
    argv: list[str], *, timeout: int, extra_env: dict | None = None
) -> CapturedRun:
    """像 ``subprocess.run(capture_output=True, timeout=...)`` 那样跑一条短命令，
    但在 Windows 上【真正有界、绝不挂死】：

    1. **stdin=DEVNULL**：子进程绝不继承父进程的 stdin。在 MCP stdio server 里这至关重要——
       父进程的 stdin 是 JSON-RPC 管道，子命令（git / 凭据助手 / pager）一旦读它就会
       永久阻塞，甚至吞掉 MCP 协议字节。
    2. **超时用 Popen + communicate(timeout)，超时后杀【整棵进程树】**：绕开 CPython 在
       Windows 上 ``subprocess.run`` 超时后第二次【无超时】``communicate()`` 因孙进程
       （fsmonitor 守护、凭据管理器等）持管道、读不到 EOF 而永久挂死的已知缺陷。杀掉
       整树后管道才会 EOF，再加一层有界 communicate 兜底。

    返回 :class:`CapturedRun`；启动失败 -> returncode=None & timed_out=False，
    超时 -> returncode=None & timed_out=True。
    """
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            **subprocess_creation_kwargs(),
        )
    except (OSError, ValueError):
        return CapturedRun(returncode=None)
    try:
        out, err = proc.communicate(timeout=timeout)
        return CapturedRun(proc.returncode, out or b"", err or b"", False)
    except subprocess.TimeoutExpired:
        _kill_proc_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
        return CapturedRun(returncode=None, stdout=out or b"", stderr=err or b"", timed_out=True)


def git_capture(git: str, cwd: str, args: list[str], *, timeout: int) -> CapturedRun:
    """跑一条只读 git 探测命令（rev-parse / status 等），叠加 git 专属硬化。

    - ``-c core.fsmonitor=false``：不触发 ``git fsmonitor--daemon``——那个长寿守护进程会
      继承并持有管道句柄，正是 Windows ``subprocess.run`` 超时死锁的孙进程来源；
    - 配 :data:`_GIT_SAFE_ENV`：禁交互凭据/锁争用。
    """
    argv = [git, *_GIT_HARDENING_FLAGS, "-C", str(cwd), *args]
    return run_capture(argv, timeout=timeout, extra_env=_GIT_SAFE_ENV)


def debug_log(msg: str) -> None:
    """设置了 ``CC_BRIDGE_DEBUG`` 时，把一行带阶段标记的诊断写到 **stderr**。

    刻意只写 stderr：MCP stdio server 的 stdout 是 JSON-RPC 协议通道，绝不能污染；
    stderr 会被宿主（Claude / Codex）收进各自的 MCP 日志，便于定位「卡在哪个阶段」。
    """
    if not os.environ.get("CC_BRIDGE_DEBUG"):
        return
    try:
        if sys.stderr is not None:
            print(f"[cc-bridge] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 配置文件路径
# ---------------------------------------------------------------------------

def claude_desktop_config_path() -> Path:
    """Claude **桌面版** 的 MCP 配置文件路径（``claude_desktop_config.json``）.

    注意：这是桌面应用的配置，不是 Claude Code CLI 的配置。
    """
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "Claude" / "claude_desktop_config.json"
    if IS_MACOS:
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    # Linux / 其他
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "Claude" / "claude_desktop_config.json"


def codex_home() -> Path:
    """Codex 的配置目录（默认 ``~/.codex``，可被 ``CODEX_HOME`` 覆盖）."""
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env)
    return Path.home() / ".codex"


def codex_config_path() -> Path:
    """Codex 的 ``config.toml`` 路径（MCP server 注册在这里）."""
    return codex_home() / "config.toml"


def codex_auth_path() -> Path:
    """Codex 登录凭证文件路径，用来判断是否已用 ChatGPT 账号登录."""
    return codex_home() / "auth.json"


# ---------------------------------------------------------------------------
# MCP 注册命令
# ---------------------------------------------------------------------------

def stable_app_dir() -> Path:
    """cc-bridge 自己的持久化目录.

    打包(frozen)分发时，安装器是个临时的可执行文件，可能被用户随手删除。
    桌面应用却要在安装器退出后、每次自己启动时反复拉起 MCP server，所以需要把
    可执行文件放到一个固定、持久的位置。
    """
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif IS_MACOS:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "cc-bridge"


def _find_python_with_cc_bridge() -> str | None:
    """在 PATH 上找一个能 ``import cc_bridge`` 的 Python 解释器。"""
    for name in ("python", "python3"):
        exe = shutil.which(name)
        if not exe:
            continue
        if run_capture([exe, "-c", "import cc_bridge"], timeout=20).returncode == 0:
            return exe
    return None


def _is_bundled_executable(src: Path) -> bool:
    """判断 frozen 可执行文件是否依赖同目录资源（PyInstaller onedir / macOS .app）。

    这类可执行文件不能单独拷出去——它依赖同目录的 ``_internal`` / ``Frameworks`` 等。
    onefile 则是自包含的，可以安全复制。
    """
    if (src.parent / "_internal").exists():  # PyInstaller 6+ onedir 布局
        return True
    return ".app/Contents/" in str(src).replace("\\", "/")  # macOS .app bundle


def _ensure_frozen_launcher() -> str:
    """打包运行时：返回一个【持久可用】的可执行文件路径用于注册 MCP server。

    - onefile（自包含）：复制到持久目录，这样即便用户删掉安装包，桌面应用仍能拉起。
    - onedir / .app bundle：可执行文件依赖同目录资源，单独复制会破坏依赖，因此就地用其
      绝对路径注册（代价是：用户不能移动/删除安装目录）。
    """
    src = Path(sys.executable)
    if _is_bundled_executable(src):
        # 就地注册，避免拷出后丢失 .app/_internal 依赖导致启动失败。
        return str(src)
    target_dir = stable_app_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / src.name
        if _should_refresh_launcher(src, target):
            shutil.copy2(src, target)
        return str(target)
    except OSError:
        # 复制失败就退回安装包本身（至少它还在原地时可用）。
        return str(src)


def _should_refresh_launcher(src: Path, target: Path) -> bool:
    """持久目录里的 launcher 是否需要从 ``src`` 重新复制。

    目标不存在、或与源【大小不同 / 源更新】时才刷新。仅按文件大小判断会漏掉
    “同样大小的新版本”（重新打包后 exe 体积常常不变），导致升级后持久目录里一直是
    旧 exe（本项目就需要避免这种版本错配）。叠加 mtime：源比目标新就刷新；``copy2``
    会一并复制 mtime，故复制后两者相等、后续幂等不重复复制。容忍 2s 文件系统时间精度。
    """
    if not target.exists():
        return True
    try:
        s, t = src.stat(), target.stat()
    except OSError:
        return True
    if s.st_size != t.st_size:
        return True
    return s.st_mtime > t.st_mtime + 2


def mcp_launch_command(
    module: str,
    console_script: str | None = None,
    server_key: str | None = None,
) -> tuple[str, list[str]]:
    """返回注册 MCP server 时要写入配置的 ``(command, args)``.

    关键要求：桌面应用会在 **安装器退出之后**、每次自身启动时用这条命令把
    stdio server 拉起来，所以命令必须长期有效、可独立运行。决策分两种运行形态：

    - **打包(frozen)运行**：bundle 内的代码才是权威。优先注册
      ``<exe> --mcp-server <key>`` 自拉起（由 :func:`cc_bridge.installer.main.main`
      解析该标志转去跑对应 server）——**刻意不先用 PATH 上的 console-script**，否则机器上
      残留的旧 ``cc-bridge-mcp-*``（指向旧版本 cc_bridge）会把新安装包的注册抢过去，
      造成版本错配。``<exe>`` 由 :func:`_ensure_frozen_launcher` 决定：onefile 复制到持久
      目录、删掉安装包也可用；onedir / ``.app`` 则就地用绝对路径。仅当没有 ``server_key``
      （理论上不该发生）时，才退回 console-script / ``python -m``；都不可得则抛
      :class:`RuntimeError`，而不是写入一条根本拉不起来的命令。
    - **普通解释器（开发 / pip 源码安装）**：PATH 上的 console-script 入口最稳，优先用它
      （``args=[]``）；没有则退回 ``sys.executable -m module``。

    Args:
        module: 形如 ``cc_bridge.bridge.mcp_to_codex`` 的模块路径。
        console_script: 对应的 console-script 名（如 ``cc-bridge-mcp-codex``）。
        server_key: frozen 自拉起时用的标识（``codex`` / ``claude``）。
    """
    if getattr(sys, "frozen", False):
        if server_key:
            return _ensure_frozen_launcher(), ["--mcp-server", server_key]
        # frozen 但没拿到 server_key（不正常）：尽力退回 console-script / python。
        if console_script:
            exe = resolve_cli(console_script)
            if exe:
                return exe, []
        py = _find_python_with_cc_bridge()
        if py:
            return py, ["-m", module]
        raise RuntimeError(
            "打包运行下无法确定可拉起 MCP server 的命令："
            "PATH 上没有 cc-bridge-mcp-* 入口，也没有能 import cc_bridge 的 python。"
            "建议改用 `pip install cc-bridge` 再运行 `cc-bridge install`。"
        )

    if console_script:
        exe = resolve_cli(console_script)
        if exe:
            return exe, []
    return sys.executable, ["-m", module]


# ---------------------------------------------------------------------------
# 运行期配置（支持环境变量覆盖）
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass
class BridgeConfig:
    """跨 agent 调用的运行期配置。所有字段都可用环境变量覆盖。"""

    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    # Codex sandbox 策略：read-only / workspace-write / danger-full-access。
    # 默认 workspace-write：允许 Codex 修改当前项目文件，但仍受沙箱约束。
    codex_sandbox: str = "workspace-write"
    # Claude 非交互模式的权限策略，让它能在 headless 下自主操作。
    claude_permission_mode: str = "bypassPermissions"
    codex_model: str | None = None
    claude_model: str | None = None

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        return cls(
            timeout_seconds=_env_int("CC_BRIDGE_TIMEOUT", DEFAULT_TIMEOUT_SECONDS),
            max_output_chars=_env_int("CC_BRIDGE_MAX_OUTPUT", DEFAULT_MAX_OUTPUT_CHARS),
            codex_sandbox=os.environ.get("CC_BRIDGE_CODEX_SANDBOX", "workspace-write"),
            claude_permission_mode=os.environ.get(
                "CC_BRIDGE_CLAUDE_PERMISSION", "bypassPermissions"
            ),
            codex_model=os.environ.get("CC_BRIDGE_CODEX_MODEL") or None,
            claude_model=os.environ.get("CC_BRIDGE_CLAUDE_MODEL") or None,
        )
