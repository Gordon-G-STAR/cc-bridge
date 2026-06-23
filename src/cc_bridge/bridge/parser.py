"""把 agent 的执行结果整理成「适合返回给调用方」的摘要.

executor 已经做了最小提取（拿到干净的最终文本、改动文件、token 用量）。
这里负责：

- 把 :class:`~cc_bridge.bridge.executor.ExecutionResult` 归一成 :class:`ParsedResult`；
- 识别常见的失败模式（额度不足、未登录、超时），给出清晰提示；
- 压缩成不超过 ``max_length`` 字符的摘要，防止上下文爆炸。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field

from . import config
from .config import DEFAULT_MAX_OUTPUT_CHARS
from .executor import ExecutionResult

# 在 stderr / 输出里命中这些关键词时，归类为「额度/限流」问题。
# 刻意避开过宽的裸词（如裸 "429" / "insufficient"），减少把无关错误误判成限流。
_QUOTA_HINTS = (
    "rate limit", "rate_limit", "quota", "usage limit",
    "insufficient_quota", "insufficient quota",
    "too many requests", "overloaded", "额度", "限流",
)
# 同理避开裸 "auth" / "login"（会误伤 author/oauth/路径里的 auth.json 等）。
_AUTH_HINTS = (
    "not logged in", "unauthorized", "authentication", "401",
    "credentials", "please log in", "未登录", "请登录", "登录已过期",
)

# 超长输出落盘目录里最多保留的历史文件数(有界清理,缩小敏感输出暴露面)。
_MAX_SAVED_OUTPUTS = 20


def _prune_saved_outputs(out_dir, keep: int) -> None:
    """把 ``out_dir`` 下的 cc-bridge 输出文件清到至多 ``keep`` 个(删最旧的)。

    尽力而为:任何 IO 异常都静默吞掉,绝不影响主流程。
    """
    try:
        files = [
            p
            for p in out_dir.iterdir()
            if p.is_file() and p.name.startswith("cc_bridge_output_")
        ]
    except OSError:
        return
    if len(files) <= keep:
        return
    try:
        files.sort(key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for p in files[: len(files) - keep]:
        try:
            p.unlink()
        except OSError:
            pass


@dataclass
class ParsedResult:
    success: bool
    summary: str                       # 给调用方看的自然语言摘要
    files_changed: list[str] = field(default_factory=list)
    failure_kind: str | None = None    # None / "quota" / "auth" / "timeout" / "error"
    token_usage: dict | None = None
    session_id: str | None = None
    duration_seconds: float = 0.0


class ResultParser:
    """归一化与摘要化 agent 输出。"""

    def parse(self, result: ExecutionResult, agent: str) -> ParsedResult:
        """把 ExecutionResult 解析成 ParsedResult（agent: "claude" / "codex"）。"""
        failure_kind = self._classify_failure(result)
        summary = result.output.strip()
        if not summary and not result.success:
            summary = result.error or "（没有任何输出）"
        return ParsedResult(
            success=result.success,
            summary=summary,
            files_changed=list(result.files_changed),
            failure_kind=failure_kind,
            token_usage=result.token_usage,
            session_id=result.session_id,
            duration_seconds=result.duration_seconds,
        )

    # 兼容 spec 中分方向的命名 ------------------------------------------------
    def parse_claude_output(self, result: ExecutionResult) -> ParsedResult:
        return self.parse(result, "claude")

    def parse_codex_output(self, result: ExecutionResult) -> ParsedResult:
        return self.parse(result, "codex")

    def _classify_failure(self, result: ExecutionResult) -> str | None:
        if result.success:
            return None
        if result.timed_out:
            return "timeout"
        haystack = f"{result.error or ''}\n{result.raw_stderr}\n{result.output}".lower()
        if any(h in haystack for h in _QUOTA_HINTS):
            return "quota"
        if any(h in haystack for h in _AUTH_HINTS):
            return "auth"
        return "error"

    # -- 摘要 -------------------------------------------------------------
    def summarize_for_caller(
        self,
        parsed: ParsedResult,
        agent: str,
        max_length: int = DEFAULT_MAX_OUTPUT_CHARS,
    ) -> str:
        """把 ParsedResult 渲染成一段紧凑的文本，返回给调用方 agent。"""
        agent_name = "Claude" if agent == "claude" else "Codex"
        lines: list[str] = []

        if parsed.success:
            lines.append(f"✅ {agent_name} 已完成任务。")
        else:
            lines.append(f"❌ {agent_name} 未能完成任务。")
            hint = self._failure_hint(agent_name, parsed.failure_kind)
            if hint:
                lines.append(hint)

        if parsed.files_changed:
            shown = parsed.files_changed[:20]
            lines.append("")
            lines.append(f"改动文件（{len(parsed.files_changed)} 个）：")
            lines.extend(f"  · {p}" for p in shown)
            if len(parsed.files_changed) > len(shown):
                lines.append(f"  …… 另有 {len(parsed.files_changed) - len(shown)} 个")

        if parsed.session_id:
            lines.append("")
            lines.append(f"{agent_name} 会话 ID：{parsed.session_id}")

        if parsed.token_usage:
            lines.append("")
            lines.append(f"Token 用量：{self._format_token_usage(parsed.token_usage)}")

        if parsed.summary:
            lines.append("")
            lines.append(f"{agent_name} 的说明：")
            lines.append(parsed.summary)

        if parsed.duration_seconds:
            lines.append("")
            lines.append(f"（耗时 {parsed.duration_seconds:.1f}s）")

        text = "\n".join(lines)
        if len(text) <= max_length:
            return text

        saved_path = None
        if max_length > 0 and parsed.summary:
            saved_path = self._save_full_output(parsed.summary)
        if saved_path:
            notice = f"完整输出已保存到:{saved_path}"
            return self._truncate_with_notice(text, max_length, notice)
        return self._truncate(text, max_length)

    @staticmethod
    def _format_token_usage(token_usage: dict) -> str:
        preferred = (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "total_tokens",
            "total_cost_usd",
        )
        parts: list[str] = []
        seen: set[str] = set()
        for key in preferred:
            if key in token_usage:
                parts.append(f"{key}={token_usage[key]}")
                seen.add(key)
        for key in sorted(token_usage):
            if key in seen:
                continue
            parts.append(f"{key}={token_usage[key]}")
            if len(parts) >= 8:
                break
        return ", ".join(parts) if parts else str(token_usage)

    def _failure_hint(self, agent_name: str, kind: str | None) -> str | None:
        if kind == "quota":
            return f"原因看起来是 {agent_name} 的订阅额度不足或被限流，请稍后再试或检查订阅状态。"
        if kind == "auth":
            return f"原因看起来是 {agent_name} 未登录，请打开对应桌面版完成登录后重试。"
        if kind == "timeout":
            return "原因是调用超时；上面是已经产生的部分结果。"
        return None

    @staticmethod
    def _save_full_output(text: str) -> str | None:
        """把超长完整输出落盘,返回路径;失败返回 None。

        刻意【不】落公共 OS temp 目录(#22):那里文件因 ``delete=False`` 长期堆积、
        且非用户私有。改为写到 cc-bridge 自己的、用户私有的
        ``stable_app_dir()/outputs`` 下,POSIX 上显式 ``0600``,并对历史文件做有界清理。
        """
        try:
            out_dir = config.stable_app_dir() / "outputs"
            out_dir.mkdir(parents=True, exist_ok=True)
            if not config.IS_WINDOWS:
                try:
                    os.chmod(out_dir, 0o700)  # 目录也收紧:POSIX 仅 owner 可进入
                except OSError:
                    pass
            # 写新文件前先清到 MAX-1,加上这一个 => 至多 MAX。
            _prune_saved_outputs(out_dir, _MAX_SAVED_OUTPUTS - 1)
            fd, path = tempfile.mkstemp(
                prefix="cc_bridge_output_", suffix=".txt", dir=str(out_dir)
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            # mkstemp 在 POSIX 上本就 0600;显式再设一次,意图明确、不依赖实现细节。
            if not config.IS_WINDOWS:
                os.chmod(path, 0o600)
            return path
        except Exception:
            return None

    @classmethod
    def _truncate_with_notice(cls, text: str, max_length: int, notice: str) -> str:
        truncated = cls._truncate(text, max_length)
        if not truncated:
            return notice
        return f"{truncated}\n{notice}"

    @staticmethod
    def _truncate(text: str, max_length: int) -> str:
        if max_length <= 0:
            return ""
        if len(text) <= max_length:
            return text
        marker = "\n\n…（中间省略部分内容）…\n\n"
        # max_length 太小时无法容纳 marker，直接硬截断，保证绝不超过上限。
        if max_length <= len(marker) + 10:
            return text[:max_length]
        keep = max_length - len(marker)
        head_n = int(keep * 0.7)
        tail_n = keep - head_n
        head = text[:head_n]
        tail = text[len(text) - tail_n:] if tail_n > 0 else ""
        result = f"{head}{marker}{tail}"
        return result[:max_length]  # 兜底：任何情况下都不超过 max_length
