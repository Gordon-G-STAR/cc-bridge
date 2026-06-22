"""共享 fixture / 测试工具。

cc_bridge 已经 editable 安装，测试里直接 import 即可。这里只放跨多个测试
文件复用的小工具：构造 ExecutionResult、伪造 _run 等。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from cc_bridge.bridge.executor import ExecutionResult


@pytest.fixture
def tmp_path():
    """Windows 沙箱下 pytest 的 0o700 临时目录不可遍历，改用项目内可写目录。"""
    base = Path(__file__).resolve().parents[1] / ".pytest-tmp" / "tmp_path"
    base.mkdir(mode=0o777, parents=True, exist_ok=True)
    path = base / uuid.uuid4().hex
    path.mkdir(mode=0o777)
    return path


@pytest.fixture
def make_execution_result():
    """工厂 fixture：用合理默认值快速造一个 ExecutionResult。

    只需覆盖关心的字段，其余走默认，避免每个用例都写一长串构造参数。
    """

    def _make(**overrides) -> ExecutionResult:
        defaults = dict(
            success=True,
            output="ok",
            files_changed=[],
            error=None,
            duration_seconds=1.0,
            token_usage=None,
            exit_code=0,
            timed_out=False,
            raw_stdout="",
            raw_stderr="",
        )
        defaults.update(overrides)
        return ExecutionResult(**defaults)

    return _make


class FakeRun:
    """可被 monkeypatch 进 executor._run 的假实现。

    记录最后一次调用参数，返回预设的 (stdout, stderr, returncode, timed_out)。
    """

    def __init__(self, stdout="", stderr="", returncode=0, timed_out=False):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timed_out = timed_out
        self.calls: list[dict] = []

    async def __call__(
        self, argv, *, cwd, stdin_text, timeout, extra_env=None, on_stdout_chunk=None
    ):
        self.calls.append(
            dict(
                argv=argv,
                cwd=cwd,
                stdin_text=stdin_text,
                timeout=timeout,
                extra_env=extra_env,
                on_stdout_chunk=on_stdout_chunk,
            )
        )
        return self.stdout, self.stderr, self.returncode, self.timed_out


@pytest.fixture
def fake_run_factory():
    """返回一个构造 FakeRun 的工厂，方便每个用例定制返回值。"""
    return FakeRun
