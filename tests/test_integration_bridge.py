"""MCP 工具端到端测试。

- 一个 **离线** 用例：codex_execute 在 project_dir 非法时返回友好字符串、绝不抛异常、
  也绝不真正去调用 Codex（覆盖 MCP 工具的错误兜底路径）。
- 一个 **integration** 用例（默认被 `-m "not integration"` 排除）：在真实临时 git 仓库里
  调用一次 Codex，断言它在硬超时内返回、不挂死——这是评审要求的 codex_execute 端到端验收，
  也是 git 探测硬化（config.git_capture）后「不再卡在上下文收集」的回归防线。需要本机已
  安装并登录 Codex CLI。
"""

from __future__ import annotations

import asyncio
import subprocess
import time

import pytest

from cc_bridge.bridge import config, mcp_to_codex


async def test_codex_execute_rejects_bad_dir_without_calling_codex():
    """project_dir 非法 → 返回以「无法调用 Codex」开头的友好串，不抛异常、不调 Codex。"""
    out = await mcp_to_codex.codex_execute("随便做点什么", project_dir=None)
    assert isinstance(out, str)
    assert out.startswith("无法调用 Codex")


@pytest.mark.integration
async def test_codex_execute_end_to_end_does_not_hang(tmp_path):
    """真实调用 Codex：在临时 git 仓库里跑一个极小任务，必须在硬超时内返回（不挂死）。"""
    git = config.resolve_cli("git")
    if git:
        subprocess.run([git, "-C", str(tmp_path), "init"], capture_output=True)

    start = time.monotonic()
    # 略高于单次调用 300s 硬上限；若 git 探测/收尾真卡死，这里会超时失败而非永久挂起。
    out = await asyncio.wait_for(
        mcp_to_codex.codex_execute("只回复 OK，不要修改任何文件。", str(tmp_path)),
        timeout=330,
    )
    assert isinstance(out, str) and out.strip()
    assert time.monotonic() - start < 330
