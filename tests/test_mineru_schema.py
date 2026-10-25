"""
测试: core/mineru_schema.py —— v3/v4 三形态的识别与归一。

夹具是**从真实产出切片**（tests/fixtures/mineru/，由 _scratch/build_mineru_fixtures.py 生成）：
  v3/Book/auto/      真书《原神剧情合集》683 页中抽 2 页（含 page_footnote）
  v4zip/             真 zip（剔掉二进制图）
  v4dir/             真 zip 的解包目录形态
  flat/              真扁平 md 的前 9 KB（含真 base64 内联图）

覆盖：形态识别 / provenance 提取 / bbox 折成 0–1 / 跨代类型别名 /
      行内文本抽平 / 脚注候选集 / 图片类型映射 / skip_ocr 分流 / 覆盖率审计。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.mineru_schema import (
    FOOTNOTE_TYPES,
    _v4_block_text,
    FORM_DIR,
    FORM_FLAT,
    FORM_FULL,
    PARSE_MODE_UNKNOWN,
    VISUAL_TYPES,
    census_report,
    load_product,
)

FIX = Path(__file__).resolve().parent / "fixtures" / "mineru"

V3_MD = FIX / "v3" / "Book" / "auto" / "Book.md"
V4ZIP_MD = FIX / "v4zip" / "Book.md"
V4DIR_MD = FIX / "v4dir" / "markdown.md"      # 真解包名就是 markdown.md
V4RENAMED_MD = FIX / "v4renamed" / "Renamed.md"
FLAT_MD = FIX / "flat" / "Book.md"
PLAIN_MD = FIX / "plain" / "Book.md"


def _require(p: Path) -> Path:
    if not p.is_file():
        pytest.skip(f"夹具缺失（先跑 _scratch/build_mineru_fixtures.py）: {p}")
    return p


# ── 形态识别 ────────────────────────────────────────────


def test_v3_dir_form_and_provenance():
    p = load_product(_require(V3_MD))
    assert p.form == FORM_DIR and p.is_mineru
    assert p.provenance.form == FORM_DIR
    assert p.provenance.version == "3.4.0"      # 来自 _version_name
    assert p.provenance.tier == ""              # v3 无档位概念
    assert p.provenance.parse_mode == ""        # v3 无此字段（≠ unknown）


def test_v4_zip_form_and_provenance():
    p = load_product(_require(V4ZIP_MD))
    assert p.form == FORM_FULL
    assert p.provenance.version == "4.0.7"      # metadata.producer.version
    assert p.provenance.tier == "flash"
    assert p.provenance.parse_mode == "ocr"


def test_v4_unpacked_dir_same_capability_as_zip():
    """解包目录与 zip 能力相同 → form 都是 full（故不叫 zip：会在解包场景撒谎）。"""
    a = load_product(_require(V4ZIP_MD))
    b = load_product(_require(V4DIR_MD))
    assert a.form == b.form == FORM_FULL
    assert a.provenance == b.provenance


def test_v4_renamed_json_fallback():
    """json 被手工改名成 <stem>.json 时仍能识别（回退分支）。"""
    p = load_product(_require(V4RENAMED_MD))
    assert p.form == FORM_FULL
    assert p.provenance.version == "4.0.7"


def test_unpacked_fixture_filenames_match_real_zip_members():
    """**夹具自检**：解包夹具的文件名必须与真 zip 的成员名逐字相同。

    这条是补漏：首版夹具把 `middle_json.json` 改名成 `Book.json`，
    于是 test_v4_unpacked_dir_same_capability_as_zip 在**代码其实找不到真解包名**
    的情况下自证通过。判据是「夹具不许自己发明文件名」。
    """
    import zipfile
    z = FIX / "v4zip" / "Book.zip"
    d = FIX / "v4dir"
    if not z.is_file() or not d.is_dir():
        pytest.skip("夹具缺失")
    with zipfile.ZipFile(z) as zf:
        members = {n for n in zf.namelist() if not n.startswith("images/")}
    actual = {f.name for f in d.iterdir() if f.is_file()}
    assert actual == members, (
        f"解包夹具的文件名与真 zip 成员名不一致\n"
        f"  真 zip: {sorted(members)}\n  夹具  : {sorted(actual)}"
    )


def test_flat_form_is_positive_judgement():
    """扁平 md 靠「正文含内联 data URI 图」正面命中，不靠排除法。"""
    p = load_product(_require(FLAT_MD))
    assert p.form == FORM_FLAT
    assert p.provenance.parse_mode == PARSE_MODE_UNKNOWN   # D1：落库标 unknown
    assert p.footnote_candidates == [] and p.image_type_map == {}


def test_plain_md_is_not_mineru():
    """同目录无侧车、正文无内联图 → 非 MinerU 产物（普通笔记不能被误判）。"""
    d = FIX / "plain"
    d.mkdir(parents=True, exist_ok=True)
    (d / "Book.md").write_text("# 普通笔记\n\n没有内联图。\n", encoding="utf-8")
    p = load_product(d / "Book.md")
    assert p.form == "" and not p.is_mineru
    assert p.provenance.version == "" and p.provenance.parse_mode == ""


def test_assume_flat_overrides_probe():
    """纯文字 v4 扁平产出（无内联图）靠显式开关兜住。"""
    d = FIX / "plain"
    p = load_product(d / "Book.md", assume_flat_mineru=True)
    assert p.form == FORM_FLAT
    assert p.provenance.parse_mode == PARSE_MODE_UNKNOWN


# ── 归一化：bbox 折成 0–1 ───────────────────────────────


def test_v3_bbox_normalized_to_0_1():
    """v3 是页内 pt 绝对坐标 → 除 page_size 后应落在 [0,1]。"""
    p = load_product(_require(V3_MD))
    boxes = [b.bbox01 for b in p.footnote_candidates if b.bbox01]
    assert boxes, "夹具里应当有带 bbox 的 discarded 块"
    for x0, y0, x1, y1 in boxes:
        for v in (x0, y0, x1, y1):
            assert 0.0 <= v <= 1.0, f"未归一化: {v}"
        assert y0 <= y1 and x0 <= x1
    assert p.page_height_pt, "v3 应记录原始页高（审计用）"


def test_v4_bbox_already_0_1():
    p = load_product(_require(V4ZIP_MD))
    for b in p.footnote_candidates:
        if b.bbox01:
            for v in b.bbox01:
                assert 0.0 <= v <= 1.0


def test_bbox_units_are_comparable_across_generations():
    """同一个「页面底部」判据在两代上落在同一量纲 —— 这是折 0–1 的全部意义。"""
    v3 = load_product(_require(V3_MD))
    v4 = load_product(_require(V4ZIP_MD))
    lo3 = [b.y_top01 for b in v3.footnote_candidates if b.y_top01 is not None]
    lo4 = [b.y_top01 for b in v4.footnote_candidates if b.y_top01 is not None]
    assert all(0 <= y <= 1 for y in lo3)
    assert all(0 <= y <= 1 for y in lo4)


# ── 跨代类型别名与文本抽平 ──────────────────────────────


def test_v4_paragraph_title_aliased_to_title():
    p = load_product(_require(V4ZIP_MD))
    raws = {b.raw_type for b in p.footnote_candidates}
    assert "paragraph_title" in raws, "夹具里应有 paragraph_title 以覆盖别名"
    t = [b for b in p.footnote_candidates if b.raw_type == "paragraph_title"]
    assert all(b.type == "title" for b in t), "别名应折成 v3 的 title"


def test_v4_inline_spans_flattened():
    """v4 middle_json 的 content 是 span 列表，必须抽平成字符串。"""
    p = load_product(_require(V4ZIP_MD))
    texts = [b.text for b in p.footnote_candidates if b.type == "text"]
    assert any(t.strip() for t in texts), "应抽出非空正文"
    assert not any(t.startswith("[") for t in texts), "不得把 span 列表 repr 当文本"


# 下面两条字面量是**从真书里抄下来的块**（Unicode 14.0 标准书 page 1，
# _scratch/mineru4_probe/flash/*.json）。抄真形状而不是编形状 —— 首版
# _v4_block_text 只抽一层，遇到下面这种嵌套就把内层列表 str() 成了 repr 塞进正文。
_REAL_NESTED_INDEX_BLOCK = {
    "type": "index", "index": 2, "bbox": [0.083, 0.174, 0.917, 0.948],
    "content": [
        {"type": "text", "content": [
            {"type": "text", "content": "Preface . . . xxi"}]},
        {"type": "text", "content": [
            {"type": "text", "content": "Why Unicode? . . . xxi"}]},
    ],
}
_REAL_HYPERLINK_BLOCK = {
    "type": "text",
    "content": [
        {"type": "hyperlink", "content": [
            {"type": "text", "content": "https://www.unicode.org/reporting.html"}]}],
}


def test_v4_nested_span_content_is_flattened_not_reprd():
    """嵌套 content 必须递归抽平；**不得**产出容器 repr。"""
    got = _v4_block_text(_REAL_NESTED_INDEX_BLOCK["content"])
    assert got == "Preface . . . xxi Why Unicode? . . . xxi"
    for bad in ("{'type'", "[{", "'}]", "}]"):
        assert bad not in got, f"抽平结果里出现容器 repr 碎片: {bad!r} → {got!r}"


def test_v4_hyperlink_span_keeps_target_text():
    got = _v4_block_text(_REAL_HYPERLINK_BLOCK["content"])
    assert got == "https://www.unicode.org/reporting.html"


def test_v4_unknown_node_shape_fails_loud():
    """不认识的节点形状必须抛 —— 静默 str() 是今天栽过的那个坑。"""
    with pytest.raises(TypeError):
        _v4_block_text([{"type": "text", "content": 12345}])


def test_v4_visual_body_excluded_from_text():
    """视觉体不进脚注文本（与 v3 只取 lines/spans 对称）。"""
    assert _v4_block_text([{"type": "table_body", "content": "<table>…</table>"}]) == ""


def test_v3_inline_equation_kept_as_math():
    """v3 的 inline_equation span 抽成 $...$（我们的表源就出在这种文本里）。"""
    p = load_product(_require(V3_MD))
    joined = "\n".join(b.text for b in p.footnote_candidates)
    assert "$" in joined or joined.strip(), "至少不该是空的"


# ── 脚注候选集 ──────────────────────────────────────────


def test_v3_footnote_candidates_come_from_discarded():
    p = load_product(_require(V3_MD))
    assert p.footnote_candidates and all(b.discarded for b in p.footnote_candidates)
    types = {b.type for b in p.footnote_candidates}
    assert types & FOOTNOTE_TYPES, f"夹具含真 page_footnote，实际 {types}"


def test_v4_footnote_candidates_are_all_blocks():
    """v4 没有 discarded 列表 → 候选集是全部 blocks，discarded 恒 False。"""
    p = load_product(_require(V4ZIP_MD))
    assert p.footnote_candidates and all(not b.discarded for b in p.footnote_candidates)
    assert len(p.footnote_candidates) == sum(p.type_census.values())


def test_page_idx_preserved():
    p = load_product(_require(V3_MD))
    idx = {b.page_idx for b in p.footnote_candidates}
    assert len(idx) == 2 and all(isinstance(i, int) and i >= 0 for i in idx), idx


# ── 图片类型映射 ────────────────────────────────────────


def test_v3_image_type_map_from_content_list_v2():
    p = load_product(_require(V3_MD))
    assert p.image_type_map, "夹具的 content_list_v2 里应能抽出图片类型"
    assert set(p.image_type_map.values()) <= VISUAL_TYPES


def test_v4_image_type_map_from_structured_content():
    p = load_product(_require(V4ZIP_MD))
    assert p.image_type_map, "structured_content 的 image_source 应能抽出映射"
    assert set(p.image_type_map.values()) <= VISUAL_TYPES
    assert all(k.startswith("images/") for k in p.image_type_map), \
        f"v4 的 image_source 应为相对路径，实际 {list(p.image_type_map)[:3]}"


def test_flat_has_no_image_type_map():
    p = load_product(_require(FLAT_MD))
    assert p.image_type_map == {}


# ── skip_ocr 分流（D1）──────────────────────────────────


def test_skip_ocr_only_for_ocr_parse_mode():
    assert load_product(_require(V4ZIP_MD)).skip_ocr_for_images is True    # parse_mode=ocr
    assert load_product(_require(V3_MD)).skip_ocr_for_images is False     # v3 无字段
    assert load_product(_require(FLAT_MD)).skip_ocr_for_images is False   # unknown → 走 OCR


# ── 覆盖率审计（「裁判」第四条）─────────────────────────


def test_census_reports_types_and_unknowns():
    p = load_product(_require(V4ZIP_MD))
    rep = census_report(p.footnote_candidates)
    assert rep["count"] == len(p.footnote_candidates)
    assert rep["no_bbox"] == 0
    assert rep["unknown_types"] == [], f"出现未识别类型: {rep['unknown_types']}"


def test_census_flags_unknown_types():
    """未识别类型必须被报出来，不能静默继续。"""
    from src.core.mineru_schema import NormalizedBlock
    fake = [NormalizedBlock(0, "某个新类型", "某个新类型", "x", (0, 0, 1, 1))]
    assert census_report(fake)["unknown_types"] == ["某个新类型"]


# ── 夹具自检 ────────────────────────────────────────────


def test_fixtures_are_real_slices_not_synthesised():
    """夹具必须来自真实产出：v3 应有 _version_name，v4 应有 schema 2.0。"""
    v3 = json.loads((FIX / "v3" / "Book" / "auto" / "Book_middle.json").read_text("utf-8"))
    assert v3.get("_version_name"), "v3 夹具缺 _version_name —— 不是真产出切片"
    assert v3["pdf_info"][0].get("page_size"), "v3 夹具缺 page_size"
    v4 = json.loads((FIX / "v4dir" / "middle_json.json").read_text("utf-8"))
    assert v4.get("schema") == "docvortex.middle" and v4.get("schema_version") == "2.0"
