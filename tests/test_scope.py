"""PR2a —— containment 基石的对抗性测试矩阵。

这个函数一旦写歪,作用域强制 / scope_hash / 证据归因会一起烂,所以这里把已知的
绕过形态逐个钉死(跨平台部分;Windows 专属的硬链接/ADS/junction 在 PR2b)。
"""

from __future__ import annotations

import sys

import pytest

from cc_bridge.bridge.scope import is_within, resolve_within_root


# ---------------------------------------------------------------------------
# 分量匹配,不是字符串前缀
# ---------------------------------------------------------------------------

def test_prefix_is_component_wise_not_string(tmp_path):
    root = tmp_path
    assert is_within(root / "src" / "auth" / "session.py", root / "src" / "auth")
    # 关键反例:字符串前缀会把 src/auth_secrets 误判为在 src/auth 内
    assert not is_within(root / "src" / "auth_secrets" / "x.py", root / "src" / "auth")


def test_is_within_self_and_parent(tmp_path):
    assert is_within(tmp_path, tmp_path)            # 自身算在内
    assert is_within(tmp_path / "a", tmp_path)
    assert not is_within(tmp_path, tmp_path / "a")  # 父不在子内


# ---------------------------------------------------------------------------
# resolve_within_root:正常 / .. 逃逸 / 绝对
# ---------------------------------------------------------------------------

def test_accepts_normal_relative(tmp_path):
    r = resolve_within_root("src/auth/session.py", str(tmp_path))
    assert r.within_root
    assert r.project_relative.replace("\\", "/") == "src/auth/session.py"
    assert r.reason is None


def test_self_dot_is_within(tmp_path):
    r = resolve_within_root(".", str(tmp_path))
    assert r.within_root
    assert r.project_relative == "."


@pytest.mark.parametrize("bad", ["../outside", "a/../../b", "x/../../../etc", "../../.."])
def test_rejects_dotdot_escape(tmp_path, bad):
    r = resolve_within_root(bad, str(tmp_path))
    assert not r.within_root
    assert r.reason


def test_absolute_request_does_not_escape(tmp_path):
    # 即便有人绕过 contracts 词法层塞进绝对路径,resolve 后也越界 => 拒绝。
    other = "/etc/passwd" if sys.platform != "win32" else "C:/Windows/System32"
    r = resolve_within_root(other, str(tmp_path))
    assert not r.within_root


# ---------------------------------------------------------------------------
# 符号链接逃逸(POSIX;Windows 上建符号链接需管理员,junction 见 PR2b)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows 建符号链接需管理员;无需提权的 junction 在 PR2b",
)
def test_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)

    r = resolve_within_root("link/secret.txt", str(root))
    assert not r.within_root          # 解析符号链接后越界 => 拒绝
    assert str(outside.resolve()) in r.resolved_absolute


# ---------------------------------------------------------------------------
# Windows 大小写不敏感
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows 大小写不敏感语义")
def test_windows_case_insensitive(tmp_path):
    assert is_within(tmp_path / "SRC" / "Auth", tmp_path / "src" / "auth")
    r = resolve_within_root("SRC/Auth", str(tmp_path / "src"))
    assert r.within_root
