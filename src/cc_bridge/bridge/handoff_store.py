"""异步 handoff 的跨进程文件状态存储。"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config
from .contracts import HandoffRequest, HandoffResult

_STATUS_STATES = frozenset(
    {
        "pending",
        "running",
        "completed",
        "failed",
        "scope_violation",
        "policy_denied",
        "interrupted",
        # HandoffResult 现有终态包含 approval_required；runner 遇到早拒时要能如实落盘。
        "approval_required",
    }
)
_TERMINAL_STATES = _STATUS_STATES - {"pending", "running"}
_ACTIVE_STATES = frozenset({"pending", "running"})

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def _handoffs_root() -> Path:
    root = config.stable_app_dir() / "handoffs"
    root.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(root)
    return root


def _chmod_private_dir(path: Path) -> None:
    if config.IS_WINDOWS:
        return
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _chmod_private_file(path: str | Path) -> None:
    if config.IS_WINDOWS:
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 + os.replace，避免跨进程读到半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        _chmod_private_file(tmp)
        os.replace(tmp, path)
        _chmod_private_file(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    _atomic_write_text(path, text)


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def handoff_dir(handoff_id: str) -> Path:
    return _handoffs_root() / str(handoff_id)


def init_handoff(request: HandoffRequest, cwd: str, agent: str, caller: str) -> str:
    for _ in range(100):
        handoff_id = uuid.uuid4().hex[:12]
        directory = handoff_dir(handoff_id)
        try:
            directory.mkdir(parents=True, exist_ok=False)
            _chmod_private_dir(directory)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("无法生成唯一 handoff_id")

    _atomic_write_json(
        directory / "request.json",
        {
            "handoff_id": handoff_id,
            "request": request.model_dump(mode="json"),
            "cwd": cwd,
            "agent": agent,
            "caller": caller,
        },
    )
    write_status(handoff_id, "pending")
    return handoff_id


def read_spec(handoff_id: str) -> dict[str, Any] | None:
    data = _read_json(handoff_dir(handoff_id) / "request.json")
    if not isinstance(data, dict):
        return None
    try:
        request = HandoffRequest.model_validate(data.get("request"))
    except Exception:
        return None
    out = dict(data)
    out["request"] = request
    return out


def write_status(handoff_id: str, state: str, note: str = "") -> None:
    if state not in _STATUS_STATES:
        raise ValueError(f"unsupported handoff state: {state}")
    _atomic_write_json(
        handoff_dir(handoff_id) / "status.json",
        {
            "state": state,
            "note": note,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def read_status(handoff_id: str) -> dict[str, Any] | None:
    data = _read_json(handoff_dir(handoff_id) / "status.json")
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("state"), str):
        return None
    return data


def write_result(handoff_id: str, result: HandoffResult) -> None:
    _atomic_write_json(
        handoff_dir(handoff_id) / "result.json",
        result.model_dump(mode="json"),
    )


def read_result(handoff_id: str) -> HandoffResult | None:
    data = _read_json(handoff_dir(handoff_id) / "result.json")
    if not isinstance(data, dict):
        return None
    try:
        return HandoffResult.model_validate(data)
    except Exception:
        return None


def write_pid(handoff_id: str, pid: int) -> None:
    _atomic_write_text(handoff_dir(handoff_id) / "runner.pid", str(int(pid)))


def read_pid(handoff_id: str) -> int | None:
    try:
        raw = (handoff_dir(handoff_id) / "runner.pid").read_text(encoding="utf-8")
        pid = int(raw.strip())
    except (OSError, TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def pid_alive(pid: int) -> bool:
    """跨平台判断 PID 是否仍在运行。"""
    if pid <= 0:
        return False

    if not config.IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

        handle = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_uint32()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                # 检测失败时保守认为还活着，避免误报 interrupted。
                return True
            return exit_code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        # Windows 进程 API 探测异常时宁可漏报中断，也不假装 runner 已结束。
        return True


def runner_alive(handoff_id: str) -> bool:
    pid = read_pid(handoff_id)
    if pid is None:
        return False
    return pid_alive(pid)


def count_active() -> int:
    active = 0
    for handoff_id in list_handoffs():
        status = read_status(handoff_id)
        if status is None or status.get("state") not in _ACTIVE_STATES:
            continue
        if runner_alive(handoff_id):
            active += 1
    return active


def kill_runner(pid: int) -> bool:
    """杀掉 runner 进程树；失败只返回 False，由调用方转成状态。"""
    if pid <= 0:
        return False

    if config.IS_WINDOWS:
        try:
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
                **config.subprocess_creation_kwargs(),
            )
            return completed.returncode == 0
        except Exception:
            return False

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except ProcessLookupError:
        return True
    except Exception:
        return False


def list_handoffs() -> list[str]:
    try:
        return sorted(p.name for p in _handoffs_root().iterdir() if p.is_dir())
    except OSError:
        return []


def prune(keep: int = 50) -> None:
    """只清理终态目录；任何 IO 问题都静默跳过，不能影响主流程。"""
    keep = max(0, int(keep))
    try:
        directories = [p for p in _handoffs_root().iterdir() if p.is_dir()]
    except OSError:
        return

    terminal: list[Path] = []
    for directory in directories:
        status = read_status(directory.name)
        if status is not None and status.get("state") in _TERMINAL_STATES:
            terminal.append(directory)
    if len(terminal) <= keep:
        return

    try:
        terminal.sort(key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for directory in terminal[: len(terminal) - keep]:
        try:
            shutil.rmtree(directory)
        except OSError:
            pass
