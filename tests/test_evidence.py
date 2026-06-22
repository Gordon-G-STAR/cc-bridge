from __future__ import annotations

from cc_bridge.bridge.evidence import classify_changes


def test_empty_writable_scope_reports_changed_file_outside_scope(tmp_path):
    path = tmp_path / "src" / "auth.py"
    path.parent.mkdir()
    path.write_text("x", encoding="utf-8")

    result = classify_changes(tmp_path, ["src/auth.py"], [])

    assert result.verified_files == []
    assert result.scope_violations == ["src/auth.py"]
    assert result.unverifiable == []
    assert result.evidence_level == "verified"
    assert result.reasons == {"src/auth.py": "outside granted writable scope"}


def test_in_scope_changed_file_is_verified(tmp_path):
    path = tmp_path / "src" / "auth" / "x.py"
    path.parent.mkdir(parents=True)
    path.write_text("x", encoding="utf-8")

    result = classify_changes(tmp_path, ["src/auth/x.py"], ["src/auth"])

    assert result.verified_files == ["src/auth/x.py"]
    assert result.scope_violations == []
    assert result.unverifiable == []
    assert result.evidence_level == "verified"
    assert result.reasons == {}


def test_out_of_scope_changed_file_is_violation(tmp_path):
    path = tmp_path / "src" / "other.py"
    path.parent.mkdir()
    path.write_text("x", encoding="utf-8")

    result = classify_changes(tmp_path, ["src/other.py"], ["src/auth"])

    assert result.verified_files == []
    assert result.scope_violations == ["src/other.py"]
    assert result.reasons == {"src/other.py": "outside granted writable scope"}


def test_dotgit_changed_path_is_tainted_violation(tmp_path):
    result = classify_changes(tmp_path, [".git/config"], [".git"])

    assert result.verified_files == []
    assert result.scope_violations == [".git/config"]
    assert result.unverifiable == []
    assert result.reasons[".git/config"].startswith("tainted: ")
    assert "dotgit" in result.reasons[".git/config"]


def test_unverifiable_changed_path_is_best_effort(tmp_path):
    path = tmp_path / "src" / "auth" / "x.py"
    path.parent.mkdir(parents=True)
    path.write_text("x", encoding="utf-8")

    result = classify_changes(
        tmp_path,
        ["src/auth/x.py"],
        ["src/auth"],
        unverifiable={"src/auth/x.py"},
    )

    assert result.verified_files == []
    assert result.scope_violations == []
    assert result.unverifiable == ["src/auth/x.py"]
    assert result.evidence_level == "best_effort"
    assert result.reasons == {
        "src/auth/x.py": "index flag hides on-disk state (skip-worktree/assume-unchanged)"
    }


def test_git_unavailable_sets_unknown_evidence_level(tmp_path):
    path = tmp_path / "src" / "auth" / "x.py"
    path.parent.mkdir(parents=True)
    path.write_text("x", encoding="utf-8")

    result = classify_changes(
        tmp_path,
        ["src/auth/x.py"],
        ["src/auth"],
        git_available=False,
    )

    assert result.verified_files == ["src/auth/x.py"]
    assert result.evidence_level == "unknown"
