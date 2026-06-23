"""ResultParser 离线单测。

构造各种 ExecutionResult，验证：
- 失败分类：quota / auth / timeout / error；
- 成功/失败摘要文案；
- files_changed 渲染；
- 超长 summary 被截断到 max_length 以内。
"""

from __future__ import annotations

from pathlib import Path

from cc_bridge.bridge.parser import ParsedResult, ResultParser


def _saved_output_path(text: str) -> Path:
    prefix = "完整输出已保存到:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return Path(line.removeprefix(prefix))
    raise AssertionError("missing saved output path")


# ---------------------------------------------------------------------------
# 失败分类
# ---------------------------------------------------------------------------

def test_classify_timeout(make_execution_result):
    result = make_execution_result(
        success=False, timed_out=True, error="claude 调用超时（5s）"
    )
    parsed = ResultParser().parse(result, "claude")

    assert parsed.success is False
    assert parsed.failure_kind == "timeout"


def test_classify_quota_from_stderr(make_execution_result):
    result = make_execution_result(
        success=False,
        output="",
        error="non-zero exit",
        raw_stderr="Error: rate limit exceeded, please retry",
        exit_code=1,
    )
    parsed = ResultParser().parse(result, "codex")

    assert parsed.failure_kind == "quota"


def test_classify_quota_chinese_hint(make_execution_result):
    result = make_execution_result(
        success=False, output="额度不足，无法继续", error="failed", exit_code=1
    )
    parsed = ResultParser().parse(result, "claude")

    assert parsed.failure_kind == "quota"


def test_classify_auth_from_error(make_execution_result):
    result = make_execution_result(
        success=False,
        output="",
        error="Error 401: unauthorized - not logged in",
        exit_code=1,
    )
    parsed = ResultParser().parse(result, "codex")

    assert parsed.failure_kind == "auth"


def test_classify_generic_error(make_execution_result):
    result = make_execution_result(
        success=False, output="", error="something weird happened", exit_code=1
    )
    parsed = ResultParser().parse(result, "claude")

    assert parsed.failure_kind == "error"


def test_success_has_no_failure_kind(make_execution_result):
    result = make_execution_result(success=True, output="done")
    parsed = ResultParser().parse(result, "claude")

    assert parsed.failure_kind is None
    assert parsed.success is True


def test_parse_fills_summary_from_error_when_no_output(make_execution_result):
    result = make_execution_result(
        success=False, output="", error="启动失败", exit_code=1
    )
    parsed = ResultParser().parse(result, "claude")

    assert parsed.summary == "启动失败"


# ---------------------------------------------------------------------------
# 摘要渲染
# ---------------------------------------------------------------------------

def test_summarize_success_marker(make_execution_result):
    result = make_execution_result(success=True, output="重构完成")
    parser = ResultParser()
    parsed = parser.parse(result, "claude")
    text = parser.summarize_for_caller(parsed, "claude")

    assert "Claude 已完成任务" in text
    assert "重构完成" in text


def test_summarize_failure_with_quota_hint(make_execution_result):
    result = make_execution_result(
        success=False, output="", raw_stderr="rate limit", error="failed", exit_code=1
    )
    parser = ResultParser()
    parsed = parser.parse(result, "codex")
    text = parser.summarize_for_caller(parsed, "codex")

    assert "未能完成任务" in text
    assert "额度" in text or "限流" in text


def test_summarize_renders_files_changed(make_execution_result):
    result = make_execution_result(
        success=True,
        output="改了几个文件",
        files_changed=["a.py", "b.py", "src/c.py"],
    )
    parser = ResultParser()
    parsed = parser.parse(result, "claude")
    text = parser.summarize_for_caller(parsed, "claude")

    assert "改动文件（3 个）" in text
    assert "a.py" in text
    assert "src/c.py" in text


def test_summarize_renders_session_id(make_execution_result):
    result = make_execution_result(success=True, output="ok", session_id="sid-visible")
    parser = ResultParser()
    parsed = parser.parse(result, "codex")
    text = parser.summarize_for_caller(parsed, "codex")

    assert parsed.session_id == "sid-visible"
    assert "sid-visible" in text


def test_summarize_renders_token_usage(make_execution_result):
    result = make_execution_result(
        success=True,
        output="ok",
        token_usage={"input_tokens": 5, "output_tokens": 3},
    )
    parser = ResultParser()
    parsed = parser.parse(result, "claude")
    text = parser.summarize_for_caller(parsed, "claude")

    assert "Token 用量" in text
    assert "input_tokens=5" in text
    assert "output_tokens=3" in text


def test_summarize_truncates_long_summary(make_execution_result):
    huge = "字" * 10000
    result = make_execution_result(success=True, output=huge)
    parser = ResultParser()
    parsed = parser.parse(result, "claude")

    text = parser.summarize_for_caller(parsed, "claude", max_length=500)

    saved_path = _saved_output_path(text)
    assert len(text) <= 500 + len(str(saved_path)) + 64
    # 截断标记应出现
    assert "省略" in text


def test_summarize_saves_full_output_when_truncated(make_execution_result):
    huge = "full-output-line\n" * 1000
    result = make_execution_result(success=True, output=huge)
    parser = ResultParser()
    parsed = parser.parse(result, "codex")

    text = parser.summarize_for_caller(parsed, "codex", max_length=500)

    assert "省略" in text
    saved_path = _saved_output_path(text)
    assert len(text) <= 500 + len(str(saved_path)) + 64
    assert saved_path.read_text(encoding="utf-8") == huge.strip()


def test_summarize_keeps_complete_saved_output_path_after_truncation(
    monkeypatch, make_execution_result, tmp_path
):
    huge = "full-output-line\n" * 1000
    result = make_execution_result(success=True, output=huge)
    parser = ResultParser()
    parsed = parser.parse(result, "codex")
    saved_path = tmp_path / f"complete-output-path-{'x' * 80}.txt"
    monkeypatch.setattr(parser, "_save_full_output", lambda _text: str(saved_path))

    text = parser.summarize_for_caller(parsed, "codex", max_length=40)

    assert str(saved_path) in text
    assert text.rstrip().endswith(str(saved_path))
    assert len(text) > 40


def test_summarize_still_returns_when_saving_full_output_fails(
    monkeypatch, make_execution_result
):
    huge = "full-output-line\n" * 1000
    result = make_execution_result(success=True, output=huge)
    parser = ResultParser()
    parsed = parser.parse(result, "codex")
    monkeypatch.setattr(parser, "_save_full_output", lambda _text: None)

    text = parser.summarize_for_caller(parsed, "codex", max_length=500)

    assert len(text) <= 500
    assert "省略" in text
    assert "完整输出已保存到:" not in text


def test_summarize_short_summary_not_truncated(make_execution_result):
    result = make_execution_result(success=True, output="短短的输出")
    parser = ResultParser()
    parsed = parser.parse(result, "claude")

    text = parser.summarize_for_caller(parsed, "claude", max_length=4000)

    assert "省略" not in text
    assert "完整输出已保存到:" not in text
    assert "短短的输出" in text


def test_summarize_report_header_with_context(make_execution_result):
    result = make_execution_result(success=True, output="ok", files_changed=["src/foo.py"])
    parser = ResultParser()
    parsed = parser.parse(result, "codex")

    text = parser.summarize_for_caller(
        parsed,
        "codex",
        caller="claude",
        project_dir="C:/repo/demo",
        task="实现标准化报告头",
    )

    assert "【cc-bridge 报告】Claude → Codex" in text
    assert "项目：C:/repo/demo" in text
    assert "任务：实现标准化报告头" in text


def test_summarize_next_step_success_with_files(make_execution_result):
    result = make_execution_result(success=True, output="ok", files_changed=["src/foo.py"])
    parser = ResultParser()
    parsed = parser.parse(result, "codex")

    text = parser.summarize_for_caller(parsed, "codex", caller="claude")

    assert "下一步：" in text
    assert "复核改动" in text


def test_summarize_next_step_success_without_files(make_execution_result):
    result = make_execution_result(success=True, output="ok", files_changed=[])
    parser = ResultParser()
    parsed = parser.parse(result, "claude")

    text = parser.summarize_for_caller(parsed, "claude", caller="codex")

    assert "下一步：" in text
    assert "据此结论继续" in text


def test_summarize_next_step_quota_failure(make_execution_result):
    result = make_execution_result(
        success=False, output="", raw_stderr="rate limit", error="failed", exit_code=1
    )
    parser = ResultParser()
    parsed = parser.parse(result, "codex")

    text = parser.summarize_for_caller(parsed, "codex", caller="claude")

    assert "下一步：" in text
    assert "额度/限流状态" in text


def test_summarize_next_step_auth_failure(make_execution_result):
    result = make_execution_result(
        success=False, output="", error="not logged in", exit_code=1
    )
    parser = ResultParser()
    parsed = parser.parse(result, "claude")

    text = parser.summarize_for_caller(parsed, "claude", caller="codex")

    assert "下一步：" in text
    assert "完成登录后重试" in text


def test_summarize_next_step_timeout_failure(make_execution_result):
    result = make_execution_result(
        success=False, output="partial", timed_out=True, error="timeout", exit_code=None
    )
    parser = ResultParser()
    parsed = parser.parse(result, "codex")

    text = parser.summarize_for_caller(parsed, "codex", caller="claude")

    assert "下一步：" in text
    assert "调高 CC_BRIDGE_TIMEOUT" in text


def test_summarize_without_new_context_is_backward_compatible(make_execution_result):
    result = make_execution_result(success=True, output="ok")
    parser = ResultParser()
    parsed = parser.parse(result, "codex")

    text = parser.summarize_for_caller(parsed, "codex")

    assert "【cc-bridge 报告】" not in text
    assert "下一步" not in text


def test_short_task_truncates_to_120_chars():
    task = "x" * 200

    shortened = ResultParser._short_task(task)

    assert len(shortened) <= 120
    assert shortened.endswith("…")


def test_parsed_result_dataclass_shape(make_execution_result):
    result = make_execution_result(
        success=True,
        output="ok",
        files_changed=["x.py"],
        token_usage={"input_tokens": 5},
        duration_seconds=2.5,
    )
    parsed = ResultParser().parse(result, "claude")

    assert isinstance(parsed, ParsedResult)
    assert parsed.files_changed == ["x.py"]
    assert parsed.token_usage == {"input_tokens": 5}
    assert parsed.duration_seconds == 2.5
