"""PR1 —— #22: 超长输出不再落公共 OS temp,改落用户私有受限位置 + 有界清理。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cc_bridge.bridge import config
from cc_bridge.bridge.parser import (
    _MAX_SAVED_OUTPUTS,
    ResultParser,
    _prune_saved_outputs,
)


def _patch_app_dir(tmp_path, monkeypatch) -> Path:
    app_dir = tmp_path / "cc-bridge"
    monkeypatch.setattr(config, "stable_app_dir", lambda: app_dir)
    return app_dir


def test_output_goes_to_private_app_dir(tmp_path, monkeypatch):
    app_dir = _patch_app_dir(tmp_path, monkeypatch)
    path = ResultParser._save_full_output("secret token=abc123")
    assert path is not None
    p = Path(path)
    # 落在我们指定的私有目录,而不是 tempfile 的默认(共享)位置。
    assert p.parent == app_dir / "outputs"
    assert p.read_text(encoding="utf-8") == "secret token=abc123"


@pytest.mark.skipif(config.IS_WINDOWS, reason="POSIX 权限位")
def test_output_file_is_owner_only_on_posix(tmp_path, monkeypatch):
    _patch_app_dir(tmp_path, monkeypatch)
    path = ResultParser._save_full_output("x")
    assert path is not None
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_output_dir_is_bounded(tmp_path, monkeypatch):
    app_dir = _patch_app_dir(tmp_path, monkeypatch)
    for i in range(_MAX_SAVED_OUTPUTS + 5):
        assert ResultParser._save_full_output(f"out-{i}") is not None
    out_dir = app_dir / "outputs"
    files = [p for p in out_dir.iterdir() if p.name.startswith("cc_bridge_output_")]
    assert len(files) <= _MAX_SAVED_OUTPUTS


def test_prune_keeps_newest(tmp_path):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    for i in range(5):
        p = out_dir / f"cc_bridge_output_{i}.txt"
        p.write_text(str(i), encoding="utf-8")
        os.utime(p, (100 + i, 100 + i))  # 单调递增 mtime,排序确定
    _prune_saved_outputs(out_dir, keep=2)
    remaining = sorted(p.name for p in out_dir.iterdir())
    assert remaining == ["cc_bridge_output_3.txt", "cc_bridge_output_4.txt"]
