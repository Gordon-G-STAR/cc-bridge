"""路径作用域(containment)—— v0.2 安全内核的基石。

把"调用方申请的相对路径"解析成绝对真实路径,并判断它是否落在项目根内。原则:

- **分量级判断,绝不字符串前缀**:``src/auth`` 不能命中 ``src/auth_secrets``。
- **两侧都先 resolve(解符号链接 + ``..``)**:符号链接 / ``..`` 逃逸在这一步被抓到
  (词法层看不到运行期的符号链接)。
- Windows 上 pathlib 的路径比较本就大小写不敏感。

注意边界:containment 是【检测 + 补救】,不是阻断沙箱——它能判定越界,但在落盘
发生前通常拦不住 CLI。更深的 Windows 检测(硬链接 file-id、ADS 流、reparse tag、
保留设备名)在 PR2b;本模块先把跨平台的核心判定 + resolve 逃逸做对、做严、可对抗测。
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


def _resolve(path: Path) -> Path:
    """resolve(解符号链接 + ``..``);不存在的尾部按词法拼接,不报错。

    极端情况(循环符号链接等)resolve 会抛 ``OSError``/``RuntimeError``,
    回退到 ``absolute()``(仅词法)——调用方据 within_root=False 保守处理。
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()


def is_within(child: Path, parent: Path) -> bool:
    """``child`` 解析后是否落在 ``parent`` 之内(含 ``parent`` 本身)。

    分量级:``src/auth`` 不会命中 ``src/auth_secrets``。两侧都 resolve,故符号链接 /
    ``..`` 逃逸会被抓到。Windows 上路径比较大小写不敏感。
    """
    c = _resolve(child)
    p = _resolve(parent)
    if c == p:
        return True
    try:
        return c.is_relative_to(p)
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Windows 危险路径形态检测(PR2b)
#
# 这些是 containment 在 Windows 上的盲区:名字落在根内、resolve 也不越界,但底层仍
# 可能把写入引到别处(硬链接 / junction)、或藏在看不见的流里(ADS)、或把写入吞成
# 设备(保留名)。统一返回 "taint" 标签——命中任何一条都【绝不得标 verified】。
# 用 os.stat / os.lstat,无需 ctypes:st_nlink 给硬链接数、st_ino/st_dev 给 file-id、
# st_reparse_tag 给 reparse 类型。
# ---------------------------------------------------------------------------

_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

_IO_REPARSE_TAG_MOUNT_POINT = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
_IO_REPARSE_TAG_SYMLINK = getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C)


def is_reserved_component(component: str) -> bool:
    """单个路径分量(去尾部点/空格、取扩展名前的名字)是否 Windows 保留设备名。"""
    name = component.strip().rstrip(". ")
    stem = name.split(".", 1)[0] if "." in name else name
    return stem.upper() in _RESERVED_DEVICE_NAMES


def path_has_reserved_name(path_str: str) -> bool:
    norm = str(path_str).replace("\\", "/")
    return any(is_reserved_component(c) for c in norm.split("/") if c)


def path_has_ads(path_str: str) -> bool:
    """路径是否含 NTFS ADS 流分隔符 ':'(盘符锚点 'C:' 之后的任何冒号)。"""
    norm = str(path_str).replace("\\", "/")
    body = norm
    if len(norm) >= 2 and norm[1] == ":" and norm[0].isalpha():
        body = norm[2:]
    return ":" in body


def hardlink_count(path) -> int | None:
    try:
        return os.stat(path).st_nlink
    except OSError:
        return None


def file_identity(path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` —— 指向同一 inode 的两个名字会得到相同 file-id。"""
    try:
        st = os.stat(path)
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


def reparse_tag(path) -> int | None:
    """若 ``path`` 自身是 reparse point,返回其 tag;否则 None(用 lstat 不跟随)。"""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        return getattr(st, "st_reparse_tag", 0)
    return None


def path_taints(path) -> list[str]:
    """返回一个路径检测到的危险属性标签(空 = 干净)。

    词法层(路径不存在也能判):``ads`` / ``reserved_name``;
    文件系统层(路径存在时):``hardlink_aliased`` / ``junction`` / ``symlink`` /
    ``reparse_point``。供证据归因(PR4)与策略(PR5)使用——命中任何一条都不得标 verified。
    """
    p = str(path)
    taints: list[str] = []
    if path_has_ads(p):
        taints.append("ads")
    if path_has_reserved_name(p):
        taints.append("reserved_name")
    nlink = hardlink_count(path)
    if nlink is not None and nlink > 1:
        taints.append("hardlink_aliased")
    tag = reparse_tag(path)
    if tag is not None:
        if tag == _IO_REPARSE_TAG_MOUNT_POINT:
            taints.append("junction")
        elif tag == _IO_REPARSE_TAG_SYMLINK:
            taints.append("symlink")
        else:
            taints.append("reparse_point")
    return taints


@dataclass(frozen=True)
class ResolvedPathIdentity:
    """一条申请路径解析后的身份。PR2b 会补 file-id / link_count / 流 / reparse tag。"""

    requested: str               # 合同里给的原始(相对)路径
    project_root: str            # resolve 后的项目根
    resolved_absolute: str       # resolve 后的绝对真实路径(解符号链接 + ..)
    project_relative: str | None  # 相对项目根的路径;越界则 None
    within_root: bool
    exists: bool
    is_symlink: bool             # 申请路径自身是否符号链接
    taints: tuple[str, ...] = ()  # 危险属性:ads/reserved_name/hardlink_aliased/junction/...
    reason: str | None = None    # 越界/异常原因


def resolve_within_root(requested: str, project_root: str) -> ResolvedPathIdentity:
    """把一条申请的(相对)路径解析到项目根下,判定是否越界。

    ``requested`` 期望已由 contracts 词法层保证为相对、无 ``..``、无冒号;但这里仍做
    运行期 resolve 兜底(符号链接 / ``..`` 在词法层看不到)。
    """
    root = _resolve(Path(project_root))
    raw = Path(project_root) / requested
    target = _resolve(raw)

    try:
        within = target == root or target.is_relative_to(root)
    except (OSError, ValueError):
        within = False

    project_relative: str | None = None
    reason: str | None = None
    if within:
        try:
            project_relative = "." if target == root else str(target.relative_to(root))
        except ValueError:
            within = False
    if not within:
        reason = f"解析后越出项目根:{target} 不在 {root} 内"

    try:
        is_symlink = raw.is_symlink()
    except OSError:
        is_symlink = False

    # taints:词法层(ads/reserved)对 requested 恒判;文件系统层仅当 raw 真实存在。
    detected: list[str] = []
    if path_has_ads(requested):
        detected.append("ads")
    if path_has_reserved_name(requested):
        detected.append("reserved_name")
    if is_symlink or raw.exists():
        for tag in path_taints(raw):
            if tag not in detected:
                detected.append(tag)

    return ResolvedPathIdentity(
        requested=requested,
        project_root=str(root),
        resolved_absolute=str(target),
        project_relative=project_relative,
        within_root=within,
        exists=target.exists(),
        is_symlink=is_symlink,
        taints=tuple(detected),
        reason=reason,
    )
