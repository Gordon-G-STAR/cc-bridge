"""ResultParser 离线单测。

构造各种 ExecutionResult，验证：
- 失败分类：quota / auth / timeout / error；
- 成功/失败摘要文案；
- files_changed 渲染；
- 超长 summary 被截断到 max_length 以内。
"""

from __future__ import annotations

from cc_bridge.bridge.parser import ParsedResult, ResultParser


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

    assert len(text) <= 500
    # 截断标记应出现
    assert "省略" in text


def test_summarize_short_summary_not_truncated(make_execution_result):
    result = make_execution_result(success=True, output="短短的输出")
    parser = ResultParser()
    parsed = parser.parse(result, "claude")

    text = parser.summarize_for_caller(parsed, "claude", max_length=4000)

    assert "省略" not in text
    assert "短短的输出" in text


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
