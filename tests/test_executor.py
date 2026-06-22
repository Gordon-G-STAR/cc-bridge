"""AgentExecutor 离线单测。

全部不碰真实 CLI：monkeypatch 掉
- executor._run（async，返回 (stdout, stderr, returncode, timed_out)）
- executor.config.resolve_cli（返回假路径 / None）
- executor._git_status（返回 None，跳过 git 快照）
"""

from __future__ import annotations

import asyncio
import json
import time

from cc_bridge.bridge import executor as ex


# ---------------------------------------------------------------------------
# 通用 monkeypatch 辅助
# ---------------------------------------------------------------------------

def _patch_cli_available(monkeypatch, mapping=None):
    """让 resolve_cli 对 claude/codex 返回假路径，其余（如 git）返回 None。"""
    default = {"claude": "C:/fake/claude.exe", "codex": "C:/fake/codex.exe"}
    if mapping is not None:
        default = mapping
    monkeypatch.setattr(ex.config, "resolve_cli", lambda name: default.get(name))


def _patch_no_git(monkeypatch):
    monkeypatch.setattr(ex, "_git_status", lambda cwd: None)


def _install_fake_run(monkeypatch, fake):
    monkeypatch.setattr(ex, "_run", fake)


# ---------------------------------------------------------------------------
# Claude：JSON 输出解析
# ---------------------------------------------------------------------------

async def test_run_claude_parses_json_result_usage_success(
    monkeypatch, fake_run_factory, tmp_path
):
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    payload = {
        "result": "已重构 foo 模块。",
        "usage": {"input_tokens": 100, "output_tokens": 42},
        "total_cost_usd": 0.0123,
        "is_error": False,
    }
    fake = fake_run_factory(stdout=json.dumps(payload), stderr="", returncode=0)
    _install_fake_run(monkeypatch, fake)

    result = await ex.AgentExecutor().run_claude("做点事", str(tmp_path))

    assert result.success is True
    assert result.output == "已重构 foo 模块。"
    assert result.token_usage is not None
    assert result.token_usage["input_tokens"] == 100
    # total_cost_usd 会被合并进 usage
    assert result.token_usage["total_cost_usd"] == 0.0123
    assert result.exit_code == 0
    assert result.error is None
    # prompt 应通过 stdin 透传
    assert fake.calls[0]["stdin_text"] == "做点事"


async def test_run_claude_is_error_marks_failure(
    monkeypatch, fake_run_factory, tmp_path
):
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    payload = {"result": "出问题了", "is_error": True}
    fake = fake_run_factory(stdout=json.dumps(payload), returncode=0)
    _install_fake_run(monkeypatch, fake)

    result = await ex.AgentExecutor().run_claude("x", str(tmp_path))

    # 即便 returncode==0，is_error=True 也要判为失败
    assert result.success is False
    assert result.output == "出问题了"


async def test_run_claude_non_json_falls_back_to_raw_text(
    monkeypatch, fake_run_factory, tmp_path
):
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    fake = fake_run_factory(stdout="纯文本输出，不是 JSON", returncode=0)
    _install_fake_run(monkeypatch, fake)

    result = await ex.AgentExecutor().run_claude("x", str(tmp_path))

    assert result.success is True
    assert result.output == "纯文本输出，不是 JSON"
    assert result.token_usage is None


# ---------------------------------------------------------------------------
# Codex：stdout 兜底
# ---------------------------------------------------------------------------

async def test_run_codex_falls_back_to_stdout(
    monkeypatch, fake_run_factory, tmp_path
):
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    # codex 没有 -o 最终文件内容时（临时文件为空），应从 stdout 兜底拿文本。
    fake = fake_run_factory(stdout="codex 的最终回答", stderr="", returncode=0)
    _install_fake_run(monkeypatch, fake)

    result = await ex.AgentExecutor().run_codex("分析一下", str(tmp_path))

    assert result.success is True
    assert result.output == "codex 的最终回答"
    assert result.exit_code == 0


async def test_run_codex_extracts_usage_from_jsonl(
    monkeypatch, fake_run_factory, tmp_path
):
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    jsonl = "\n".join(
        [
            json.dumps({"type": "token_count", "usage": {"input_tokens": 7, "output_tokens": 3}}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "完成了任务"},
                }
            ),
        ]
    )
    fake = fake_run_factory(stdout=jsonl, returncode=0)
    _install_fake_run(monkeypatch, fake)

    result = await ex.AgentExecutor().run_codex("x", str(tmp_path))

    assert result.success is True
    assert result.output == "完成了任务"
    assert result.token_usage == {"input_tokens": 7, "output_tokens": 3}


# ---------------------------------------------------------------------------
# 超时
# ---------------------------------------------------------------------------

async def test_run_claude_timeout_sets_failure_and_error(
    monkeypatch, fake_run_factory, tmp_path
):
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    fake = fake_run_factory(stdout="部分结果", returncode=None, timed_out=True)
    _install_fake_run(monkeypatch, fake)

    result = await ex.AgentExecutor().run_claude("x", str(tmp_path), timeout=5)

    assert result.success is False
    assert result.timed_out is True
    assert result.error is not None
    assert "超时" in result.error
    # 超时不是「agent 不可用」（exit_code 虽为 None 但 timed_out 为真）
    assert result.agent_unavailable is False


async def test_run_codex_timeout_sets_failure_and_error(
    monkeypatch, fake_run_factory, tmp_path
):
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    fake = fake_run_factory(stdout="", returncode=None, timed_out=True)
    _install_fake_run(monkeypatch, fake)

    result = await ex.AgentExecutor().run_codex("x", str(tmp_path), timeout=5)

    assert result.success is False
    assert result.timed_out is True
    assert "超时" in (result.error or "")


# ---------------------------------------------------------------------------
# CLI 不可用
# ---------------------------------------------------------------------------

async def test_run_claude_unavailable_when_cli_missing(monkeypatch, tmp_path):
    # resolve_cli 一律返回 None
    monkeypatch.setattr(ex.config, "resolve_cli", lambda name: None)
    _patch_no_git(monkeypatch)

    result = await ex.AgentExecutor().run_claude("x", str(tmp_path))

    assert result.success is False
    assert result.agent_unavailable is True
    assert result.exit_code is None
    assert result.timed_out is False
    assert result.error is not None
    # 含安装/登录提示
    assert "安装" in result.error


async def test_run_codex_unavailable_when_cli_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(ex.config, "resolve_cli", lambda name: None)
    _patch_no_git(monkeypatch)

    result = await ex.AgentExecutor().run_codex("x", str(tmp_path))

    assert result.success is False
    assert result.agent_unavailable is True
    assert result.error is not None
    assert "安装" in result.error


# ---------------------------------------------------------------------------
# 非零退出码
# ---------------------------------------------------------------------------

async def test_run_claude_nonzero_exit_uses_stderr_as_error(
    monkeypatch, fake_run_factory, tmp_path
):
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    fake = fake_run_factory(stdout="", stderr="boom: 出错了", returncode=2)
    _install_fake_run(monkeypatch, fake)

    result = await ex.AgentExecutor().run_claude("x", str(tmp_path))

    assert result.success is False
    assert result.exit_code == 2
    assert result.error == "boom: 出错了"
    # 这是真实退出，不是 agent 不可用
    assert result.agent_unavailable is False


# ---------------------------------------------------------------------------
# argv 契约：codex exec / claude -p，prompt 走 stdin，无过时 flag
# ---------------------------------------------------------------------------

async def test_run_codex_argv_uses_exec_no_legacy_flags(
    monkeypatch, fake_run_factory, tmp_path
):
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    fake = fake_run_factory(stdout="ok", returncode=0)
    _install_fake_run(monkeypatch, fake)

    await ex.AgentExecutor().run_codex("做事", str(tmp_path))
    argv = fake.calls[0]["argv"]

    assert "exec" in argv
    assert "--sandbox" in argv and "workspace-write" in argv
    assert "--skip-git-repo-check" in argv
    assert "--json" in argv and "-o" in argv
    # 过时 / 错误的写法绝不能出现
    for bad in ("-q", "-p", "--full-auto"):
        assert bad not in argv
    # prompt 走 stdin，不进命令行
    assert "做事" not in argv
    assert fake.calls[0]["stdin_text"] == "做事"


async def test_run_codex_argv_respects_sandbox_and_model(
    monkeypatch, fake_run_factory, tmp_path
):
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    fake = fake_run_factory(stdout="ok")
    _install_fake_run(monkeypatch, fake)

    cfg = ex.config.BridgeConfig(codex_sandbox="read-only", codex_model="o3")
    await ex.AgentExecutor(cfg).run_codex("x", str(tmp_path))
    argv = fake.calls[0]["argv"]

    assert "read-only" in argv
    assert "-m" in argv and "o3" in argv


async def test_run_codex_resume_argv_uses_session_and_stdin_dash(
    monkeypatch, fake_run_factory, tmp_path
):
    """续接时使用 codex exec resume <session_id> -，prompt 仍只走 stdin。"""
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    fake = fake_run_factory(stdout="ok", returncode=0)
    _install_fake_run(monkeypatch, fake)

    cfg = ex.config.BridgeConfig(codex_sandbox="read-only")
    await ex.AgentExecutor(cfg).run_codex(
        "继续做事", str(tmp_path), resume_session_id="sid-123"
    )
    argv = fake.calls[0]["argv"]

    assert argv[:3] == ["C:/fake/codex.exe", "exec", "resume"]
    assert "sid-123" in argv
    assert argv[-1] == "-"
    assert "--json" in argv and "-o" in argv
    assert "--skip-git-repo-check" in argv
    assert "-c" in argv
    assert "sandbox_mode=read-only" in argv
    assert "--sandbox" not in argv
    assert fake.calls[0]["stdin_text"] == "继续做事"


async def test_run_codex_without_resume_keeps_new_session_argv(
    monkeypatch, fake_run_factory, tmp_path
):
    """默认仍然新开 codex exec 会话，保持既有行为。"""
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    fake = fake_run_factory(stdout="ok", returncode=0)
    _install_fake_run(monkeypatch, fake)

    await ex.AgentExecutor().run_codex("新任务", str(tmp_path))
    argv = fake.calls[0]["argv"]

    assert argv[:2] == ["C:/fake/codex.exe", "exec"]
    assert "resume" not in argv
    assert "--sandbox" in argv
    assert "新任务" not in argv


async def test_run_codex_captures_session_id_from_jsonl(
    monkeypatch, fake_run_factory, tmp_path
):
    """从本次 --json 事件流里记录 Codex session id。"""
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    jsonl = "\n".join(
        [
            json.dumps({"type": "session.created", "session_id": "sid-jsonl"}),
            json.dumps({"type": "agent_message", "text": "done"}),
        ]
    )
    fake = fake_run_factory(stdout=jsonl, returncode=0)
    _install_fake_run(monkeypatch, fake)

    result = await ex.AgentExecutor().run_codex("x", str(tmp_path))

    assert result.session_id == "sid-jsonl"


async def test_run_codex_reports_progress_from_jsonl_chunks(monkeypatch, tmp_path):
    """stdout 分块到达时，_invoke 应增量解析 JSONL 并回调人类可读进度。"""
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    events = [
        json.dumps({"type": "item.started", "item": {"type": "command", "command": "pytest -q"}})
        + "\n",
        json.dumps({"type": "agent_message", "text": "测试通过"}) + "\n",
    ]
    labels: list[str] = []

    async def fake_run(argv, *, cwd, stdin_text, timeout, extra_env=None, on_stdout_chunk=None):
        stdout = ""
        for event in events:
            stdout += event
            if on_stdout_chunk is not None:
                await on_stdout_chunk(event)
        return stdout, "", 0, False

    monkeypatch.setattr(ex, "_run", fake_run)

    async def on_progress(message: str) -> None:
        labels.append(message)

    await ex.AgentExecutor().run_codex("x", str(tmp_path), on_progress=on_progress)

    assert any("pytest -q" in label for label in labels)
    assert any("测试通过" in label for label in labels)


async def test_pump_progress_dispatcher_slow_callback_does_not_block_stdout():
    """慢进度回调不能拖住 stdout pump；stdout 字节仍要完整返回。"""
    chunks = [(b"x" * 8192) + b"\n" for _ in range(256)]
    expected = b"".join(chunks)
    callback_started = asyncio.Event()

    class _Stream:
        def __init__(self):
            self._chunks = list(chunks)

        async def read(self, _n):
            await asyncio.sleep(0)
            if self._chunks:
                return self._chunks.pop(0)
            return b""

    async def slow_progress(_message: str) -> None:
        callback_started.set()
        await asyncio.sleep(5)

    dispatcher = ex._ProgressDispatcher(slow_progress, send_timeout=0.05)
    dispatcher.start()
    start = time.monotonic()
    try:
        data = await ex._pump(_Stream(), on_chunk=dispatcher.enqueue)
    finally:
        await dispatcher.stop()

    assert callback_started.is_set()
    assert data == expected
    assert time.monotonic() - start < 1.0


async def test_run_claude_argv_uses_print_stream_json(
    monkeypatch, fake_run_factory, tmp_path
):
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    fake = fake_run_factory(stdout="{}")
    _install_fake_run(monkeypatch, fake)

    cfg = ex.config.BridgeConfig(claude_model="opus")
    await ex.AgentExecutor(cfg).run_claude("x", str(tmp_path))
    argv = fake.calls[0]["argv"]

    assert "-p" in argv
    assert "--output-format" in argv and "stream-json" in argv
    assert "--verbose" in argv
    assert "--permission-mode" in argv
    assert "--model" in argv and "opus" in argv
    for bad in ("-q", "exec", "--full-auto"):
        assert bad not in argv


async def test_run_claude_resume_argv_uses_session_and_stdin(
    monkeypatch, fake_run_factory, tmp_path
):
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    fake = fake_run_factory(stdout="{}")
    _install_fake_run(monkeypatch, fake)

    cfg = ex.config.BridgeConfig(claude_model="opus")
    await ex.AgentExecutor(cfg).run_claude(
        "continue work", str(tmp_path), resume_session_id="sid-claude"
    )
    argv = fake.calls[0]["argv"]

    assert "-p" in argv
    assert "--output-format" in argv and "stream-json" in argv
    assert "--verbose" in argv
    assert "--permission-mode" in argv
    assert "--resume" in argv and "sid-claude" in argv
    assert "--model" in argv and "opus" in argv
    assert "continue work" not in argv
    assert fake.calls[0]["stdin_text"] == "continue work"


async def test_run_claude_captures_session_id_from_json(
    monkeypatch, fake_run_factory, tmp_path
):
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    payload = {"result": "ok", "session_id": "sid-json"}
    fake = fake_run_factory(stdout=json.dumps(payload), returncode=0)
    _install_fake_run(monkeypatch, fake)

    result = await ex.AgentExecutor().run_claude("x", str(tmp_path))

    assert result.session_id == "sid-json"


async def test_run_claude_reports_progress_from_stream_json_chunks(monkeypatch, tmp_path):
    """stdout 分块到达时，Claude stream-json 也应增量回调可读进度。"""
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)
    events = [
        json.dumps({"type": "system", "subtype": "init"}) + "\n",
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "pytest -q"},
                        }
                    ]
                },
            }
        )
        + "\n",
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "正在检查结果"}]},
            }
        )
        + "\n",
        json.dumps({"type": "result", "result": "done", "is_error": False}) + "\n",
    ]
    labels: list[str] = []

    async def fake_run(argv, *, cwd, stdin_text, timeout, extra_env=None, on_stdout_chunk=None):
        stdout = ""
        for event in events:
            stdout += event
            if on_stdout_chunk is not None:
                await on_stdout_chunk(event)
        return stdout, "", 0, False

    monkeypatch.setattr(ex, "_run", fake_run)

    async def on_progress(message: str) -> None:
        labels.append(message)

    result = await ex.AgentExecutor().run_claude(
        "x", str(tmp_path), on_progress=on_progress
    )

    assert result.output == "done"
    assert any("pytest -q" in label for label in labels)
    assert any("正在检查结果" in label for label in labels)


# ---------------------------------------------------------------------------
# 启动失败 / 工作目录无效
# ---------------------------------------------------------------------------

async def test_run_codex_os_error_returns_friendly(monkeypatch, tmp_path):
    _patch_cli_available(monkeypatch)
    _patch_no_git(monkeypatch)

    async def boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(ex, "_run", boom)

    result = await ex.AgentExecutor().run_codex("x", str(tmp_path))
    assert result.success is False
    assert "启动" in (result.error or "")


async def test_run_codex_missing_cwd_reports_dir_error(monkeypatch, tmp_path):
    _patch_cli_available(monkeypatch)

    async def must_not_run(*a, **k):
        raise AssertionError("工作目录无效时不应启动子进程")

    monkeypatch.setattr(ex, "_run", must_not_run)

    missing = str(tmp_path / "does-not-exist")
    result = await ex.AgentExecutor().run_codex("x", missing)

    assert result.success is False
    assert "目录不存在" in (result.error or "")
    # 这是入参错误，不是「CLI 不可用」
    assert result.agent_unavailable is False


# ---------------------------------------------------------------------------
# git 改动检测：-z 解析（空格 / 中文 / 重命名）与 diff
# ---------------------------------------------------------------------------

def test_diff_git_added_changed_removed():
    assert ex._diff_git({"a.py": " M"}, {"a.py": " M", "b.py": "??"}) == ["b.py"]
    # 之前有改动、现在消失（被还原 / 提交）也算动过
    assert ex._diff_git({"a.py": " M"}, {}) == ["a.py"]
    # 状态变化
    assert ex._diff_git({"a.py": " M"}, {"a.py": "M "}) == ["a.py"]
    # 非 git 仓库
    assert ex._diff_git(None, {"a.py": "??"}) == []
    assert ex._diff_git({}, None) == []


def test_git_status_parses_z_format(monkeypatch):
    monkeypatch.setattr(ex.config, "resolve_cli", lambda name: "git" if name == "git" else None)

    canned = " M src/a b.py\x00?? 新建.py\x00R  new.py\x00old.py\x00".encode("utf-8")
    monkeypatch.setattr(
        ex.config,
        "git_capture",
        lambda git, cwd, args, timeout: ex.config.CapturedRun(
            returncode=0, stdout=canned, stderr=b"", timed_out=False
        ),
    )

    snap = ex._git_status("/whatever")
    assert "src/a b.py" in snap   # 含空格路径
    assert "新建.py" in snap       # 中文路径
    assert "new.py" in snap        # 重命名取目标路径
    assert "old.py" not in snap    # 源路径被跳过


# ---------------------------------------------------------------------------
# _extract_claude：usage 非 dict 不得崩（之前会 TypeError，把成功误判为失败）
# ---------------------------------------------------------------------------

def test_extract_claude_non_dict_usage_no_crash():
    text, usage, is_error, session_id = ex._extract_claude(
        json.dumps({"result": "hi", "usage": 5, "total_cost_usd": 0.01})
    )
    assert text == "hi"
    assert usage == {"total_cost_usd": 0.01}  # 非 dict 的 usage 被丢弃，仅保留成本
    assert is_error is False
    assert session_id is None


def test_extract_claude_string_usage_no_crash():
    text, usage, _, session_id = ex._extract_claude(
        json.dumps({"result": "x", "usage": "abc"})
    )
    assert text == "x"
    assert usage is None
    assert session_id is None


def test_extract_claude_single_json_remains_supported():
    text, usage, is_error, session_id = ex._extract_claude(
        json.dumps(
            {
                "result": "legacy result",
                "usage": {"input_tokens": 2},
                "is_error": False,
                "session_id": "sid-legacy",
            }
        )
    )

    assert text == "legacy result"
    assert usage == {"input_tokens": 2}
    assert is_error is False
    assert session_id == "sid-legacy"


def test_extract_claude_stream_json_uses_result_event():
    jsonl = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "session_id": "sid-init"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "not final"}]},
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "result": "final answer",
                    "usage": {"input_tokens": 11, "output_tokens": 7},
                    "is_error": False,
                    "session_id": "sid-result",
                }
            ),
        ]
    )

    text, usage, is_error, session_id = ex._extract_claude(jsonl)

    assert text == "final answer"
    assert usage == {"input_tokens": 11, "output_tokens": 7}
    assert is_error is False
    assert session_id == "sid-result"


# ---------------------------------------------------------------------------
# _drain：卡住的 pump 任务必须有界返回（防止超时路径无限挂死）
# ---------------------------------------------------------------------------

async def test_drain_bounded_on_stuck_task():
    async def stuck():
        await asyncio.Event().wait()  # 永不完成（模拟孙进程持管道、读不到 EOF）

    task = asyncio.create_task(stuck())
    out = await ex._drain(task, timeout=0.2)
    assert out == b""  # 超时后返回空，而不是无限等待
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_pump_returns_partial_on_cancel():
    # _pump 被取消时返回已读到的部分，便于 _run 干净收尾。
    class _Stream:
        def __init__(self):
            self._chunks = [b"hello "]

        async def read(self, n):
            if self._chunks:
                return self._chunks.pop(0)
            await asyncio.Event().wait()  # 之后永不返回

    task = asyncio.create_task(ex._pump(_Stream()))
    await asyncio.sleep(0.05)
    task.cancel()
    result = await task
    assert result == b"hello "


async def test_drain_recovers_partial_output_on_timeout():
    """超时后 _drain 必须捞回 _pump 已读到的部分，而不是直接丢成空串。

    场景：孙进程持着写端 → 管道读不到 EOF → _pump 读到一些数据后永远阻塞。
    超时不该把这部分已产生的输出丢掉（这正是“超时仍保留部分输出”承诺的关键）。
    """
    class _Stream:
        def __init__(self):
            self._chunks = [b"partial-", b"output"]

        async def read(self, n):
            if self._chunks:
                return self._chunks.pop(0)
            await asyncio.Event().wait()  # 读完已有数据后永不返回（模拟无 EOF）

    task = asyncio.create_task(ex._pump(_Stream()))
    await asyncio.sleep(0.05)  # 让 _pump 先把两块数据读进去
    out = await ex._drain(task, timeout=0.2)
    assert out == b"partial-output"  # 已读部分被保住，而非 b""
