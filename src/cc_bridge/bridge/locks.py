"""Cross-process project locks for bridge handoffs."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import sys
import time
from pathlib import Path

from cc_bridge.bridge import config, wal

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class LockBusy(Exception):
    """Raised when a project lock cannot be acquired before the timeout."""


def _canonical_project_identity(project_dir: str | os.PathLike[str]) -> str:
    identity = str(Path(project_dir).resolve())
    if sys.platform == "win32":
        identity = identity.casefold()
    return identity


def _lock_key(project_dir: str | os.PathLike[str]) -> str:
    identity = _canonical_project_identity(project_dir)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _lock_path(project_dir: str | os.PathLike[str]) -> Path:
    key = _lock_key(project_dir)
    locks_dir = config.stable_app_dir() / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    return locks_dir / f"{key}.lock"


def _open_lock_file(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(path, flags, 0o666)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except Exception:
        os.close(fd)
        raise


def _try_lock(fd: int) -> bool:
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        if sys.platform == "win32":
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if sys.platform == "win32":
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _resume_pending_rollbacks(project_dir: str | os.PathLike[str]) -> None:
    """锁拿到后、放行新 handoff 前,续完上次崩溃中断的回滚(WAL acquire-scan)。"""
    try:
        for handoff_id in wal.pending_rollbacks():
            manifest = wal._read_manifest(handoff_id)
            if not isinstance(manifest, dict):
                continue
            to_revert = manifest.get("to_revert", [])
            reverted = set(manifest.get("reverted", []))
            remaining = [p for p in to_revert if p not in reverted]
            if remaining:
                wal.rollback(handoff_id, str(project_dir), remaining)
    except Exception:
        pass


@contextlib.contextmanager
def project_lock(
    project_dir: str | os.PathLike[str],
    *,
    timeout: float = 10.0,
    poll: float = 0.1,
):
    """Acquire an exclusive crash-releasable lock for one project."""

    path = _lock_path(project_dir)
    fd = _open_lock_file(path)
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            if _try_lock(fd):
                acquired = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LockBusy(
                    "Project lock is busy; another bridge process is already "
                    f"handling this project; key={_lock_key(project_dir)}"
                )
            time.sleep(min(poll, remaining))
        _resume_pending_rollbacks(project_dir)
        yield path
    finally:
        try:
            if acquired:
                with contextlib.suppress(OSError):
                    _unlock(fd)
        finally:
            os.close(fd)


@contextlib.asynccontextmanager
async def async_project_lock(
    project_dir: str | os.PathLike[str],
    *,
    timeout: float = 5.0,
):
    """Acquire a project lock without blocking the event loop."""

    cm = project_lock(project_dir, timeout=timeout)
    await asyncio.to_thread(cm.__enter__)
    try:
        yield
    finally:
        await asyncio.to_thread(cm.__exit__, None, None, None)


__all__ = ["LockBusy", "async_project_lock", "project_lock"]
