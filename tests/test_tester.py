"""ConnectivityTester 离线单测（不真实调用 CLI）.

补上审查指出的盲区：连通性测试的成功判定、read-only 沙箱与 120s 超时覆盖、
方向文案，都用 async stub 替换 executor 后离线验证。
"""

from __future__ import annotations

import asyncio
import threading
import time

from cc_bridge.bridge.executor import AgentExecutor, ExecutionResult
from cc_bridge.installer.tester import ConnectivityTester


def _stub(monkeypatch, codex_result, claude_result):
    async def _run_codex(self, prompt, cwd, timeout=None):
        return codex_result

    async def _run_claude(self, prompt, cwd, timeout=None):
        return claude_result

    monkeypatch.setattr(AgentExecutor, "run_codex", _run_codex)
    monkeypatch.setattr(AgentExecutor, "run_claude", _run_claude)


def test_tester_uses_readonly_sandbox_and_short_timeout():
    t = ConnectivityTester()
    assert t.executor.cfg.codex_sandbox == "read-only"
    assert t.executor.cfg.timeout_seconds == 120


def test_run_all_both_directions_success(monkeypatch):
    _stub(
        monkeypatch,
        ExecutionResult(success=True, output="OK", duration_seconds=1.0),
        ExecutionResult(success=True, output="OK", duration_seconds=2.0),
    )
    outcomes = ConnectivityTester().run_all()
    assert [o.direction for o in outcomes] == ["Claude → Codex", "Codex → Claude"]
    assert all(o.success for o in outcomes)
    assert "正常响应" in outcomes[0].detail


def test_empty_output_counts_as_failure(monkeypatch):
    # 即便 success=True，但 output 为空 → 视为没真正打通。
    _stub(
        monkeypatch,
        ExecutionResult(success=True, output="   ", duration_seconds=1.0),
        ExecutionResult(success=False, output="", error="未登录", duration_seconds=0.5),
    )
    outcomes = ConnectivityTester().run_all()
    assert outcomes[0].success is False
    assert outcomes[1].success is False
    assert outcomes[1].detail == "未登录"


def test_cancel_interrupts_run(monkeypatch):
    """cancel() 能中途打断正在跑的连通性测试（GUI 跳过/关窗的核心诉求）。"""
    started = threading.Event()

    async def slow(self, prompt, cwd, timeout=None):
        started.set()
        await asyncio.sleep(30)  # 模拟长时间运行、永不自然结束
        return ExecutionResult(success=True, output="OK")

    monkeypatch.setattr(AgentExecutor, "run_codex", slow)
    monkeypatch.setattr(AgentExecutor, "run_claude", slow)

    tester = ConnectivityTester()
    box = {}

    def worker():
        box["result"] = tester.run_all_cancellable()

    th = threading.Thread(target=worker)
    th.start()
    assert started.wait(5), "连通性测试未能启动"
    time.sleep(0.05)

    tester.cancel()
    th.join(timeout=5)

    assert not th.is_alive(), "cancel() 后后台线程未及时结束"
    assert box["result"] is None  # 被取消 → 返回 None
