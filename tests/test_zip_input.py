"""
测试: core.zip_input —— MinerU `--format zip` 产出的物化器。

三条硬要求，各有一组断言钉住：
  ① 判据只看「归档里有没有 markdown.md」，不猜结构 —— 裸 middle_json 打成包也必须拒；
  ② zip-slip 与总量上限的处置是**整包拒绝**，不是"清洗后继续"；
  ③ 目录名按**内容** md5 定 ⟹ 同一份产物换位置/改名命中同一次解包，而重新解析
     过的新产物（内容变了）不会被旧内容串味。

全程 tmp_path，不碰真 cache、不碰库。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.core import zip_input as zi


def _zip(path: Path, members: list[tuple[str, bytes | str]]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, payload in members:
            z.writestr(name, payload)
    return path


def _mineru_zip(path: Path, *, with_model_output: bool = True,
                wrap: str = "") -> Path:
    """一份最小但形态完整的 MinerU zip。"""
    p = wrap
    members: list[tuple[str, bytes | str]] = [
        (f"{p}markdown.md", "# 标题\n\n正文\n\n![](images/page_1_image_1.jpg)\n"),
        (f"{p}middle_json.json", json.dumps(
            {"schema": "docvortex.middle", "schema_version": "2.0",
             "pages": [], "metadata": {"producer": {"version": "4.0.7"}}})),
        (f"{p}structured_content.json", json.dumps(
            {"schema": "docvortex.structured", "pages": []})),
        (f"{p}images/page_1_image_1.jpg", b"\xff\xd8\xff\xe0JPEG"),
    ]
    if with_model_output:
        members.append((f"{p}model_output.json", "x" * 4096))
    return _zip(path, members)


# ── 判据：只看有没有正文 ────────────────────────────────


def test_real_mineru_zip_recognized(tmp_path: Path):
    z = _mineru_zip(tmp_path / "book.zip")
    assert zi.is_mineru_zip(z) is True


def test_bare_middle_json_zip_rejected(tmp_path: Path):
    """`--format middle_json` 的产出打成包 —— 必须拒。

    官方 reference/output_files：standalone structural JSON
    "is not a complete result package"（没有正文、没有图片文件）。
    """
    z = _zip(tmp_path / "mjson.zip", [
        ("ermakov2017.json",
         json.dumps({"schema": "docvortex.middle", "schema_version": "2.0"})),
    ])
    assert zi.is_mineru_zip(z) is False


def test_plain_zip_rejected(tmp_path: Path):
    z = _zip(tmp_path / "plain.zip", [("readme.txt", "hello")])
    assert zi.is_mineru_zip(z) is False


def test_broken_zip_rejected(tmp_path: Path):
    p = tmp_path / "broken.zip"
    p.write_bytes(b"not a zip at all")
    assert zi.is_mineru_zip(p) is False


def test_root_prefix_returns_none_when_absent():
    """'' 与 None 必须分开：'' 是"正文在归档根"这一合法结论，None 才是"没有正文"。

    首版把落空返回值也写成 ''，于是 is_mineru_zip() 对任何 zip 都返回 True ——
    这条断言就是为那个 bug 立的墓碑。
    """
    assert zi._root_prefix({"markdown.md"}) == ""
    assert zi._root_prefix({"a/markdown.md"}) == "a/"
    assert zi._root_prefix({"a.txt"}) is None


# ── 物化 ────────────────────────────────────────────────


def test_materialize_layout_and_skips(tmp_path: Path):
    z = _mineru_zip(tmp_path / "book.zip")
    m = zi.materialize(z, root=tmp_path / "cache")

    assert m.reused is False
    assert m.md_path.is_file()
    assert m.md_path.name == zi.REQUIRED_MEMBER
    assert (m.work_dir / "images" / "page_1_image_1.jpg").is_file()
    assert (m.work_dir / "middle_json.json").is_file()
    assert (m.work_dir / "structured_content.json").is_file()
    # model_output.json 是唯一按设计跳过的成员（实测 8.4MB，本项目不消费）
    assert not (m.work_dir / "model_output.json").exists()
    assert m.skipped == ("model_output.json",)
    assert m.n_files == 4


def test_materialize_is_idempotent_by_content(tmp_path: Path):
    root = tmp_path / "cache"
    a = _mineru_zip(tmp_path / "a.zip")
    m1 = zi.materialize(a, root=root)
    assert m1.reused is False

    # 同一份内容换个名字 ⟹ 命中同一次解包（目录名是内容 md5，不是路径）
    b = tmp_path / "renamed.zip"
    b.write_bytes(a.read_bytes())
    m2 = zi.materialize(b, root=root)
    assert m2.reused is True
    assert m2.work_dir == m1.work_dir
    assert m2.n_files == 0


def test_materialize_distinguishes_changed_content(tmp_path: Path):
    """内容变一个字节就是另一份产物 —— 不能被旧解包目录串味。"""
    root = tmp_path / "cache"
    a = _mineru_zip(tmp_path / "a.zip")
    m1 = zi.materialize(a, root=root)

    b = _zip(tmp_path / "b.zip", [
        ("markdown.md", "# 改过的正文\n"),
        ("middle_json.json", json.dumps({"schema": "docvortex.middle"})),
    ])
    m2 = zi.materialize(b, root=root)
    assert m2.work_dir != m1.work_dir
    assert m2.md_path.read_text(encoding="utf-8") == "# 改过的正文\n"


def test_materialize_tolerates_one_level_wrapper(tmp_path: Path):
    z = _mineru_zip(tmp_path / "wrapped.zip", wrap="book/")
    m = zi.materialize(z, root=tmp_path / "cache")
    assert m.md_path.is_file()
    assert (m.work_dir / "images" / "page_1_image_1.jpg").is_file()


def test_materialize_writes_provenance_note(tmp_path: Path):
    """解包目录名是哈希，人看不出它来自哪个 zip ⟹ 留一张溯源小票。"""
    z = _mineru_zip(tmp_path / "book.zip")
    m = zi.materialize(z, root=tmp_path / "cache")
    note = json.loads((m.work_dir / "_zip_source.json").read_text(encoding="utf-8"))
    assert note["zip_md5"] == m.work_dir.name
    assert Path(note["zip_path"]).name == "book.zip"
    assert note["members_skipped"] == ["model_output.json"]


# ── 拒绝路径：整包拒绝，不清洗后继续 ────────────────────


def test_zip_slip_rejected_whole_archive(tmp_path: Path):
    z = _zip(tmp_path / "slip.zip", [
        ("markdown.md", "# x"),
        ("../../evil.txt", "boom"),
    ])
    with pytest.raises(zi.ZipInputError, match="路径穿越"):
        zi.materialize(z, root=tmp_path / "cache")
    # 半成品目录不得留下
    assert not any((tmp_path / "cache").glob("*.tmp"))


def test_absolute_member_never_escapes_root(tmp_path: Path):
    """前导斜杠的成员既不能逃出解包根，也不在白名单里 ⟹ 根本不落盘。"""
    z1 = _zip(tmp_path / "abs.zip", [("markdown.md", "# x"), ("/etc/passwd", "x")])
    root = tmp_path / "cache"
    m = zi.materialize(z1, root=root)
    assert m.md_path.is_file()
    assert not (tmp_path / "etc").exists(), "前导斜杠成员绝不能写到解包根之外"
    assert list(root.rglob("passwd")) == []


def test_ntfs_ads_member_rejected(tmp_path: Path):
    z2 = _zip(tmp_path / "ads.zip", [("markdown.md", "# x"), ("a:b.txt", "x")])
    with pytest.raises(zi.ZipInputError, match="NTFS 数据流"):
        zi.materialize(z2, root=tmp_path / "cache2")


def test_missing_markdown_member_rejected(tmp_path: Path):
    z = _zip(tmp_path / "noref.zip", [("middle_json.json", "{}")])
    with pytest.raises(zi.ZipInputError, match=zi.REQUIRED_MEMBER):
        zi.materialize(z, root=tmp_path / "cache")


def test_oversize_archive_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(zi, "MAX_TOTAL_BYTES", 16)
    z = _mineru_zip(tmp_path / "big.zip")
    with pytest.raises(zi.ZipInputError, match="zip 炸弹"):
        zi.materialize(z, root=tmp_path / "cache")


def test_missing_file_rejected(tmp_path: Path):
    with pytest.raises(zi.ZipInputError, match="不存在"):
        zi.materialize(tmp_path / "nope.zip", root=tmp_path / "cache")


def test_stop_check_aborts_without_leaving_tmp(tmp_path: Path):
    z = _mineru_zip(tmp_path / "book.zip")
    with pytest.raises(KeyboardInterrupt):
        zi.materialize(z, root=tmp_path / "cache", stop_check=lambda: True)
    cache = tmp_path / "cache"
    assert not any(cache.glob("*.tmp")), "中断必须清干净半成品目录"


def test_describe_lines(tmp_path: Path):
    z = _mineru_zip(tmp_path / "book.zip")
    m = zi.materialize(z, root=tmp_path / "cache")
    assert "跳过 model_output.json" in zi.describe(m)
    assert "复用" in zi.describe(zi.materialize(z, root=tmp_path / "cache"))
