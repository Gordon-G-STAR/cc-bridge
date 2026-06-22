"""安装后的连通性自检：让 Claude 真的去叫一次 Codex、Codex 真的去叫一次 Claude.

警告：这里的方法会 **真实调用对方的 CLI**，会消耗订阅 / API 额度，且每次都要等
CLI 冷启动 + 模型响应（可能十几秒到上百秒）。它们仅供安装器在「检查连通性」时
一次性触发，不要放进任何热路径或循环里。

为安全起见，连通性测试统一用只读沙箱（``codex_sandbox="read-only"``）并把超时压到
120 秒——只要对方能正常回话即视为打通，不需要它真的改文件。
"""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import tempfile
import threading
from dataclasses import dataclass

from cc_bridge.bridge.config import BridgeConfig
from cc_bridge.bridge.executor import AgentExecutor

# 连通性测试的固定参数。
_TEST_TIMEOUT = 120
_TEST_PROMPT = "请只回复两个字符：OK"
_DETAIL_PREVIEW_CHARS = 80


@dataclass
class TestOutcome:
    """一次单向连通性测试的结果，字段直接用于安装界面展示。"""

    direction: str             # "Claude → Codex" / "Codex → Claude"
    success: bool
    detail: str                # 成功 / 失败的人类可读说明
    duration_seconds: float


class ConnectivityTester:
    """逐向触发一次真实跨 agent 调用，验证桥接是否打通。"""

    def __init__(self) -> None:
        # 以环境配置为基线，覆盖沙箱策略与超时，避免连通性测试改动文件或卡太久。
        base = BridgeConfig.from_env()
        cfg = dataclasses.replace(
            base,
            codex_sandbox="read-only",
            timeout_seconds=_TEST_TIMEOUT,
        )
        self.executor = AgentExecutor(cfg)
        # 取消支持：记录正在运行的 loop / task，供其它线程（GUI）调用 cancel()。
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._cancelled = False

    async def test_codex(self) -> TestOutcome:
        """Claude 侧发起，验证能否调到 Codex。"""
        direction = "Claude → Codex"
        tmp = tempfile.mkdtemp()
        try:
            result = await self.executor.run_codex(
                _TEST_PROMPT, tmp, timeout=_TEST_TIMEOUT
            )
            success = result.success and bool(result.output.strip())
            detail = self._detail(success, result, "Codex")
            return TestOutcome(
                direction=direction,
                success=success,
                detail=detail,
                duration_seconds=result.duration_seconds,
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_claude(self) -> TestOutcome:
        """Codex 侧发起，验证能否调到 Claude。"""
        direction = "Codex → Claude"
        tmp = tempfile.mkdtemp()
        try:
            result = await self.executor.run_claude(
                _TEST_PROMPT, tmp, timeout=_TEST_TIMEOUT
            )
            success = result.success and bool(result.output.strip())
            detail = self._detail(success, result, "Claude")
            return TestOutcome(
                direction=direction,
                success=success,
                detail=detail,
                duration_seconds=result.duration_seconds,
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_all(self) -> list:
        """按 [Codex, Claude] 顺序各跑一次，返回两个 TestOutcome。"""
        codex = await self.test_codex()
        claude = await self.test_claude()
        return [codex, claude]

    def run_all(self) -> list:
        """同步包装，方便非 async 的安装器调用（不可取消，给 CLI 用）。"""
        return asyncio.run(self.test_all())

    def run_all_cancellable(self) -> list | None:
        """供 GUI 后台线程调用：在本线程建独立事件循环跑 test_all，并记录 loop/task，
        让其它线程能通过 :meth:`cancel` 中途打断（executor 会借 CancelledError 杀子进程）。

        正常完成返回结果列表；被取消返回 ``None``。
        """
        loop = asyncio.new_event_loop()
        with self._lock:
            if self._cancelled:
                loop.close()
                return None
            self._loop = loop
        try:
            asyncio.set_event_loop(loop)
            task = loop.create_task(self.test_all())
            with self._lock:
                # 处理「task 建好之前就被 cancel」的竞态。
                if self._cancelled:
                    task.cancel()
                self._task = task
            return loop.run_until_complete(task)
        except asyncio.CancelledError:
            return None
        finally:
            with self._lock:
                self._loop = None
                self._task = None
            try:
                asyncio.set_event_loop(None)
            except Exception:
                pass
            loop.close()

    def cancel(self) -> None:
        """从其它线程请求取消正在跑的连通性测试，并尽力终止已启动的子进程。"""
        with self._lock:
            self._cancelled = True
            loop, task = self._loop, self._task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                # loop 已关闭：无事可做。
                pass

    # -- 文案 -------------------------------------------------------------
    @staticmethod
    def _detail(success: bool, result, peer: str) -> str:
        if success:
            preview = result.output.strip()[:_DETAIL_PREVIEW_CHARS]
            return f"{peer} 正常响应：{preview}"
        return (result.error or "无响应").strip() or "无响应"
