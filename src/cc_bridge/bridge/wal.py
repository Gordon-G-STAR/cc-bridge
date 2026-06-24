"""逐文件内容 WAL，用于 handoff 越界改动回滚。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import config

_DEFAULT_MAX_FILES = 2000
_DEFAULT_MAX_BYTES = 50 * 1024 * 1024


@dataclass
class RollbackResult:
    reverted: list[str]
    failed: list[str]
    missing_baseline: list[str]
    skipped: bool = False


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


def _wal_root(*, create: bool) -> Path:
    root = config.stable_app_dir() / "wal"
    if create:
        root.mkdir(parents=True, exist_ok=True)
        _chmod_private_dir(root)
    return root


def _handoff_dir(handoff_id: str, *, create: bool) -> Path:
    handoff = str(handoff_id)
    if not handoff or any(ch in handoff for ch in "/\\") or handoff in {".", ".."}:
        raise ValueError("invalid handoff_id")
    directory = _wal_root(create=create) / handoff
    if create:
        directory.mkdir(parents=True, exist_ok=True)
        _chmod_private_dir(directory)
    return directory


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
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
    _atomic_write_bytes(path, text.encode("utf-8"))


def _read_manifest_path(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_manifest(handoff_id: str) -> dict[str, Any] | None:
    try:
        path = _handoff_dir(str(handoff_id), create=False) / "manifest.json"
    except ValueError:
        return None
    return _read_manifest_path(path)


def _normalize_rel_path(path: str | Path) -> str | None:
    raw = str(path)
    if not raw or raw.startswith(("/", "\\")):
        return None
    rel = Path(raw)
    if rel.is_absolute() or rel.drive:
        return None
    if any(part == ".." for part in rel.parts):
        return None
    normalized = rel.as_posix()
    if normalized in {"", "."}:
        return None
    return normalized


def _unique_rel_paths(paths: Iterable[str | Path]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        rel = _normalize_rel_path(path)
        if rel is None or rel in seen:
            continue
        seen.add(rel)
        out.append(rel)
    return out


def _resolve_under_root(root: Path, rel: str) -> Path | None:
    target = root / rel
    try:
        root_resolved = root.resolve(strict=False)
        target_resolved = target.resolve(strict=False)
        target_resolved.relative_to(root_resolved)
    except (OSError, ValueError):
        return None
    return target


def _write_skipped_manifest(handoff_id: str) -> None:
    directory = _handoff_dir(str(handoff_id), create=True)
    _atomic_write_json(directory / "manifest.json", {"state": "skipped_too_large"})


def record_baseline(
    handoff_id,
    root,
    rel_paths,
    *,
    max_files=_DEFAULT_MAX_FILES,
    max_bytes=_DEFAULT_MAX_BYTES,
) -> str:
    root_path = Path(root)
    rels = _unique_rel_paths(rel_paths)

    # 先只看元数据，超界或不可可靠读取时不承诺回滚。
    existing_files = 0
    total_bytes = 0
    try:
        for rel in rels:
            target = _resolve_under_root(root_path, rel)
            if target is None or not target.exists():
                continue
            if not target.is_file():
                _write_skipped_manifest(str(handoff_id))
                return "skipped_too_large"
            stat = target.stat()
            existing_files += 1
            total_bytes += stat.st_size
    except OSError:
        _write_skipped_manifest(str(handoff_id))
        return "skipped_too_large"

    if existing_files > max_files or total_bytes > max_bytes:
        _write_skipped_manifest(str(handoff_id))
        return "skipped_too_large"

    directory = _handoff_dir(str(handoff_id), create=True)
    blobs_dir = directory / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(blobs_dir)

    baseline: dict[str, dict[str, Any]] = {}
    try:
        for rel in rels:
            target = _resolve_under_root(root_path, rel)
            if target is None or not target.exists():
                baseline[rel] = {"sha": None, "existed": False}
                continue
            if not target.is_file():
                _write_skipped_manifest(str(handoff_id))
                return "skipped_too_large"

            data = target.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            blob = blobs_dir / sha
            if not blob.exists():
                _atomic_write_bytes(blob, data)
            baseline[rel] = {"sha": sha, "existed": True}
    except OSError:
        _write_skipped_manifest(str(handoff_id))
        return "skipped_too_large"

    _atomic_write_json(
        directory / "manifest.json",
        {
            "state": "ready",
            "baseline": baseline,
            "to_revert": [],
            "reverted": [],
        },
    )
    return "ready"


def _delete_file_if_present(path: Path) -> None:
    if path.exists() or path.is_symlink():
        path.unlink()


def _mark_reverted(manifest: dict[str, Any], rel: str) -> None:
    reverted = manifest.setdefault("reverted", [])
    if not isinstance(reverted, list):
        reverted = []
        manifest["reverted"] = reverted
    if rel not in reverted:
        reverted.append(rel)


def rollback(handoff_id, root, paths) -> RollbackResult:
    raw_paths = [str(path) for path in paths]
    manifest = _read_manifest(str(handoff_id))
    if not isinstance(manifest, dict):
        return RollbackResult(
            reverted=[], failed=raw_paths, missing_baseline=[], skipped=False
        )
    if manifest.get("state") == "skipped_too_large":
        return RollbackResult(
            reverted=[], failed=raw_paths, missing_baseline=[], skipped=True
        )

    rels = [(_normalize_rel_path(path), path) for path in raw_paths]
    to_revert = [rel if rel is not None else raw for rel, raw in rels]
    root_path = Path(root)
    try:
        directory = _handoff_dir(str(handoff_id), create=True)
    except ValueError:
        return RollbackResult(
            reverted=[], failed=raw_paths, missing_baseline=[], skipped=False
        )

    manifest["state"] = "reverting"
    manifest["to_revert"] = to_revert
    if not isinstance(manifest.get("reverted"), list):
        manifest["reverted"] = []
    _atomic_write_json(directory / "manifest.json", manifest)

    baseline = manifest.get("baseline")
    if not isinstance(baseline, dict):
        baseline = {}

    result = RollbackResult(reverted=[], failed=[], missing_baseline=[])
    for rel, raw in rels:
        if rel is None:
            result.failed.append(raw)
            continue

        target = _resolve_under_root(root_path, rel)
        if target is None:
            result.failed.append(rel)
            continue

        try:
            entry = baseline.get(rel)
            if isinstance(entry, dict):
                if entry.get("existed") is True:
                    sha = entry.get("sha")
                    if not isinstance(sha, str) or not sha:
                        result.failed.append(rel)
                        continue
                    blob = directory / "blobs" / sha
                    data = blob.read_bytes()
                    _atomic_write_bytes(target, data)
                else:
                    _delete_file_if_present(target)
            else:
                # baseline 没有该项时，只能按“越界新增”处理；记录证据，避免谎称有快照。
                result.missing_baseline.append(rel)
                _delete_file_if_present(target)
        except OSError:
            result.failed.append(rel)
            continue

        result.reverted.append(rel)
        _mark_reverted(manifest, rel)
        _atomic_write_json(directory / "manifest.json", manifest)

    reverted_set = set(manifest.get("reverted", []))
    to_revert_set = set(to_revert)
    if not result.failed and to_revert_set.issubset(reverted_set):
        manifest["state"] = "reverted"
    else:
        manifest["state"] = "reverting"
    _atomic_write_json(directory / "manifest.json", manifest)
    return result


def pending_rollbacks() -> list[str]:
    root = _wal_root(create=False)
    try:
        directories = [path for path in root.iterdir() if path.is_dir()]
    except OSError:
        return []

    pending: list[str] = []
    for directory in sorted(directories, key=lambda path: path.name):
        manifest = _read_manifest_path(directory / "manifest.json")
        if not isinstance(manifest, dict) or manifest.get("state") != "reverting":
            continue
        to_revert = manifest.get("to_revert")
        reverted = manifest.get("reverted")
        if not isinstance(to_revert, list) or not isinstance(reverted, list):
            continue
        if set(to_revert) - set(reverted):
            pending.append(directory.name)
    return pending


def cleanup(handoff_id) -> None:
    try:
        directory = _handoff_dir(str(handoff_id), create=False)
        shutil.rmtree(directory)
    except (OSError, ValueError):
        pass
