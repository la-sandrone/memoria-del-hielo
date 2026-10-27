"""测试: mcp_server 的文本可信度承载（_text_trust_note / read_book_chunk）。

本库不碰源 PDF，所以提示只能基于**产出文件本身可算得出**的信号。
数据源是 documents_book.errata_stats（一列 JSON），判据与词表都在
core.text_errata —— 这里只测「DB 里的 JSON 有没有被正确交给渲染器」。

关键纪律：
  · **空 `{}` != 「算了是 0」**：未计算必须说「未记录」，不许说「无误差」。
  · 坏 JSON 不静默吞：按未记录处理并记日志，不许把异常抛给 MCP 客户端。
"""

from __future__ import annotations

import json

import pytest

from src import mcp_server
from src.core.text_errata import CRITERIA_VERSION, count_errata
from src.mcp_server import _text_trust_note

CLEAN_ERRATA = json.dumps(count_errata("Византийский гуманизм известен всем."),
                          ensure_ascii=False)
DIRTY_ERRATA = json.dumps(
    {"criteria_version": CRITERIA_VERSION, "chars": 1000000, "words": 134069,
     "script_mix": 8706, "accented_latin": 29, "hyphen_break": 2071,
     "cjk_halluc": 0, "kangxi_radical": 0, "pua": 0},
    ensure_ascii=False,
)


def test_uncomputed_says_unrecorded_not_safe():
    """全空 `{}` = 迁移 007 之前的存量书 → 必须说「未记录」，不许说「无风险」。"""
    n = _text_trust_note({"errata_stats": "{}"})
    assert "未记录" in n or "无误差统计记录" in n
    assert "无误差" in n and "而非" in n


def test_missing_column_treated_as_unrecorded():
    """列都不在（旧库未迁 007）也不许炸。"""
    n = _text_trust_note({})
    assert "未记录" in n or "无误差统计记录" in n


def test_signal_reported_with_mode_and_counts():
    n = _text_trust_note({"mineru_parse_mode": "txt", "errata_stats": DIRTY_ERRATA})
    assert "8706" in n and "2071" in n
    assert "文本层读数" in n
    assert "形近字母串写" in n and "行尾连字符未合并" in n
    assert "另找资料旁证" in n


def test_clean_computed_book_gives_no_note():
    """已计算且全零 → 不给提示（docstring 里的普适提示仍在）。"""
    assert _text_trust_note({"mineru_parse_mode": "ocr",
                             "errata_stats": CLEAN_ERRATA}) == ""


def test_unknown_parse_mode_is_not_printed_as_english():
    dirty = dict(json.loads(DIRTY_ERRATA))
    dirty["script_mix"] = 1
    n = _text_trust_note({"mineru_parse_mode": "unknown",
                          "errata_stats": json.dumps(dirty, ensure_ascii=False)})
    assert "未知" in n
    assert "unknown" not in n


def test_accepts_dict_as_well_as_json_string():
    """驱动有时给回 dict（JSON 列被某些路径自动解析）——两条路都要通。"""
    d = json.loads(DIRTY_ERRATA)
    assert _text_trust_note({"errata_stats": d, "mineru_parse_mode": "ocr"}) \
        == _text_trust_note({"errata_stats": DIRTY_ERRATA,
                             "mineru_parse_mode": "ocr"})


def test_broken_json_degrades_loudly_not_silently(caplog):
    """坏 JSON 不许抛给客户端；要记一条 warning 再按未记录处理。"""
    with caplog.at_level("WARNING"):
        n = _text_trust_note({"errata_stats": "not json at all"})
    assert "未记录" in n or "无误差统计记录" in n
    assert any("errata_stats" in r.message for r in caplog.records), \
        "静默降级 = 把「没查」说成「没问题」，必须留痕"


def test_non_dict_json_is_ignored():
    n = _text_trust_note({"errata_stats": "[1, 2, 3]"})
    assert "未记录" in n or "无误差统计记录" in n


# ── read_book_chunk 的承载 ────────────────────────────


class _FakeDb:
    ROWS: list[dict] = []

    def execute_sql(self, sql: str, params: tuple = ()):  # noqa: ANN201
        self.LAST_SQL = sql
        return [dict(r) for r in self.ROWS]


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mcp_server, "get_db", lambda: _FakeDb())
    return _FakeDb


def _rows() -> list[dict]:
    common = {
        "heading_path": "§1", "book_title": "书", "book_path": "E:\\x.zip",
        "mineru_form": "full", "mineru_parse_mode": "ocr",
        "errata_stats": DIRTY_ERRATA,
    }
    return [
        dict(common, chunk_seq=1, char_start=0, content="正文"),
        dict(common, chunk_seq=2, char_start=10, content="正文2"),
    ]


def test_read_book_chunk_lifts_errata_out_of_rows(fake):
    fake.ROWS = _rows()
    out = json.loads(mcp_server.read_book_chunk("G", 1, 1))
    assert out["text_provenance"]["mineru_parse_mode"] == "ocr"
    assert "8706" in out["text_trust"]
    assert out["text_errata"]["counts"]["script_mix"] == 8706
    # 溯源字段不逐 chunk 重复
    for c in out["chunks"]:
        assert "errata_stats" not in c and "mineru_form" not in c
    # 原始 JSON 串不直接外泄（走 text_errata 这个自文档化视图）
    assert "errata_stats" not in out["text_provenance"]


def test_read_book_chunk_self_documents_only_fired_signals(fake):
    fake.ROWS = _rows()
    out = json.loads(mcp_server.read_book_chunk("G", 1, 1))
    gloss = out["text_errata"]["glossary"]
    assert set(gloss) == {"script_mix", "accented_latin", "hyphen_break"}
    assert "cjk_halluc" not in gloss, "没触发的键不贴释义（省 token）"
    for entry in gloss.values():
        assert entry["what"] and entry["harm"], "释义必须是完整句子"


def test_read_book_chunk_on_uncomputed_book(fake):
    """`errata_stats = {}` 时不给 text_errata，但 text_trust 要如实说未记录。"""
    rows = _rows()
    for r in rows:
        r["errata_stats"] = "{}"
    fake.ROWS = rows
    out = json.loads(mcp_server.read_book_chunk("G", 1, 1))
    assert "text_errata" not in out
    assert "未记录" in out["text_trust"] or "无误差统计记录" in out["text_trust"]
