"""测试: mcp_server._text_trust_note + read_book_chunk 的溯源承载。

本库不碰源 PDF，所以提示只能基于**产出文件本身可算得出**的信号
（康熙部首码点 / PUA）+ 落库的 provenance。
关键纪律：**计数 0 ≠ 无风险**（旧书一律 0，那是「未记录」）。
"""

from __future__ import annotations

import json

import pytest

from src import mcp_server
from src.mcp_server import _text_trust_note


def test_no_provenance_says_unrecorded_not_safe():
    """全空 = 早期导入/非 MinerU → 必须说「未记录」，不许说「无风险」。"""
    n = _text_trust_note({})
    assert "未记录" in n or "无 MinerU 溯源" in n
    assert "无风险" in n or "而非「无风险」" in n


def test_signal_reported_with_mode():
    n = _text_trust_note({
        "mineru_form": "dir", "mineru_parse_mode": "txt",
        "kangxi_radical_count": 11018, "pua_count": 0,
    })
    assert "11018" in n and "文本层读数" in n
    assert "康熙部首" in n


def test_zero_counts_with_clean_provenance_gives_no_note():
    """有溯源且观测到 0 → 不给提示（docstring 里的普适提示仍在）。"""
    assert _text_trust_note({
        "mineru_form": "full", "mineru_parse_mode": "ocr",
        "kangxi_radical_count": 0, "pua_count": 0,
    }) == ""


def test_unknown_parse_mode_is_not_called_txt():
    n = _text_trust_note({"mineru_parse_mode": "unknown", "pua_count": 3})
    assert "未知" in n and "私用区" in n


class _FakeDb:
    ROWS: list[dict] = []

    def execute_sql(self, sql: str, params: tuple = ()):  # noqa: ANN201
        return [dict(r) for r in self.ROWS]


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mcp_server, "get_db", lambda: _FakeDb())
    return _FakeDb


def test_read_book_chunk_lifts_provenance_out_of_rows(fake):
    fake.ROWS = [
        {"chunk_seq": 1, "heading_path": "§1", "char_start": 0, "content": "正文",
         "book_title": "书", "book_path": "E:\\x.md",
         "mineru_form": "dir", "mineru_parse_mode": "txt",
         "kangxi_radical_count": 7, "pua_count": 0},
        {"chunk_seq": 2, "heading_path": "§1", "char_start": 10, "content": "正文2",
         "book_title": "书", "book_path": "E:\\x.md",
         "mineru_form": "dir", "mineru_parse_mode": "txt",
         "kangxi_radical_count": 7, "pua_count": 0},
    ]
    out = json.loads(mcp_server.read_book_chunk("G", 1, 1))
    assert out["text_provenance"]["mineru_parse_mode"] == "txt"
    assert "7" in out["text_trust"]
    # 溯源字段不逐 chunk 重复
    for c in out["chunks"]:
        assert "kangxi_radical_count" not in c and "mineru_form" not in c
