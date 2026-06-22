"""ContextBuilder 离线单测。

在 tmp_path 造一个最小假项目，验证语言探测、关键文件收集、目录树渲染，
以及 build_task_prompt 的拼装结果。git 信息在非仓库目录下应安全降级。
"""

from __future__ import annotations

from cc_bridge.bridge.context import ContextBuilder, ProjectContext


def _make_fake_project(root):
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.1.0'\n", encoding="utf-8"
    )
    (root / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (root / "utils.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")


def test_build_project_context_detects_python(tmp_path):
    _make_fake_project(tmp_path)
    ctx = ContextBuilder().build_project_context(str(tmp_path))

    assert isinstance(ctx, ProjectContext)
    assert ctx.language == "Python"
    assert ctx.root == str(tmp_path.resolve())


def test_build_project_context_collects_key_files(tmp_path):
    _make_fake_project(tmp_path)
    ctx = ContextBuilder().build_project_context(str(tmp_path))

    assert "pyproject.toml" in ctx.key_files
    assert "README.md" in ctx.key_files
    assert "name = 'demo'" in ctx.key_files["pyproject.toml"]


def test_build_project_context_tree_contains_filenames(tmp_path):
    _make_fake_project(tmp_path)
    ctx = ContextBuilder().build_project_context(str(tmp_path))

    assert "main.py" in ctx.tree
    assert "utils.py" in ctx.tree
    assert "pkg/" in ctx.tree


def test_unknown_language_when_no_source_files(tmp_path):
    (tmp_path / "notes.txt").write_text("just notes\n", encoding="utf-8")
    ctx = ContextBuilder().build_project_context(str(tmp_path))

    assert ctx.language == "Unknown"


def test_build_task_prompt_contains_task_and_root(tmp_path):
    _make_fake_project(tmp_path)
    builder = ContextBuilder()
    ctx = builder.build_project_context(str(tmp_path))

    prompt = builder.build_task_prompt("请修复登录 bug", ctx, caller="claude")

    # 任务原文
    assert "请修复登录 bug" in prompt
    # 项目根目录
    assert ctx.root in prompt
    # 调用方身份
    assert "Claude" in prompt
    # 语言信息
    assert "Python" in prompt


def test_build_task_prompt_caller_codex_label(tmp_path):
    _make_fake_project(tmp_path)
    builder = ContextBuilder()
    ctx = builder.build_project_context(str(tmp_path))

    prompt = builder.build_task_prompt("分析架构", ctx, caller="codex")

    assert "Codex" in prompt
    assert "分析架构" in prompt


def test_build_task_prompt_includes_key_file_section(tmp_path):
    _make_fake_project(tmp_path)
    builder = ContextBuilder()
    ctx = builder.build_project_context(str(tmp_path))

    prompt = builder.build_task_prompt("做事", ctx, caller="claude")

    assert "pyproject.toml" in prompt
    assert "关键配置文件" in prompt


# ---------------------------------------------------------------------------
# require_project_dir：强制显式、绝对、存在的项目目录
# ---------------------------------------------------------------------------

import os  # noqa: E402

import pytest  # noqa: E402

from cc_bridge.bridge.context import require_project_dir  # noqa: E402


def test_require_project_dir_accepts_valid_absolute(tmp_path):
    assert require_project_dir(str(tmp_path)) == str(tmp_path)


def test_require_project_dir_rejects_none():
    with pytest.raises(ValueError, match="project_dir"):
        require_project_dir(None)


def test_require_project_dir_rejects_empty():
    with pytest.raises(ValueError):
        require_project_dir("   ")


def test_require_project_dir_rejects_relative():
    with pytest.raises(ValueError, match="绝对路径"):
        require_project_dir("some/relative/dir")


def test_require_project_dir_rejects_nonexistent(tmp_path):
    with pytest.raises(ValueError):
        require_project_dir(str(tmp_path / "does-not-exist"))


# ---------------------------------------------------------------------------
# CC_BRIDGE_ALLOWED_ROOTS：工作区白名单（纵深防御，opt-in）
# ---------------------------------------------------------------------------

def test_no_allowlist_is_unrestricted(tmp_path, monkeypatch):
    """未设置白名单时不施加限制，行为与历史一致（向后兼容）。"""
    monkeypatch.delenv("CC_BRIDGE_ALLOWED_ROOTS", raising=False)
    assert require_project_dir(str(tmp_path)) == str(tmp_path)


def test_allowlist_accepts_within_root(tmp_path, monkeypatch):
    sub = tmp_path / "proj"
    sub.mkdir()
    monkeypatch.setenv("CC_BRIDGE_ALLOWED_ROOTS", str(tmp_path))
    assert require_project_dir(str(sub)) == str(sub)


def test_allowlist_accepts_root_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_BRIDGE_ALLOWED_ROOTS", str(tmp_path))
    assert require_project_dir(str(tmp_path)) == str(tmp_path)


def test_allowlist_rejects_outside_root(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("CC_BRIDGE_ALLOWED_ROOTS", str(allowed))
    with pytest.raises(ValueError, match="允许的工作区"):
        require_project_dir(str(outside))


def test_allowlist_rejects_dotdot_escape(tmp_path, monkeypatch):
    """用 .. 跳出允许根的，resolve 后会被拦下。"""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("CC_BRIDGE_ALLOWED_ROOTS", str(allowed))
    # allowed/.. → tmp_path，落在 allowed 之外
    with pytest.raises(ValueError, match="允许的工作区"):
        require_project_dir(str(allowed / ".."))


def test_allowlist_supports_multiple_roots(tmp_path, monkeypatch):
    r1 = tmp_path / "r1"
    r2 = tmp_path / "r2"
    r1.mkdir()
    r2.mkdir()
    monkeypatch.setenv("CC_BRIDGE_ALLOWED_ROOTS", os.pathsep.join([str(r1), str(r2)]))
    assert require_project_dir(str(r2)) == str(r2)


def test_build_task_prompt_injects_key_file_content_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CC_BRIDGE_INJECT_CONTEXT", raising=False)
    _make_fake_project(tmp_path)
    builder = ContextBuilder()
    ctx = builder.build_project_context(str(tmp_path))

    prompt = builder.build_task_prompt("do work", ctx, caller="claude")

    assert "name = 'demo'" in prompt


@pytest.mark.parametrize("raw", ["0", "false", "no", " FALSE ", "No"])
def test_build_task_prompt_can_disable_key_file_content(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("CC_BRIDGE_INJECT_CONTEXT", raw)
    _make_fake_project(tmp_path)
    builder = ContextBuilder()
    ctx = builder.build_project_context(str(tmp_path))

    prompt = builder.build_task_prompt("do work", ctx, caller="claude")

    assert "name = 'demo'" not in prompt
    assert "main.py" in prompt
    assert ctx.root in prompt
