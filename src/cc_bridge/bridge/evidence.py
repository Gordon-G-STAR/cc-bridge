"""Classify changed paths against writable scope and snapshot evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import scope, snapshot

_UNVERIFIABLE_REASON = "index flag hides on-disk state (skip-worktree/assume-unchanged)"
_OUTSIDE_SCOPE_REASON = "outside granted writable scope"


@dataclass(frozen=True)
class EvidenceResult:
    verified_files: list[str]
    scope_violations: list[str]
    unverifiable: list[str]
    evidence_level: str
    reasons: dict[str, str]


def baseline(root) -> dict[str, str | None]:
    return snapshot.snapshot_files(root, snapshot.list_snapshot_targets(root))


def baseline_targets(root) -> list[str]:
    return snapshot.list_snapshot_targets(root)


def gather(root, baseline_snapshot, writable_paths=()) -> EvidenceResult:
    targets = snapshot.list_snapshot_targets(root)
    after = snapshot.snapshot_files(root, targets)
    changed = snapshot.diff_snapshots(baseline_snapshot, after)
    unver = snapshot.unverifiable_paths(root)
    git_available = snapshot.is_git_repo(root)
    return classify_changes(
        root,
        changed,
        list(writable_paths),
        unverifiable=unver,
        git_available=git_available,
    )


def _normalize_relpaths(paths) -> list[str]:
    return sorted({str(path).replace("\\", "/") for path in paths})


def _is_granted(root: Path, relpath: str, writable_paths: list[str]) -> bool:
    child = root / relpath
    return any(scope.is_within(child, root / writable) for writable in writable_paths)


def classify_changes(
    root,
    changed_relpaths,
    writable_paths,
    *,
    unverifiable=(),
    git_available=True,
) -> EvidenceResult:
    root_path = Path(root)
    changed = _normalize_relpaths(changed_relpaths)
    writable = _normalize_relpaths(writable_paths)
    unverifiable_set = set(_normalize_relpaths(unverifiable))

    verified_files: list[str] = []
    scope_violations: list[str] = []
    unverifiable_files: list[str] = []
    reasons: dict[str, str] = {}

    for relpath in changed:
        if relpath in unverifiable_set:
            unverifiable_files.append(relpath)
            reasons[relpath] = _UNVERIFIABLE_REASON
            continue

        taints = scope.path_taints(root_path / relpath)
        if taints:
            scope_violations.append(relpath)
            reasons[relpath] = "tainted: " + ",".join(taints)
            continue

        if not _is_granted(root_path, relpath, writable):
            scope_violations.append(relpath)
            reasons[relpath] = _OUTSIDE_SCOPE_REASON
            continue

        verified_files.append(relpath)

    if not git_available:
        evidence_level = "unknown"
    elif unverifiable_files:
        evidence_level = "best_effort"
    else:
        evidence_level = "verified"

    return EvidenceResult(
        verified_files=verified_files,
        scope_violations=scope_violations,
        unverifiable=unverifiable_files,
        evidence_level=evidence_level,
        reasons=reasons,
    )
