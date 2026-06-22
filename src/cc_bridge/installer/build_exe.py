"""把 GUI 安装器打包成 Windows 单文件 exe（PyInstaller）。

用法（在 Windows 上、项目根目录下）::

    # 1) 先以可编辑方式安装本包并带上打包依赖（含 pyinstaller）
    pip install -e .[build]

    # 2) 运行本脚本，产物落在 <项目根>/dist/cc-bridge-installer.exe
    python -m cc_bridge.installer.build_exe

⚠️ **构建用的 Python 必须是 python.org 官方版（或其 venv），不要用 Anaconda/conda 的 Python。**
实测：用 Anaconda 的 Python 构建时，PyInstaller 解析不到 conda 的 tcl/tk 运行库
（``tk86t.dll`` / ``tcl86t.dll``），打出来的 exe 里 ``import _tkinter`` 会 “DLL load failed”，
GUI 起不来（会回退到 CLI）。python.org 的 Python 自带可被 PyInstaller 正确打包的 tcl/tk。
注意：MCP server 子命令（``--mcp-server``）不依赖 tkinter，即便用 conda 构建也能跑；
受影响的只有图形安装向导。

说明：
- 入口是本包的 GUI 安装器 ``cc_bridge/installer/main.py``（tkinter）。
- 打成 ``--onefile`` **console 模式** 的单文件 exe，产物名 ``cc-bridge-installer``。

  **为什么是 console 而非 --windowed**：这同一个 exe 在 frozen 安装后会被注册为
  ``<exe> --mcp-server <key>`` 给桌面应用反复拉起，充当 **MCP stdio server**。
  PyInstaller 的 ``--windowed``/no-console 模式下 ``sys.stdin/stdout/stderr`` 为 ``None``，
  stdio server 无法通信（PyInstaller 已知问题）。所以必须用 console 模式保证 stdio 可用；
  GUI 启动时由 ``main.py`` 在 Windows 上把控制台窗口隐藏掉，用户看不到黑窗。
- 用 ``--collect-submodules mcp.server`` + ``--collect-data mcp`` 收集 MCP SDK 的 server 子树
  （**不要用 ``--collect-all mcp``**：那会强行 import ``mcp.cli``，而它依赖可选的 typer/rich，
  未安装时收集阶段直接失败）；用 ``--collect-submodules cc_bridge`` 确保本包内通过字符串动态
  引用的模块也被打包。
- dist / build / spec 全部放到 ``<项目根>/dist`` 下，避免污染源码目录。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

# 产物名（同时是 exe 文件名的主干）。
APP_NAME = "cc-bridge-installer"

# GUI 安装器入口模块（属于本包，由安装器 agent 提供 main()）。
ENTRY_MODULE = "cc_bridge.installer.main"


def _entry_script() -> Path:
    """推断 GUI 安装器入口脚本的绝对路径。

    优先用 importlib 定位已安装的模块；找不到时退回到「相对本文件」的路径推断，
    这样即使包还没装好也能在源码树里跑通。
    """
    spec = importlib.util.find_spec(ENTRY_MODULE)
    if spec is not None and spec.origin and Path(spec.origin).exists():
        return Path(spec.origin).resolve()
    # 退回：本文件位于 .../cc_bridge/installer/build_exe.py，main.py 是同目录邻居。
    fallback = Path(__file__).resolve().parent / "main.py"
    return fallback


def _project_root() -> Path:
    """项目根目录 = .../src/cc_bridge/installer 往上三级。"""
    return Path(__file__).resolve().parents[3]


def _pyinstaller_available() -> bool:
    return importlib.util.find_spec("PyInstaller") is not None


def main() -> None:
    if sys.platform != "win32":
        # 本脚本面向 Windows；其它平台请用 build_dmg.sh（macOS）。
        print(
            "warning: build_exe.py 设计用于 Windows。"
            "当前平台不是 win32，生成的产物可能不是 .exe。",
            file=sys.stderr,
        )

    if not _pyinstaller_available():
        print(
            "error: 未检测到 PyInstaller。请先安装打包依赖：\n"
            "    pip install -e .[build]\n"
            "然后重新运行： python -m cc_bridge.installer.build_exe",
            file=sys.stderr,
        )
        sys.exit(1)

    entry = _entry_script()
    if not entry.exists():
        print(
            f"error: 找不到 GUI 安装器入口脚本：{entry}\n"
            f"（期望模块 {ENTRY_MODULE} 存在并指向 installer/main.py）",
            file=sys.stderr,
        )
        sys.exit(1)

    root = _project_root()
    dist_dir = root / "dist"
    build_dir = dist_dir / "build"
    spec_dir = dist_dir / "spec"
    for d in (dist_dir, build_dir, spec_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 用 `python -m PyInstaller` 而非裸 `pyinstaller`，避免 PATH 里没有脚本入口。
    argv = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        # 故意用 console 模式（不加 --windowed）：见模块 docstring。
        # 同一个 exe 既当 GUI 安装器（启动时隐藏控制台窗口），又当 MCP stdio server（需要可用 stdio）。
        "--console",
        "--name",
        APP_NAME,
        # 只收集 mcp 的 server 子模块 + 数据；不要用 --collect-all mcp：那会强行
        # import mcp.cli，而后者依赖可选的 typer/rich，未安装时会让收集阶段直接失败。
        # 本项目只用 mcp.server.fastmcp，server 子树足矣。
        "--collect-submodules",
        "mcp.server",
        "--collect-data",
        "mcp",
        "--exclude-module",
        "mcp.cli",
        "--collect-submodules",
        "cc_bridge",
        "--noconfirm",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir),
        "--specpath",
        str(spec_dir),
        str(entry),
    ]

    print("running:", " ".join(argv), file=sys.stderr)
    result = subprocess.run(argv)
    if result.returncode != 0:
        print(
            f"error: PyInstaller 退出码 {result.returncode}，打包失败。",
            file=sys.stderr,
        )
        sys.exit(result.returncode)

    exe_path = dist_dir / f"{APP_NAME}.exe"
    print(f"done: 产物位于 {exe_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
