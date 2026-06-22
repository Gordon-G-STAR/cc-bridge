"""PR2a —— containment 基石的对抗性测试矩阵。

这个函数一旦写歪,作用域强制 / scope_hash / 证据归因会一起烂,所以这里把已知的
绕过形态逐个钉死(跨平台部分;Windows 专属的硬链接/ADS/junction 在 PR2b)。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cc_bridge.bridge.scope import (
    is_dotgit_path,
    is_reserved_component,
    is_within,
    path_has_ads,
    path_has_reserved_name,
    path_taints,
    reparse_tag,
    resolve_within_root,
)


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

def test_resolve_failures_fail_closed(monkeypatch, tmp_path):
    def fail_resolve(self, *args, **kwargs):
        raise OSError("forced resolve failure")

    monkeypatch.setattr(Path, "resolve", fail_resolve)

    r = resolve_within_root("src/auth/session.py", str(tmp_path))
    assert r.within_root is False
    assert r.reason
    assert is_within(tmp_path / "src" / "auth" / "session.py", tmp_path) is False


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


# ---------------------------------------------------------------------------
# PR2b —— Windows 危险路径形态检测(词法部分跨平台,真实 fs 部分仅 Windows)
# ---------------------------------------------------------------------------

def test_reserved_name_detection():
    assert path_has_reserved_name("src/NUL")
    assert path_has_reserved_name("a/con.txt/b")        # 带扩展名仍是保留名
    assert is_reserved_component("COM1")
    assert is_reserved_component("nul")                  # 大小写不敏感
    assert is_reserved_component("PRN. ")                # 尾部点/空格被吞
    assert not path_has_reserved_name("src/auth/session.py")
    assert not is_reserved_component("console")          # 不是精确保留名


def test_ads_detection():
    assert path_has_ads("README.md:payload")
    assert path_has_ads("C:/proj/README.md:payload")     # 盘符之后的冒号
    assert path_has_ads("src/auth:secret")
    assert not path_has_ads("C:/proj/README.md")         # 盘符锚点的冒号不算
    assert not path_has_ads("src/auth/session.py")


def test_resolve_flags_reserved_and_ads_lexically(tmp_path):
    assert "reserved_name" in resolve_within_root("NUL", str(tmp_path)).taints
    assert "ads" in resolve_within_root("README.md:payload", str(tmp_path)).taints
    assert resolve_within_root("src/auth/x.py", str(tmp_path)).taints == ()


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS 硬链接(os.link,无需管理员)")
def test_hardlink_detected_on_windows(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("x", encoding="utf-8")
    alias = tmp_path / "alias.txt"
    os.link(real, alias)
    assert "hardlink_aliased" in path_taints(alias)
    assert "hardlink_aliased" in path_taints(real)


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS ADS 真实创建")
def test_real_ads_stream_on_windows(tmp_path):
    base = tmp_path / "doc.txt"
    base.write_text("base", encoding="utf-8")
    ads = f"{base}:hidden"
    with open(ads, "w", encoding="utf-8") as fh:
        fh.write("payload")
    assert path_has_ads(ads)


@pytest.mark.skipif(sys.platform != "win32", reason="junction is a Windows reparse point (mklink /J)")
def test_junction_detected_on_windows(tmp_path):
    import subprocess

    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "jx"
    res = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True, text=True,
    )
    if res.returncode != 0 or not junction.exists():
        pytest.skip(f"cannot create junction: {res.stderr or res.stdout}")
    assert reparse_tag(junction) is not None
    assert "junction" in path_taints(junction)


@pytest.mark.skipif(sys.platform != "win32", reason="junction is a Windows reparse point (mklink /J)")
def test_missing_leaf_under_junction_inherits_ancestor_taint(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "project"
    root.mkdir()
    junction = root / "jx"
    res = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0 or not junction.exists():
        pytest.skip(f"cannot create junction: {res.stderr or res.stdout}")

    r = resolve_within_root("jx/missing-leaf.txt", str(root))
    assert "junction" in r.taints


def test_dotgit_detection():
    assert is_dotgit_path(".git/hooks/pre-commit")
    assert is_dotgit_path("a/.git/config")
    assert is_dotgit_path(".git")
    assert is_dotgit_path("a/.GIT/x")
    assert is_dotgit_path("a/.git./x")
    assert is_dotgit_path("a/.git /x")
    assert not is_dotgit_path("src/git/x")        # 'git' != '.git'
    assert not is_dotgit_path("src/.gitignore")
    assert not is_dotgit_path("src/.github/ci")   # '.github' != '.git'


def test_resolve_flags_dotgit(tmp_path):
    assert "dotgit" in resolve_within_root(".git/config", str(tmp_path)).taints
    assert "dotgit" in path_taints(".git/hooks/x")
