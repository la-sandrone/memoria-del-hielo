"""
测试: 脚注判据 v2（mineru_postprocess.is_footnote_v2 / extract_footnotes）。

v1 的两头错都是**实测**出来的（全库 64,256 条）：
  · 不筛类型 ⟹ `footer`（页脚冒充）15,367 条 = 24%
  · 又叠加 `y >= 0.70` ⟹ 误杀真 `page_footnote` 80/588 = 13.6%（y/h min 0.415）
这两条在这里各配一条**正对照**断言 —— 判据改回去就必须红。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core import mineru_postprocess as mp
from src.core.mineru_postprocess import (
    JUDGE_VERSION,
    MineruPostprocessor,
    is_footnote_v2,
    is_noise_text,
)
from src.core.mineru_schema import FOOTNOTE_TYPES, NON_FOOTNOTE_TYPES, NormalizedBlock

V3_FIX = Path(__file__).resolve().parent / "fixtures" / "mineru" / "v3" / "Book" / "auto" / "Book.md"


# ── 纯判据 ─────────────────────────────────────────────


def test_type_first_accepts_footnotes_at_any_y():
    """类型明确就收，不问 y —— v1 的 y 门槛正是误杀源。"""
    for t in FOOTNOTE_TYPES:
        assert is_footnote_v2(t, 0.415) is True, f"{t} 在 y=0.415 被误杀（v1 的病）"
        assert is_footnote_v2(t, 0.95) is True
        assert is_footnote_v2(t, None) is True, f"{t} 无 bbox 也该收"


def test_type_first_rejects_layout_elements():
    """页眉/页脚/页码明确拒，即使它们落在页面底部。"""
    for t in NON_FOOTNOTE_TYPES:
        assert is_footnote_v2(t, 0.95) is False, f"{t} 被当成脚注（v1 的病，24% 来源）"
        assert is_footnote_v2(t, 0.99) is False


def test_others_fall_back_to_bottom_region():
    assert is_footnote_v2("text", 0.75) is True
    assert is_footnote_v2("text", 0.69) is False
    assert is_footnote_v2("aside_text", None) is False


# ── v1 两头错的正对照（改回去必红）──────────────────────


def test_positive_control_v1_error_1_footer_impersonation():
    """v1：不筛类型 ⟹ footer 被当脚注收（15,367 条 = 24%）。"""
    assert is_footnote_v2("footer", 0.95) is False


def test_positive_control_v1_error_2_real_footnote_killed():
    """v1：y 门槛误杀真 page_footnote（80/588 = 13.6%，min y/h = 0.415）。"""
    assert is_footnote_v2("page_footnote", 0.415) is True


# ── 噪音过滤只作用于回退分支 ────────────────────────────


def test_noise_text_rules():
    assert is_noise_text("  12 ") is True
    assert is_noise_text("· 7 ·") is True
    assert is_noise_text("(178)") is True
    assert is_noise_text("(127, 203, 321)") is True
    assert is_noise_text("ab") is True              # <3 字
    assert is_noise_text("ibid.") is False
    assert is_noise_text("See §23.4 Variation Selectors.") is False


# ── detect 走适配器 ─────────────────────────────────────


def test_detect_via_adapter_v3_fixture():
    if not V3_FIX.is_file():
        pytest.skip("夹具缺失（先跑 _scratch/build_mineru_fixtures.py）")
    assert MineruPostprocessor.detect(V3_FIX) is True
    assert MineruPostprocessor.load(V3_FIX).form == "dir"


def test_detect_false_for_plain_md(tmp_path: Path):
    f = tmp_path / "note.md"
    f.write_text("# 普通笔记\n", encoding="utf-8")
    assert MineruPostprocessor.detect(f) is False
    assert MineruPostprocessor.load(f).form == ""


# ── extract_footnotes：落 judge_version + 类型分流 ─────


class _FakeDb:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def execute_sql(self, sql: str, params: tuple = ()):  # noqa: ANN201
        if "INSERT IGNORE INTO page_footnotes" in " ".join(sql.split()):
            self.rows.append(params)
        return []


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch):
    fake_db = _FakeDb()
    monkeypatch.setattr(mp, "get_db", lambda: fake_db)
    return fake_db


def _prod(blocks: list[NormalizedBlock]):
    from src.core.mineru_schema import MineruProduct, Provenance
    return MineruProduct(provenance=Provenance(form="full"), form="full",
                         footnote_candidates=blocks)


def test_extract_footnotes_writes_v2_and_filters(fake, monkeypatch):
    blocks = [
        NormalizedBlock(0, "page_footnote", "page_footnote", "真脚注", (0, 0.9, 1, 0.95)),
        NormalizedBlock(0, "footer", "footer", "页脚冒充", (0, 0.95, 1, 0.99)),
        NormalizedBlock(0, "header", "header", "页眉", (0, 0.01, 1, 0.05)),
        NormalizedBlock(0, "page_number", "page_number", "7", (0.5, 0.97, 0.6, 0.99)),
        NormalizedBlock(0, "text", "text", "底部正文", (0, 0.8, 1, 0.85)),
        NormalizedBlock(0, "text", "text", "中间正文", (0, 0.4, 1, 0.45)),
    ]
    monkeypatch.setattr(mp, "load_product", lambda p: _prod(blocks))
    n = MineruPostprocessor().extract_footnotes(Path("x.md"), "GUID-1")
    assert n == 2, f"应只收 page_footnote 与底部 text，实际 {n}"
    texts = [r[2] for r in fake.rows]
    assert texts == ["真脚注", "底部正文"], texts
    assert all(r[5] == JUDGE_VERSION for r in fake.rows), "必须写 judge_version"
    assert JUDGE_VERSION == "v2"


def test_extract_footnotes_returns_zero_for_non_mineru(fake, monkeypatch):
    monkeypatch.setattr(mp, "load_product", lambda p: _prod([]))
    assert MineruPostprocessor().extract_footnotes(Path("x.md"), "G") == 0
    assert fake.rows == []


def test_v2_does_not_lose_v1_coverage_on_typed_blocks(fake, monkeypatch):
    """v2 必须比 v1 **只多不少**：v1 能收的（page_footnote 且 y 高）v2 也要收。"""
    b = [NormalizedBlock(0, "page_footnote", "page_footnote", "高 y 脚注",
                         (0, 0.9, 1, 0.95))]
    monkeypatch.setattr(mp, "load_product", lambda p: _prod(b))
    assert MineruPostprocessor().extract_footnotes(Path("x.md"), "G") == 1
