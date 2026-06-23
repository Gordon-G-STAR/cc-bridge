"""把对方 agent(callee)的进度文本净化成【粗分类】再回传给调用方(PR6)。

callee 的进度文本是【不可信】的:可能含提示注入、终端转义、或诱导调用方的内容。进度 /
info 通道绝不逐字回传 callee 文本——只回一组【固定常量】里的粗分类,既给用户"在动"的反馈,
又不让 callee 控制的字节进入调用方上下文(封掉 #10「进度通道逐字回传」这条第二信任通道)。

两个方向的 MCP server 共用 :func:`make_progress_callback`,避免实现漂移。
"""

from __future__ import annotations

import time

# 回传给调用方的【唯一】可能取值——全是固定常量,绝不含 callee 字节。
RUNNING = "对方 agent 处理中…"
STARTED = "对方 agent 已开始处理"
EXEC_COMMAND = "对方 agent 正在执行命令"
USING_TOOL = "对方 agent 正在使用工具"
PRODUCING = "对方 agent 正在产出内容"
COMPLETED = "对方 agent 已完成处理"
ERRORED = "对方 agent 报告了错误"

# 节流:同类 / 过密的进度不重复回传。
_THROTTLE_SECONDS = 0.25


def coarse_category(label: str | None) -> str:
    """把 executor 生成的(含 callee 文本的)详细标签映射成固定粗分类。

    只【读取】label 做分类判断,但【返回值恒为上面的固定常量】——绝不回传 label 本身。
    故 callee 无论怎样构造文本,都注入不进调用方的进度通道(最坏只是分类不准,无注入)。
    """
    text = "" if label is None else str(label)
    low = text.lower()
    if "错误" in text or "失败" in text or "error" in low:
        return ERRORED
    if "已完成" in text or "completed" in low or "finished" in low:
        return COMPLETED
    if "正在执行" in text or "executing" in low or "running" in low:
        return EXEC_COMMAND
    if "使用工具" in text or "tool" in low:
        return USING_TOOL
    if "已开始" in text or "started" in low or "init" in low:
        return STARTED
    if text.strip():
        return PRODUCING
    return RUNNING


def make_progress_callback(ctx):
    """构造一个把 executor 进度【粗分类后】转成 MCP progress/info 的回调;异常静默降级。

    ``ctx`` 为 None(无 MCP 上下文)时返回 None。回调发出的文本恒为 :func:`coarse_category`
    的固定常量,绝不含被调用方产生的原始文本。
    """
    if ctx is None:
        return None
    state = {"count": 0, "last_message": "", "last_at": 0.0}

    async def _on_progress(message) -> None:
        try:
            text = coarse_category(message)
            if not text or text == state["last_message"]:
                return
            now = time.monotonic()
            if state["count"] and now - state["last_at"] < _THROTTLE_SECONDS:
                return
            state["count"] += 1
            state["last_message"] = text
            state["last_at"] = now
            await ctx.report_progress(
                progress=float(state["count"]), total=None, message=text
            )
            await ctx.info(text)
        except Exception:
            pass

    return _on_progress


__all__ = ["coarse_category", "make_progress_callback"]
