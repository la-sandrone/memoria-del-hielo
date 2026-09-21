"""测试: book_importer._safe_glob_src — 坏目录跳过不崩批（md+txt）。"""

from __future__ import annotations

import os
from pathlib import Path

from src.importers.book_importer import _safe_glob_src


def test_safe_glob_recursive_collects(tmp_path: Path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.md").write_text("x", encoding="utf-8")
    (tmp_path / "a" / "z.txt").write_text("z", encoding="utf-8")
    (tmp_path / "b" / "deep").mkdir(parents=True)
    (tmp_path / "b" / "deep" / "y.md").write_text("y", encoding="utf-8")
    (tmp_path / "b" / "skip.pdf").write_text("p", encoding="utf-8")
    (tmp_path / ".hidden.md").write_text("h", encoding="utf-8")

    hits = _safe_glob_src(tmp_path, recursive=True)
    assert [h.name for h in hits] == ["x.md", "z.txt", "y.md"]  # 排序、滤 pdf/隐藏文件、收 txt


def test_safe_glob_skips_bad_dir(tmp_path: Path, monkeypatch):
    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "a.md").write_text("x", encoding="utf-8")
    bad = tmp_path / "bad"
    bad.mkdir()

    real_scandir = os.scandir

    def fake_scandir(path):
        if Path(path) == bad:
            raise OSError(5, "Input/output error", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)

    hits = _safe_glob_src(tmp_path, recursive=True)
    assert [h.name for h in hits] == ["a.md"]  # 坏目录被跳过，good 照常收集


def test_safe_glob_non_recursive_single_level(tmp_path: Path):
    (tmp_path / "top.md").write_text("t", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.md").write_text("n", encoding="utf-8")

    hits = _safe_glob_src(tmp_path, recursive=False)
    assert [h.name for h in hits] == ["top.md"]
