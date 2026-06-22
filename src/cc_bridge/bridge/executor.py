"""CLI 子进程管理：在后台静默调用对方的 CLI，并把结果收回来.

设计要点：

- **prompt 走 stdin，不走命令行**。无论多长、含多少特殊字符，都不会触发
  Windows ``cmd.exe`` 元字符注入或命令行长度限制。
- **超时仍保留部分输出**：用流式读取 + 进程树终止，超时后依然能拿到已产生的内容。
- **改动文件检测**：调用前后对 ``git status`` 做快照对比（仓库内有效）。
- **静默**：隐藏控制台窗口（见 :func:`config.subprocess_creation_kwargs`）。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import config

ProgressCallback = Callable[[str], Awaitable[None]]
ProgressChunkHandler = Callable[[str], None]

_PROGRESS_QUEUE_MAXSIZE = 64
_PROGRESS_SEND_TIMEOUT_SECONDS = 0.25
_PROGRESS_STOP = object()


@dataclass
class ExecutionResult:
    """一次跨 agent 调用的结果。"""

    success: bool
    output: str                       # 主要输出（已尽量提取成干净文本）
    files_changed: list[str] = field(default_factory=list)
    error: str | None = None
    duration_seconds: float = 0.0
    token_usage: dict | None = None    # 能解析出来时填入
    session_id: str | None = None      # agent 会话 ID；用于后续续接
    exit_code: int | None = None
    timed_out: bool = False
    raw_stdout: str = ""               # 原始输出，供 parser 进一步处理 / 调试
    raw_stderr: str = ""

    @property
    def agent_unavailable(self) -> bool:
        return self.exit_code is None and not self.timed_out and not self.success


# ---------------------------------------------------------------------------
# 进程工具
# ---------------------------------------------------------------------------

async def _safe_progress(on_progress: ProgressCallback | None, message: str) -> None:
    """安全发送进度；回调失败只记为宿主问题，绝不影响 agent 主流程。"""
    if on_progress is None:
        return
    text = str(message).strip()
    if not text:
        return
    try:
        await on_progress(text)
    except Exception:
        pass


class _ProgressDispatcher:
    """Best-effort progress delivery that never blocks stdout readers."""

    def __init__(
        self,
        on_progress: ProgressCallback | None,
        *,
        maxsize: int = _PROGRESS_QUEUE_MAXSIZE,
        send_timeout: float = _PROGRESS_SEND_TIMEOUT_SECONDS,
    ) -> None:
        self._on_progress = on_progress
        self._queue: asyncio.Queue[str | object] = asyncio.Queue(maxsize=maxsize)
        self._send_timeout = send_timeout
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._on_progress is None or self._task is not None:
            return
        self._task = asyncio.create_task(self._consume())

    def enqueue(self, message: str) -> None:
        if self._task is None:
            return
        text = str(message)
        if not text:
            return
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(text)
            except asyncio.QueueFull:
                pass

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _PROGRESS_STOP:
                    return
                if isinstance(item, str):
                    try:
                        await asyncio.wait_for(
                            _safe_progress(self._on_progress, item),
                            timeout=self._send_timeout,
                        )
                    except asyncio.TimeoutError:
                        pass
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        if self._task is None:
            return
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
        self._queue.put_nowait(_PROGRESS_STOP)

        done, _pending = await asyncio.wait(
            {self._task}, timeout=self._send_timeout + 0.5
        )
        if not done:
            self._task.cancel()
            await asyncio.wait({self._task}, timeout=0.5)
        if self._task.done():
            await asyncio.gather(self._task, return_exceptions=True)


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """尽力终止整个进程树（codex/claude 会派生 node 等子进程）。"""
    pid = proc.pid
    try:
        if config.IS_WINDOWS:
            # /T 连子孙进程一起杀（codex.cmd -> node 这种）。taskkill 万一失败
            # （缺失 / 被策略拦），退回到至少杀掉直接子进程，别让 node 继续烧额度。
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                **config.subprocess_creation_kwargs(),
            )
            if result.returncode != 0:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        else:
            # start_new_session=True 让子进程独立成组，可整组发信号。
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
    except Exception:
        # 兜底：至少杀掉直接子进程。
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _pump(
    stream: asyncio.StreamReader | None,
    *,
    on_chunk: ProgressChunkHandler | None = None,
) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    try:
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if on_chunk is not None:
                try:
                    on_chunk(chunk.decode("utf-8", errors="replace"))
                except Exception:
                    pass
    except asyncio.CancelledError:
        # 被取消时返回已读到的部分，而不是丢弃；让 _run 的 finally 能干净收尾。
        pass
    return b"".join(chunks)


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """尽力终止进程树，并【有界】等待其退出（绝不无限阻塞）。"""
    _kill_tree(proc)
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            pass


async def _drain(task: "asyncio.Task", timeout: float = 5.0) -> bytes:
    """有界地取 pump 任务结果，并尽量【保住已读到的部分】。

    若进程已被杀但仍有【孙进程】持有 stdout/stderr 写端，管道永远等不到 EOF，
    无界 ``await`` 会让整个调用永久挂死、令 timeout 形同虚设。这里用 shield + wait_for
    设上限；但超时后【不能直接丢成空串】——那会违背“超时仍保留部分输出”的承诺
    （孙进程持管道时尤甚）。改为：超时即主动取消 pump，并短等它在 ``CancelledError``
    分支里返回的已读 bytes（见 :func:`_pump`）；只有真正永不返回的任务才在二次超时后
    回退空串。
    """
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            # _pump 收到取消会 return 已读到的部分（不抛 CancelledError）；
            # 真正卡死、连取消都不响应的任务则在这里二次超时，回退空串。
            return await asyncio.wait_for(task, 1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return b""


async def _run(
    argv: list[str],
    *,
    cwd: str,
    stdin_text: str,
    timeout: int,
    extra_env: dict | None = None,
    on_stdout_chunk: ProgressCallback | None = None,
) -> tuple[str, str, int | None, bool]:
    """启动子进程，喂入 stdin_text，返回 (stdout, stderr, returncode, timed_out)。

    超时时终止进程树，但仍返回已经读到的部分输出。
    """
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        **config.subprocess_creation_kwargs(),
    )

    progress_dispatcher = _ProgressDispatcher(on_stdout_chunk)
    progress_dispatcher.start()

    out_task = asyncio.create_task(_pump(proc.stdout, on_chunk=progress_dispatcher.enqueue))
    err_task = asyncio.create_task(_pump(proc.stderr))

    # 把 prompt 通过 stdin 送进去然后关闭，告知 CLI 输入结束。
    if proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    timed_out = False
    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            await _terminate(proc)
        # 进程已退出（或已尽力终止）。有界地收集 pump 输出——即便有孙进程仍持管道、
        # 读不到 EOF，也只等有限时间后返回已读到的部分，绝不无限挂死。
        out = (await _drain(out_task)).decode("utf-8", errors="replace")
        err = (await _drain(err_task)).decode("utf-8", errors="replace")
        return out, err, proc.returncode, timed_out
    except asyncio.CancelledError:
        # 被上层取消（GUI「跳过 / 上一步 / 关窗」）：杀掉子进程树，再把取消向上抛，
        # 否则 codex / claude 会在后台继续跑、继续消耗订阅额度。
        await _terminate(proc)
        raise
    finally:
        # 任何退出路径（正常 / 超时 / 取消，含「超时清理时又被取消」的叠加竞态）
        # 都不留下悬挂的 pump 任务。
        for pump_task in (out_task, err_task):
            if not pump_task.done():
                pump_task.cancel()
        await asyncio.gather(out_task, err_task, return_exceptions=True)
        await progress_dispatcher.stop()


# ---------------------------------------------------------------------------
# git 改动快照
# ---------------------------------------------------------------------------

def _git_status(cwd: str) -> dict[str, str] | None:
    """返回 {path: status} 的快照；不是 git 仓库或没有 git 时返回 None。

    用 ``--porcelain -z``（NUL 分隔、不加引号、不做 C 风格八进制转义），这样含空格
    或中文的文件名也能原样取到——对中文用户尤其重要。重命名/复制条目会多出一个源路径
    token，跳过它、只记目标路径。
    """
    git = config.resolve_cli("git")
    if not git:
        return None
    # 与 context._git_info 一致走 config.git_capture：硬化超时 + stdin 隔离 + 禁 fsmonitor。
    res = config.git_capture(
        git, cwd, ["status", "--porcelain", "-z", "--untracked-files=all"], timeout=15
    )
    if res.returncode != 0:  # 非零 / 启动失败 / 超时（returncode=None）→ 无快照
        return None
    # git 默认按 UTF-8 存储路径字节；errors=replace 兜底坏字节。
    text = res.stdout.decode("utf-8", errors="replace")
    tokens = text.split("\0")
    snapshot: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if len(tok) < 4:
            i += 1
            continue
        status, path = tok[:2], tok[3:]
        snapshot[path] = status
        # 重命名(R)/复制(C) 后面紧跟一个源路径 token，跳过。
        if "R" in status or "C" in status:
            i += 2
        else:
            i += 1
    return snapshot


def _diff_git(before: dict[str, str] | None, after: dict[str, str] | None) -> list[str]:
    if before is None or after is None:
        return []
    changed: set[str] = set()
    for path, status in after.items():
        if before.get(path) != status:
            changed.add(path)
    # 之前有改动、现在恢复干净（被还原/提交）的文件也算被动过。
    for path in before:
        if path not in after:
            changed.add(path)
    return sorted(changed)


# ---------------------------------------------------------------------------
# 输出的最小提取（语义解析交给 parser.py）
# ---------------------------------------------------------------------------

def _extract_claude_event(data: dict) -> tuple[str, dict | None, bool, str | None]:
    text = data.get("result") or data.get("text") or ""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = None  # usage 可能是非 dict（脏数据）；不臆断其形状
    if data.get("total_cost_usd") is not None:
        # 解包前确保是 mapping，否则 {**非mapping} 会抛 TypeError，
        # 把一次【实际成功】的调用误判成失败、丢掉真实输出。
        usage = {**(usage or {}), "total_cost_usd": data["total_cost_usd"]}
    is_error = bool(data.get("is_error"))
    session_id = _find_codex_session_id(data)
    return (text if isinstance(text, str) else json.dumps(text)), usage, is_error, session_id


def _extract_claude(stdout: str) -> tuple[str, dict | None, bool, str | None]:
    """从 Claude 单 JSON 或 stream-json(JSONL) 输出里取出结果。"""
    stdout = stdout.strip()
    if not stdout:
        return "", None, False, None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        return _extract_claude_event(data)

    result_event: dict | None = None
    stream_session_id: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if stream_session_id is None:
            stream_session_id = _find_codex_session_id(event)
        if event.get("type") == "result":
            result_event = event

    if result_event is not None:
        text, usage, is_error, session_id = _extract_claude_event(result_event)
        return text, usage, is_error, session_id or stream_session_id
    return stdout, None, False, stream_session_id


def _extract_codex(final_message: str, stdout_jsonl: str) -> tuple[str, dict | None]:
    """优先用 ``-o`` 写出的最终消息；usage 尽力从 JSONL 事件里捞。"""
    text = final_message.strip()
    usage: dict | None = None
    for line in stdout_jsonl.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        # 不同 codex 版本字段名不同，宽松匹配 token 用量。
        for key in ("usage", "token_usage", "tokens"):
            if isinstance(event.get(key), dict):
                usage = event[key]
        # 没有 -o 文件时，从事件里兜底提取最终助手消息。
        if not text:
            item = event.get("item") if isinstance(event.get("item"), dict) else None
            candidate = None
            if item and item.get("type") in {"agent_message", "assistant_message", "message"}:
                candidate = item.get("text") or item.get("content")
            elif event.get("type") in {"agent_message", "assistant_message"}:
                candidate = event.get("text") or event.get("message")
            if isinstance(candidate, str) and candidate.strip():
                text = candidate.strip()
    return text, usage


_SESSION_ID_KEYS = (
    "session_id",
    "sessionId",
    "conversation_id",
    "conversationId",
    "thread_id",
    "threadId",
)


def _short_progress(text: str, limit: int = 240) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _string_value(value) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("text", "content", "message", "result"):
            nested = _string_value(value.get(key))
            if nested:
                return nested
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                nested = _string_value(item.get("text") or item.get("content"))
                if nested:
                    parts.append(nested)
        return " ".join(parts).strip() or None
    return None


def _event_text(data: dict) -> str | None:
    for key in ("text", "message", "content", "result"):
        value = _string_value(data.get(key))
        if value:
            return value
    return None


def _event_command(data: dict) -> str | None:
    for key in ("command", "cmd", "shell_command"):
        value = _string_value(data.get(key))
        if value:
            return value
    argv = data.get("argv") or data.get("args")
    if isinstance(argv, list) and all(isinstance(part, str) for part in argv):
        return " ".join(argv).strip() or None
    return None


def _codex_progress_label(event: dict) -> str | None:
    """把 Codex JSONL 事件压缩成人类可读的一行进度标签。"""
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    command = _event_command(item) or _event_command(event)
    if command:
        return f"Codex 正在执行: {_short_progress(command)}"

    event_type = str(event.get("type") or "")
    item_type = str(item.get("type") or "")
    text = _event_text(item) or _event_text(event)
    if text and (
        "message" in event_type
        or "message" in item_type
        or item_type in {"agent_message", "assistant_message"}
    ):
        return f"Codex: {_short_progress(text)}"

    if event_type in {"turn.started", "task.started", "session.created"}:
        return "Codex 已开始处理"
    return None


def _claude_tool_progress_label(event: dict) -> str | None:
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    content = message.get("content") or event.get("content")
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return None

    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_use":
            continue
        tool_name = _string_value(item.get("name")) or "tool"
        tool_input = item.get("input") if isinstance(item.get("input"), dict) else {}
        command = _event_command(tool_input) or _event_command(item)
        if command:
            return f"Claude 正在执行: {_short_progress(command)}"
        return f"Claude 正在使用工具: {_short_progress(tool_name)}"
    return None


def _claude_progress_label(event: dict) -> str | None:
    """把 Claude stream-json 事件压缩成人类可读的一行进度标签。"""
    event_type = str(event.get("type") or "")
    subtype = str(event.get("subtype") or "")

    if event_type == "result":
        if event.get("is_error"):
            return "Claude 返回错误"
        return "Claude 已完成处理"

    tool_label = _claude_tool_progress_label(event)
    if tool_label:
        return tool_label

    text = _event_text(event)
    if text and (event_type == "assistant" or "message" in event_type):
        return f"Claude: {_short_progress(text)}"

    if event_type == "system" and subtype == "init":
        return "Claude 已开始处理"
    return None


def _jsonl_progress_callback(
    on_progress: ProgressCallback,
    labeler: Callable[[dict], str | None],
) -> ProgressCallback:
    line_buffer = ""

    async def _on_stdout(chunk: str) -> None:
        nonlocal line_buffer
        line_buffer += chunk
        while "\n" in line_buffer:
            line, line_buffer = line_buffer.split("\n", 1)
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            try:
                label = labeler(event)
            except Exception:
                continue
            await _safe_progress(on_progress, label or "")

    return _on_stdout


def _find_codex_session_id(data: dict) -> str | None:
    for key in _SESSION_ID_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for container_key in ("session", "conversation", "thread"):
        nested = data.get(container_key)
        if not isinstance(nested, dict):
            continue
        for key in ("id", *_SESSION_ID_KEYS):
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    event_type = str(data.get("type") or "")
    value = data.get("id")
    if event_type.startswith("session") and isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _extract_codex_session_id(stdout_jsonl: str) -> str | None:
    """从本次 Codex --json 输出中提取会话 ID；找不到时返回 None。"""
    for line in stdout_jsonl.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        session_id = _find_codex_session_id(event)
        if session_id:
            return session_id
    return None


def _extract_claude_session_id(stdout_json: str) -> str | None:
    """从 Claude 单 JSON 或 stream-json(JSONL) 输出里提取 session id。"""
    try:
        _text, _usage, _is_error, session_id = _extract_claude(stdout_json)
    except Exception:
        return None
    return session_id


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------

class AgentExecutor:
    """管理对 ``claude`` / ``codex`` CLI 的后台调用。"""

    def __init__(self, cfg: config.BridgeConfig | None = None) -> None:
        self.cfg = cfg or BridgeConfigFactory()

    # -- Claude -----------------------------------------------------------
    async def run_claude(
        self,
        prompt: str,
        cwd: str,
        timeout: int | None = None,
        resume_session_id: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> ExecutionResult:
        exe = config.resolve_cli("claude")
        if not exe:
            return _unavailable("claude")

        args = [
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            self.cfg.claude_permission_mode,
        ]
        if self.cfg.claude_model:
            args += ["--model", self.cfg.claude_model]
        if resume_session_id:
            args += ["--resume", resume_session_id]
        argv = config.build_launch_argv(exe, args)
        return await self._invoke(
            "claude",
            argv,
            prompt,
            cwd,
            timeout,
            on_progress=on_progress,
            session_id_hint=resume_session_id,
        )

    # -- Codex ------------------------------------------------------------
    async def run_codex(
        self,
        prompt: str,
        cwd: str,
        timeout: int | None = None,
        resume_session_id: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> ExecutionResult:
        exe = config.resolve_cli("codex")
        if not exe:
            return _unavailable("codex")

        out_fd, out_path = tempfile.mkstemp(prefix="cc_bridge_codex_", suffix=".txt")
        os.close(out_fd)
        if resume_session_id:
            args = [
                "exec",
                "resume",
                "-c",
                f"sandbox_mode={self.cfg.codex_sandbox}",
                resume_session_id,
                "--skip-git-repo-check",
                "--json",
                "-o",
                out_path,
            ]
        else:
            args = [
                "exec",
                "--sandbox",
                self.cfg.codex_sandbox,
                "--skip-git-repo-check",
                "--json",
                "-o",
                out_path,
            ]
        if self.cfg.codex_model:
            args += ["-m", self.cfg.codex_model]
        if resume_session_id:
            args.append("-")
        argv = config.build_launch_argv(exe, args)
        try:
            return await self._invoke(
                "codex",
                argv,
                prompt,
                cwd,
                timeout,
                final_file=out_path,
                on_progress=on_progress,
                session_id_hint=resume_session_id,
            )
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass

    # -- 共通流程 ---------------------------------------------------------
    async def _invoke(
        self,
        agent: str,
        argv: list[str],
        prompt: str,
        cwd: str,
        timeout: int | None,
        final_file: str | None = None,
        on_progress: ProgressCallback | None = None,
        session_id_hint: str | None = None,
    ) -> ExecutionResult:
        timeout = timeout or self.cfg.timeout_seconds
        cwd = str(Path(cwd))
        # 先校验工作目录：否则 create_subprocess_exec(cwd=不存在) 抛的
        # FileNotFoundError 会被误判成「CLI 未安装」，把用户引向错误方向。
        if not Path(cwd).is_dir():
            return ExecutionResult(
                success=False,
                output="",
                error=(
                    f"项目目录不存在或不是目录：{cwd}。"
                    "请确认传入的 project_dir 是有效的绝对路径。"
                ),
                exit_code=-1,
            )
        # git 快照用阻塞 subprocess，放到线程池跑，避免占住事件循环、拖慢取消投递。
        before = await asyncio.to_thread(_git_status, cwd)
        start = time.monotonic()
        stdout_progress: ProgressCallback | None = None
        if agent == "codex" and on_progress is not None:
            stdout_progress = _jsonl_progress_callback(on_progress, _codex_progress_label)
        elif agent == "claude" and on_progress is not None:
            stdout_progress = _jsonl_progress_callback(on_progress, _claude_progress_label)

        try:
            stdout, stderr, code, timed_out = await _run(
                argv,
                cwd=cwd,
                stdin_text=prompt,
                timeout=timeout,
                on_stdout_chunk=stdout_progress,
            )
        except FileNotFoundError:
            return _unavailable(agent)
        except OSError as exc:
            return ExecutionResult(
                success=False,
                output="",
                error=f"启动 {agent} 失败：{exc}",
                duration_seconds=time.monotonic() - start,
            )
        duration = time.monotonic() - start
        after = await asyncio.to_thread(_git_status, cwd)
        files_changed = _diff_git(before, after)

        claude_session_id = None
        if agent == "claude":
            try:
                text, usage, is_error, claude_session_id = _extract_claude(stdout)
            except Exception:
                # 解析意外失败绝不能把一次成功调用判为失败：退回原始输出。
                text, usage, is_error = stdout.strip(), None, False
            success = (code == 0) and not timed_out and not is_error
        else:
            final_message = ""
            if final_file:
                try:
                    final_message = Path(final_file).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    final_message = ""
            text, usage = _extract_codex(final_message, stdout)
            if not text:
                text = stdout.strip()
            success = (code == 0) and not timed_out
        session_id = None
        if agent == "codex":
            session_id = _extract_codex_session_id(stdout) or session_id_hint
        elif agent == "claude":
            session_id = claude_session_id or _extract_claude_session_id(stdout) or session_id_hint

        error = None
        if timed_out:
            error = f"{agent} 调用超时（{timeout}s），返回的是已产生的部分结果。"
        elif not success:
            error = (stderr.strip() or f"{agent} 以非零状态码 {code} 退出。")

        return ExecutionResult(
            success=success,
            output=text,
            files_changed=files_changed,
            error=error,
            duration_seconds=duration,
            token_usage=usage,
            session_id=session_id,
            exit_code=code,
            timed_out=timed_out,
            raw_stdout=stdout,
            raw_stderr=stderr,
        )


def _unavailable(agent: str) -> ExecutionResult:
    name = "Claude Code CLI" if agent == "claude" else "Codex CLI"
    return ExecutionResult(
        success=False,
        output="",
        error=(
            f"未找到 {name}（`{agent}` 命令不可用）。"
            f"请先安装并登录对应的桌面版，再运行 cc-bridge 安装向导。"
        ),
        exit_code=None,
    )


def BridgeConfigFactory() -> config.BridgeConfig:  # noqa: N802 - 兼容默认参数惰性求值
    """惰性读取环境变量，避免在 import 期固化配置。"""
    return config.BridgeConfig.from_env()
