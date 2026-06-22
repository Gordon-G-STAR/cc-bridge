"""安装期 MCP 启动自检：把【真正注册给宿主的启动命令】拉起来，做一次 MCP 握手。

为什么必须有它（连通性测试不够用）
----------------------------------
:mod:`cc_bridge.installer.tester` 的连通性测试走的是【进程内】的
:class:`~cc_bridge.bridge.executor.AgentExecutor`，验证的是「桥能不能调到对方
CLI」。它【完全不会】启动 ``<launcher> --mcp-server <key>`` 这条**宿主真正使用**
的 stdio server 路径。历史上正是这条路径被 PyInstaller ``--windowed`` 打包打挂
（stdin/stdout 变成 ``None``，server 一启动就退出，宿主侧表现为 “Server
disconnected”），而连通性测试照样全绿、安装器报“一切正常”——盲区就在这里。

本模块补上这块盲区：用和 configurator 完全一致的方式解析出启动命令
（:func:`cc_bridge.bridge.config.mcp_launch_command`），把它 spawn 起来，发
MCP ``initialize`` + ``tools/list``，确认能在超时内列出预期工具。任何打包模式、
PATH、依赖缺失导致 server 起不来，都会在安装时被【当场抓出】，而不是等用户在
Claude/Codex 里发现工具消失才回头排查。

实现刻意【不依赖 mcp.client】
----------------------------
frozen 安装器只打包了 ``mcp.server`` 子树（见 build_exe.py 的
``--collect-submodules mcp.server``），``mcp.client`` 不在内。所以这里用最小的
**换行分隔 JSON-RPC** 手写握手——既能在 frozen exe 里直接跑，也顺带规避了
PyInstaller onefile “父 stub + 真子进程” 的清理坑（用进程树终止收尾）。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field

from cc_bridge.bridge import config

# server_key -> (模块路径, console-script 名, frozen 自拉起 key, 预期工具集, 注册到哪个宿主)
# 与 configurator.py 的常量一一对应；注意方向是“交叉”的：
#   - mcp_to_codex 暴露 codex_* 工具，装进 Claude 桌面版（让 Claude 调 Codex）；
#   - mcp_to_claude 暴露 claude_* 工具，装进 Codex（让 Codex 调 Claude）。
_SPECS: dict[str, tuple[str, str, str, set[str], str]] = {
    "codex": (
        "cc_bridge.bridge.mcp_to_codex",
        "cc-bridge-mcp-codex",
        "codex",
        {"codex_execute", "codex_status"},
        "Claude 桌面版（bridge-to-codex，供 Claude 调 Codex）",
    ),
    "claude": (
        "cc_bridge.bridge.mcp_to_claude",
        "cc-bridge-mcp-claude",
        "claude",
        {"claude_analyze", "claude_status"},
        "Codex（bridge-to-claude，供 Codex 调 Claude）",
    ),
}

_DEFAULT_TIMEOUT = 30.0  # 给 21MB onefile 冷启动 + 握手留足余量（实测约 2~3s）


@dataclass
class McpSelfTestResult:
    """一次 MCP 启动自检的结果，字段直接用于安装界面 / CLI 展示。"""

    server_key: str                 # "codex" / "claude"
    host: str                       # 该 server 注册到哪个宿主（人类可读）
    success: bool
    detail: str                     # 成功 / 失败的人类可读说明
    duration_seconds: float = 0.0
    tools_found: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 进程树终止（onefile：父 stub 之外还有真正的 server 子进程）
# ---------------------------------------------------------------------------

def _kill_tree(pid: int) -> None:
    try:
        if config.IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                **config.subprocess_creation_kwargs(),
            )
        else:
            # spawn 时 start_new_session=True（见 config.subprocess_creation_kwargs），
            # 子进程独立成组，可整组发信号。
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 最小 MCP stdio 握手
# ---------------------------------------------------------------------------

def _handshake(
    argv: list[str], expected_tools: set[str], timeout: float
) -> tuple[bool, set[str], str]:
    """spawn ``argv``，做 MCP initialize + tools/list。

    返回 ``(是否拿到全部预期工具, 实际工具集, 人类可读说明)``。无论成功失败，
    都会把整棵进程树杀干净，绝不留下后台 server 继续烧额度。
    """
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **config.subprocess_creation_kwargs(),
        )
    except OSError as exc:
        return False, set(), f"无法启动 MCP server（{exc}）。命令：{argv}"

    out_lines: list[bytes] = []
    err_chunks: list[bytes] = []

    def _rd_out() -> None:
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                out_lines.append(raw)
        except Exception:
            pass

    def _rd_err() -> None:
        try:
            assert proc.stderr is not None
            for raw in iter(lambda: proc.stderr.read(4096), b""):
                err_chunks.append(raw)
        except Exception:
            pass

    threading.Thread(target=_rd_out, daemon=True).start()
    threading.Thread(target=_rd_err, daemon=True).start()

    msgs = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "cc-bridge-selftest", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    try:
        assert proc.stdin is not None
        for m in msgs:
            proc.stdin.write((json.dumps(m) + "\n").encode("utf-8"))
            proc.stdin.flush()
    except OSError as exc:
        _kill_tree(proc.pid)
        return False, set(), (
            f"MCP server 启动后过早退出（写 stdin 失败：{exc}）。"
            "最常见原因是该可执行文件被以 --windowed/no-console 模式打包，"
            "导致 stdin/stdout 不可用——请用 console 模式重新打包（见 build_exe.py）。"
        )

    tools: set[str] = set()
    got_tools = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        for raw in list(out_lines):  # 快照，避免读线程并发改动
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            res = obj.get("result") if isinstance(obj, dict) else None
            if isinstance(res, dict) and isinstance(res.get("tools"), list):
                for t in res["tools"]:
                    if isinstance(t, dict) and isinstance(t.get("name"), str):
                        tools.add(t["name"])
                got_tools = True
        if got_tools:
            break
        time.sleep(0.1)

    exited = proc.poll() is not None
    # 先关 stdin 送 EOF：FastMCP stdio server 收到 EOF 会【自行优雅退出】，
    # 这样即便是 onefile（父 stub + 子 server）也能干净收尾，避免 _kill_tree
    # 在 stub 刚 fork 出子进程的瞬间漏杀、留下孤儿 server 后台烧额度。
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except Exception:
        pass
    # 兜底：EOF 后仍没退出，才动用进程树终止。
    if proc.poll() is None:
        _kill_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    if not got_tools:
        err = b"".join(err_chunks).decode("utf-8", errors="replace").strip()
        if exited and not out_lines:
            hint = (
                "server 还没完成 MCP 握手就退出了"
                "（疑似 --windowed/no-console 打包导致 stdin/stdout 不可用）"
            )
        elif exited:
            hint = "server 在握手中途退出"
        else:
            hint = f"在 {timeout:.0f}s 内未收到 tools/list 响应"
        tail = f"；stderr：{err[-300:]}" if err else ""
        return False, tools, f"MCP 握手失败：{hint}{tail}"

    missing = expected_tools - tools
    if missing:
        return False, tools, (
            f"MCP 握手成功但缺少预期工具：{sorted(missing)}"
            f"（实际暴露：{sorted(tools)}）"
        )
    return True, tools, f"MCP 握手正常，工具齐全：{sorted(tools)}"


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def selftest_server(server_key: str, timeout: float = _DEFAULT_TIMEOUT) -> McpSelfTestResult:
    """对单个 server（``"codex"`` / ``"claude"``）做启动自检。"""
    spec = _SPECS.get(server_key)
    if spec is None:
        return McpSelfTestResult(
            server_key=server_key,
            host="?",
            success=False,
            detail=f"未知的 server key：{server_key!r}（应为 codex / claude）。",
        )
    module, script, frozen_key, expected, host = spec

    try:
        command, args = config.mcp_launch_command(module, script, frozen_key)
    except RuntimeError as exc:
        return McpSelfTestResult(
            server_key=server_key,
            host=host,
            success=False,
            detail=f"无法确定 MCP server 启动命令：{exc}",
        )

    argv = [command, *args]
    start = time.monotonic()
    ok, tools, detail = _handshake(argv, expected, timeout)
    return McpSelfTestResult(
        server_key=server_key,
        host=host,
        success=ok,
        detail=detail,
        duration_seconds=time.monotonic() - start,
        tools_found=sorted(tools),
    )


def selftest_all(timeout: float = _DEFAULT_TIMEOUT) -> list[McpSelfTestResult]:
    """按 [codex, claude] 顺序对两个 server 都做启动自检。"""
    return [selftest_server("codex", timeout), selftest_server("claude", timeout)]
