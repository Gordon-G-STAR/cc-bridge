from __future__ import annotations

import hashlib
import os
import pathlib

from . import config

_GIT_SNAPSHOT_TIMEOUT_SECONDS = 10


def _git_or_none() -> str | None:
    return config.resolve_cli("git")


def _decode_git_stdout(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _normalize_relpath(path: str) -> str:
    return path.replace("\\", "/")


def _parse_nul_paths(data: bytes) -> set[str]:
    paths: set[str] = set()
    for token in _decode_git_stdout(data).split("\0"):
        if token:
            paths.add(_normalize_relpath(token))
    return paths


def list_snapshot_targets(root) -> list[str]:
    git = _git_or_none()
    if not git:
        return []

    root_text = os.fspath(root)
    tracked = config.git_capture(
        git,
        root_text,
        ["ls-files", "-z"],
        timeout=_GIT_SNAPSHOT_TIMEOUT_SECONDS,
    )
    if tracked.returncode != 0:
        return []

    targets = _parse_nul_paths(tracked.stdout)
    untracked = config.git_capture(
        git,
        root_text,
        ["ls-files", "-z", "--others", "--exclude-standard"],
        timeout=_GIT_SNAPSHOT_TIMEOUT_SECONDS,
    )
    if untracked.returncode == 0:
        targets.update(_parse_nul_paths(untracked.stdout))
    return sorted(targets)


def unverifiable_paths(root) -> set[str]:
    git = _git_or_none()
    if not git:
        return set()

    result = config.git_capture(
        git,
        os.fspath(root),
        ["ls-files", "-v"],
        timeout=_GIT_SNAPSHOT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return set()

    paths: set[str] = set()
    for line in _decode_git_stdout(result.stdout).splitlines():
        tag, separator, path = line.partition(" ")
        if not separator or not tag or not path:
            continue
        flag = tag[0]
        if flag in {"S", "s"} or (flag.isalpha() and flag.islower()):
            paths.add(_normalize_relpath(path))
    return paths


def sha256_file(path) -> str | None:
    file_path = pathlib.Path(path)
    try:
        if not file_path.is_file():
            return None
        digest = hashlib.sha256()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def snapshot_files(root, rel_paths) -> dict[str, str | None]:
    root_path = pathlib.Path(root)
    result = {}
    for rel_path in rel_paths:
        rel_text = os.fspath(rel_path).replace("\\", "/")
        result[rel_text] = sha256_file(root_path / rel_text)
    return result


def diff_snapshots(before, after) -> list[str]:
    changed = []
    for rel_path in before.keys() | after.keys():
        if rel_path not in before or rel_path not in after:
            changed.append(rel_path)
        elif before[rel_path] != after[rel_path]:
            changed.append(rel_path)
    return sorted(changed)
