from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

_AUDIT_LOG_ENV = "CC_BRIDGE_AUDIT_LOG"
_MAX_TASK_SUMMARY_CHARS = 200


def _task_summary(task: str) -> str:
    text = " ".join(str(task).split())
    if len(text) <= _MAX_TASK_SUMMARY_CHARS:
        return text
    return text[: _MAX_TASK_SUMMARY_CHARS - 3] + "..."


def append_audit_record(
    *,
    direction: Literal["codex", "claude"],
    cwd: str,
    task: str,
    success: bool,
    files_changed: list[str],
) -> None:
    log_path = os.environ.get(_AUDIT_LOG_ENV)
    if not log_path or not log_path.strip():
        return

    try:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "direction": direction,
            "cwd": str(cwd),
            "task_summary": _task_summary(task),
            "success": bool(success),
            "files_changed": list(files_changed or []),
        }
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
    except Exception:
        pass
