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


@dataclass(frozen=True)
class ResolvedPathIdentity:
    """一条申请路径解析后的身份。PR2b 会补 file-id / link_count / 流 / reparse tag。"""

    requested: str               # 合同里给的原始(相对)路径
    project_root: str            # resolve 后的项目根
    resolved_absolute: str       # resolve 后的绝对真实路径(解符号链接 + ..)
    project_relative: str | None  # 相对项目根的路径;越界则 None
    within_root: bool
    exists: bool
    is_symlink: bool             # 申请路径自身是否符号链接(父链 reparse 检测在 PR2b)
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

    return ResolvedPathIdentity(
        requested=requested,
        project_root=str(root),
        resolved_absolute=str(target),
        project_relative=project_relative,
        within_root=within,
        exists=target.exists(),
        is_symlink=is_symlink,
        reason=reason,
    )
