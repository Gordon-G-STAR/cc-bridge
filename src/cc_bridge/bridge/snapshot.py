from __future__ import annotations

import hashlib
import os
import pathlib


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
