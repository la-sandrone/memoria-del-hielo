"""
测试: 源文件枚举（book_importer._safe_glob_src）与 CLI 计数的一致性。

本项目踩过「平行宇宙型屎山」：同一个可导入集合在两处各写一份判据，于是
"总说 13、实际跑了 12"。本测试把两条不变式钉住：

  ① 扩展名白名单只有一个真相源（SRC_EXTS）—— CLI 的计数与根目录枚举都 import 它；
  ② zip 的接受判据在**枚举里**（内容确认有 markdown.md），不推迟到导入时，
     否则被拒的 zip 会算进总数 N 却永远导不进来，N 与 X 就散了。

不碰库、不碰真 cache。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from src.cli.mhi_import_book import _count_path
from src.importers.book_importer import SRC_EXTS, _safe_glob_src


def _mineru_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("markdown.md", "# 书\n")
        z.writestr("middle_json.json", json.dumps({"schema": "docvortex.middle"}))
    return path


def _plain_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("notes.txt", "无关压缩包")
    return path


def test_src_exts_is_the_single_source(tmp_path: Path):
    assert SRC_EXTS == (".md", ".txt", ".zip")
    (tmp_path / "a.md").write_text("# x", encoding="utf-8")
    (tmp_path / "b.txt").write_text("x", encoding="utf-8")
    (tmp_path / "c.pdf").write_bytes(b"%PDF")
    got = {p.name for p in _safe_glob_src(tmp_path, False)}
    assert got == {"a.md", "b.txt"}


def test_mineru_zip_accepted_plain_zip_rejected(tmp_path: Path):
    good = _mineru_zip(tmp_path / "good.zip")
    bad = _plain_zip(tmp_path / "bad.zip")
    (tmp_path / ".hidden.zip").write_bytes(good.read_bytes())   # 隐藏文件不收
    got = _safe_glob_src(tmp_path, False)
    assert got == [good]
    assert bad not in got, "非 MinerU 的 zip 不进枚举（否则 N 会虚高）"


def test_recursive_matches_flat(tmp_path: Path):
    sub = tmp_path / "分类"
    sub.mkdir()
    _mineru_zip(sub / "deep.zip")
    (sub / "sibling.md").write_text("# y", encoding="utf-8")   # 不同主干，不会被顶掉
    assert _safe_glob_src(tmp_path, True) == _safe_glob_src(tmp_path, True)
    assert {p.name for p in _safe_glob_src(tmp_path, True)} == {"deep.zip", "sibling.md"}
    assert _safe_glob_src(tmp_path, False) == []


def test_count_path_agrees_with_glob(tmp_path: Path):
    """计数的尺 = 执行的尺。这是本测试存在的唯一理由。"""
    _mineru_zip(tmp_path / "one.zip")
    _mineru_zip(tmp_path / "two.zip")
    (tmp_path / "three.md").write_text("# z", encoding="utf-8")
    _plain_zip(tmp_path / "noise.zip")           # 两边都必须无视它

    n = _count_path(tmp_path, recursive=False, no_dir_tag=True, excluded=set())
    assert n == len(_safe_glob_src(tmp_path, False)) == 3


def test_count_path_root_files_use_same_whitelist(tmp_path: Path):
    """子目录分派模式下的「根散装文件」也复用 SRC_EXTS，不另写一份。"""
    sub = tmp_path / "化学"
    sub.mkdir()
    (sub / "in.md").write_text("# x", encoding="utf-8")
    _mineru_zip(tmp_path / "root.zip")
    (tmp_path / "root.md").write_text("# y", encoding="utf-8")
    (tmp_path / "ignored.pdf").write_bytes(b"%PDF")

    n = _count_path(tmp_path, recursive=False, no_dir_tag=False, excluded=set())
    assert n == 3, "根散装 2（md+zip）+ 子目录 1"


# ── 同主干撞车：zip 优先（用户裁定 2026-09-29） ──────────────


def test_same_stem_zip_supersedes_flat_md(tmp_path: Path):
    """同一目录 book.zip + book.md ⟹ 只导 zip。

    两者是同一次解析的两种序列化，不是两本书；幂等键 md5(source_path) 不同，
    都导会在库里留两条几乎重复的记录（真样本实测 67 块 + 66 块、正文逐字相同）。
    """
    z = _mineru_zip(tmp_path / "book.zip")
    m = tmp_path / "book.md"
    m.write_text("# 同一篇论文的 flat 序列化\n", encoding="utf-8")
    got = _safe_glob_src(tmp_path, False)
    assert got == [z], f"应只留 zip，实得 {[p.name for p in got]}"


def test_same_stem_non_mineru_zip_does_not_supersede(tmp_path: Path):
    """反例：同主干的 zip 若不是 MinerU 产出，就没有"同一次解析"这回事，
    不能拿它去顶掉一份正常的 md。

    判据必须挂在**被接受**的 zip 上（内容已确认），而不是"存在同名 .zip"。
    """
    m = tmp_path / "book.md"
    m.write_text("# 真的是一本书\n", encoding="utf-8")
    _plain_zip(tmp_path / "book.zip")
    assert {p.name for p in _safe_glob_src(tmp_path, False)} == {"book.md"}


def test_same_stem_rule_does_not_touch_txt(tmp_path: Path):
    """.txt 不是 MinerU 的产出形态 —— 规则只对 .md 生效。"""
    z = _mineru_zip(tmp_path / "book.zip")
    t = tmp_path / "book.txt"
    t.write_text("txt 形态的原始文本\n", encoding="utf-8")
    assert {p.name for p in _safe_glob_src(tmp_path, False)} == {"book.zip", "book.txt"}


def test_count_path_agrees_after_supersede(tmp_path: Path):
    """加规则之后，"计数的尺"与"执行的尺"仍必须一致。"""
    _mineru_zip(tmp_path / "book.zip")
    (tmp_path / "book.md").write_text("# dup\n", encoding="utf-8")
    (tmp_path / "other.md").write_text("# 另一本\n", encoding="utf-8")
    assert _count_path(tmp_path, recursive=False, no_dir_tag=True,
                       excluded=set()) == len(_safe_glob_src(tmp_path, False)) == 2
