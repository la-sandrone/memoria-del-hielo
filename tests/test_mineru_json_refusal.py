"""
测试: 拒绝把 MinerU 的裸结构 JSON 当书导入。

依据（官方 reference/output_files）：PDF `ParseResult.to_dict()/to_json()`
"still omit block image_base64, so standalone structural JSON is not a
 complete result package" —— `--format middle_json` 的产出就是它：只有结构，
没有 markdown 正文、也没有图片文件，做不成切片语料。

两条入口各一组断言：
  · 显式点名一个 .json（refuse_explicit_mineru_json）
  · 目录里零可导入文件但躺着 middle_json（_refuse_if_only_mineru_json）

另有一条**反例**：v3 的 `<stem>_middle.json` 是合法侧车，绝不能按名字误伤
—— 所以判据必须读内容认 `schema` 键。不碰库，全是纯函数。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.importers.book_importer import (
    MiddleJsonInputError,
    describe_mineru_json,
    refuse_explicit_mineru_json,
    _refuse_if_only_mineru_json,
)

MIDDLE = {"schema": "docvortex.middle", "schema_version": "2.0",
          "metadata": {"producer": {"version": "4.0.7"}}, "pages": []}
MODEL = {"schema": "docvortex.model", "schema_version": "2.0", "pages": []}


def _write(p: Path, obj) -> Path:
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


# ── 内容判据 ────────────────────────────────────────────


def test_describe_recognizes_both_schemas(tmp_path: Path):
    assert describe_mineru_json(_write(tmp_path / "a.json", MIDDLE)) == "docvortex.middle"
    assert describe_mineru_json(_write(tmp_path / "b.json", MODEL)) == "docvortex.model"


def test_describe_rejects_other_json(tmp_path: Path):
    """任意 json（配置、结构化笔记…）不能被误判成 MinerU 产出。"""
    assert describe_mineru_json(_write(tmp_path / "c.json", {"hello": 1})) is None
    assert describe_mineru_json(_write(tmp_path / "d.json", [1, 2, 3])) is None


def test_describe_survives_non_json(tmp_path: Path):
    p = tmp_path / "broken.json"
    p.write_text("{ not json", encoding="utf-8")
    assert describe_mineru_json(p) is None


def test_describe_skips_oversize(tmp_path: Path):
    """病态输入不值得读进内存。"""
    p = _write(tmp_path / "big.json", MIDDLE)
    assert describe_mineru_json(p, max_bytes=4) is None


# ── 显式点名 ────────────────────────────────────────────


def test_explicit_middle_json_refused_with_hint(tmp_path: Path):
    p = _write(tmp_path / "ermakov2017.json", MIDDLE)
    with pytest.raises(MiddleJsonInputError) as ei:
        refuse_explicit_mineru_json(p)
    msg = str(ei.value)
    assert "裸结构 JSON" in msg
    assert "--format zip" in msg, "拒绝必须给出改法，不能只说不行"


def test_explicit_non_mineru_json_passes_through(tmp_path: Path):
    """别的 .json 不在这里拦 —— 该由"不支持的格式"那条路去报。"""
    refuse_explicit_mineru_json(_write(tmp_path / "conf.json", {"a": 1}))


def test_explicit_non_json_untouched(tmp_path: Path):
    p = tmp_path / "book.md"
    p.write_text("# x", encoding="utf-8")
    refuse_explicit_mineru_json(p)          # 不抛


def test_missing_path_untouched(tmp_path: Path):
    refuse_explicit_mineru_json(tmp_path / "nope.json")   # 不抛


# ── 目录场景 ────────────────────────────────────────────


def test_dir_with_only_middle_json_refused(tmp_path: Path):
    """目录里零可导入文件 + 躺着裸结构 json ⟹ 指名道姓地说清楚。

    旧行为是静默返回 0 本（"什么都没发生"），用户会以为路径写错了。
    """
    _write(tmp_path / "ermakov2017.json", MIDDLE)
    with pytest.raises(MiddleJsonInputError) as ei:
        _refuse_if_only_mineru_json(tmp_path, recursive=False)
    assert "ermakov2017.json" in str(ei.value)


def test_dir_with_middle_json_nested_refused_when_recursive(tmp_path: Path):
    sub = tmp_path / "ermakov2017-json"
    sub.mkdir()
    _write(sub / "ermakov2017.json", MIDDLE)
    with pytest.raises(MiddleJsonInputError):
        _refuse_if_only_mineru_json(tmp_path, recursive=True)
    _refuse_if_only_mineru_json(tmp_path, recursive=False)   # 非递归看不见，不抛


def test_empty_dir_is_not_an_error(tmp_path: Path):
    """空目录照旧安静返回 0 本 —— 不能把"没有 json"也变成报错。"""
    _refuse_if_only_mineru_json(tmp_path, recursive=True)


def test_v3_sidecar_is_never_flagged(tmp_path: Path):
    """反例：v3 的 `<stem>_middle.json` 是**合法侧车**（_load_v3 要读它）。

    若判据按文件名拦，v3 产出的目录扫描会被整片误伤。故必须读内容认 schema。
    """
    sidecar = tmp_path / "book_middle.json"
    sidecar.write_text(
        json.dumps({"_version_name": "3.1.6", "pdf_info": []}), encoding="utf-8"
    )
    assert describe_mineru_json(sidecar) is None
    _refuse_if_only_mineru_json(tmp_path, recursive=False)   # 不抛
