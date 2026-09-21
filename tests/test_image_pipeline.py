"""测试: image_pipeline._find_in_media_dirs — pandoc/z-lib 图片兜底查找。"""

from __future__ import annotations

from pathlib import Path

from src.core.image_pipeline import _find_in_media_dirs


def test_zlib_layout_media_images(tmp_path: Path):
    """z-lib 布局：media/images/ 下按文件名命中（引用为失效的 Windows 绝对路径）。"""
    img_dir = tmp_path / "media" / "images"
    img_dir.mkdir(parents=True)
    (img_dir / "00537.jpg").write_bytes(b"j")

    raw = r"E:\某书\media\images\00537.jpg"
    assert _find_in_media_dirs(raw, tmp_path) == (img_dir / "00537.jpg").resolve()


def test_priority_media_over_images(tmp_path: Path):
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "a.jpg").write_bytes(b"m")
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "a.jpg").write_bytes(b"i")

    hit = _find_in_media_dirs(r"E:\x\a.jpg", tmp_path)
    assert hit is not None and hit.read_bytes() == b"m"


def test_direct_same_dir(tmp_path: Path):
    (tmp_path / "b.png").write_bytes(b"d")
    assert _find_in_media_dirs(r"E:\x\b.png", tmp_path) == (tmp_path / "b.png").resolve()


def test_missing_returns_none(tmp_path: Path):
    assert _find_in_media_dirs(r"E:\x\none.jpg", tmp_path) is None
    assert _find_in_media_dirs("", tmp_path) is None
    assert _find_in_media_dirs("a.jpg", None) is None
