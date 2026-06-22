"""mcp_selftest 单测：用本地 fake server 真实验证 MCP 握手逻辑（不依赖 21MB exe）。

覆盖：
- 握手成功（工具齐全）/ windowed 式过早退出 / 工具缺失 三种 server 行为；
- selftest_server 的 host 映射、未知 key、启动命令解析失败（RuntimeError）路径；
- selftest_server 端到端（monkeypatch 启动命令指向 fake server）。
"""

from __future__ import annotations

import sys

from cc_bridge.bridge import config
from cc_bridge.installer import mcp_selftest

# 一个最小的、会正确应答 initialize + tools/list 的 fake MCP server。
_FAKE_OK = """
import sys, json
def reply(o):
    sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    m = msg.get("method")
    if m == "initialize":
        reply({"jsonrpc":"2.0","id":msg["id"],"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"fake","version":"0"}}})
    elif m == "tools/list":
        reply({"jsonrpc":"2.0","id":msg["id"],"result":{"tools":[{"name":"codex_execute"},{"name":"codex_status"}]}})
"""

# 模拟 --windowed 打包：进程一启动就退出，根本不读 stdin。
_FAKE_DEAD = "import sys\nsys.exit(0)\n"

# 握手成功但只暴露一个工具 → 应判“缺少预期工具”。
_FAKE_MISSING = """
import sys, json
def reply(o):
    sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        reply({"jsonrpc":"2.0","id":msg["id"],"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"fake","version":"0"}}})
    elif msg.get("method") == "tools/list":
        reply({"jsonrpc":"2.0","id":msg["id"],"result":{"tools":[{"name":"codex_execute"}]}})
"""


def _write(tmp_path, body: str):
    p = tmp_path / "fake_server.py"
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_handshake_success(tmp_path):
    argv = [sys.executable, _write(tmp_path, _FAKE_OK)]
    ok, tools, detail = mcp_selftest._handshake(
        argv, {"codex_execute", "codex_status"}, timeout=15
    )
    assert ok is True
    assert tools == {"codex_execute", "codex_status"}
    assert "工具齐全" in detail


def test_handshake_dead_server_is_failure(tmp_path):
    """server 一启动就退出（windowed 式）→ 失败，且不会挂死。"""
    argv = [sys.executable, _write(tmp_path, _FAKE_DEAD)]
    ok, _tools, detail = mcp_selftest._handshake(
        argv, {"codex_execute", "codex_status"}, timeout=5
    )
    assert ok is False
    assert detail  # 有可读说明


def test_handshake_missing_tools_is_failure(tmp_path):
    argv = [sys.executable, _write(tmp_path, _FAKE_MISSING)]
    ok, tools, detail = mcp_selftest._handshake(
        argv, {"codex_execute", "codex_status"}, timeout=15
    )
    assert ok is False
    assert "codex_status" in detail  # 指出缺了哪个
    assert tools == {"codex_execute"}


def test_handshake_unlaunchable_command_is_failure():
    ok, _tools, detail = mcp_selftest._handshake(
        ["this-binary-does-not-exist-cc-bridge"], {"x"}, timeout=3
    )
    assert ok is False
    assert "无法启动" in detail


def test_selftest_server_unknown_key():
    r = mcp_selftest.selftest_server("bogus")
    assert r.success is False
    assert "未知" in r.detail


def test_selftest_server_launch_command_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("打包运行下无法确定启动命令")

    monkeypatch.setattr(config, "mcp_launch_command", _boom)
    r = mcp_selftest.selftest_server("codex")
    assert r.success is False
    assert "无法确定" in r.detail
    assert "Claude 桌面版" in r.host  # host 映射仍正确


def test_selftest_server_end_to_end(monkeypatch, tmp_path):
    """把启动命令指向 fake-ok server，跑通 selftest_server 全流程。"""
    fake = _write(tmp_path, _FAKE_OK)
    monkeypatch.setattr(
        config, "mcp_launch_command", lambda *a, **k: (sys.executable, [fake])
    )
    r = mcp_selftest.selftest_server("codex", timeout=15)
    assert r.success is True
    assert set(r.tools_found) == {"codex_execute", "codex_status"}
    assert r.duration_seconds >= 0.0


def test_host_mapping_directions():
    """codex server 装进 Claude 桌面版，claude server 装进 Codex（方向交叉，别搞反）。"""
    assert "Claude 桌面版" in mcp_selftest._SPECS["codex"][4]
    assert mcp_selftest._SPECS["codex"][3] == {"codex_execute", "codex_status"}
    assert mcp_selftest._SPECS["claude"][4].startswith("Codex")
    assert mcp_selftest._SPECS["claude"][3] == {"claude_analyze", "claude_status"}
