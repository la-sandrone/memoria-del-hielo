"""
测试: src/core/search.py 的书级语义检索（find_books）—— SQL 形状 + 纯函数收敛。

不碰数据库：只断言 SQL 文本与纯函数行为（与 tests/test_remove_book_cli.py 同思路）。
护栏：
  · SQL 必须把查询向量放进派生表解析一次（否则 21KB 文本逐行重解析 → 全表 33s，见 search.py 注释）
  · signal 只给数字，不给出存在性结论（note 必须写明这一点，防将来有人"顺手加个阈值"）
"""

import pytest

from src.core.search import BOOK_FIND_NOTE, _book_rows_to_result, _find_books_sql


def test_sql_shape_derived_table_and_order():
    sql, params = _find_books_sql(None, None)
    assert "(SELECT VEC_FromText(?) AS qv) q" in sql            # 向量只解析一次
    assert "VEC_DISTANCE_COSINE(b.embedding, q.qv)" in sql      # 用的是派生表的列，不是逐行求值
    assert "FROM documents_book b" in sql
    assert sql.rstrip().endswith("ORDER BY _distance ASC")
    assert "LIMIT" not in sql.upper()                            # 全取：signal 的 gap/z 需要全库分布
    assert params == []


def test_sql_bucket_and_tags_params_order():
    sql, params = _find_books_sql("图书", "化学,光学系列")
    assert "b.bucket = ?" in sql
    assert "FIND_IN_SET(?, b.tags) OR FIND_IN_SET(?, b.tags)" in sql
    assert params == ["图书", "化学", "光学系列"]                # 顺序必须与 SQL 占位符一致


def test_sql_no_tag_predicate_when_tags_none():
    sql, _ = _find_books_sql(None, None)
    assert "FIND_IN_SET" not in sql


def _rows(dists):
    return [
        {
            "guid": f"g{i}",
            "title": f"书{i}",
            "bucket": "图书",
            "tags": "某tag",
            "chunks": 10 + i,
            "_distance": d,
        }
        for i, d in enumerate(dists)
    ]


def test_result_scores_and_signal():
    rows = _rows([0.30, 0.35, 0.40, 0.45, 0.50, 0.60])
    out = _book_rows_to_result("测试", rows, 3)
    assert [b["guid"] for b in out["books"]] == ["g0", "g1", "g2"]
    assert out["books"][0]["score"] == pytest.approx(0.7)       # 1 - 0.30
    assert out["books"][0]["chunks"] == 10
    assert out["signal"]["corpus_books"] == 6
    assert out["signal"]["top1_distance"] == pytest.approx(0.3)
    assert out["signal"]["gap"] == pytest.approx(0.2)           # d5(第5名) - d1 = 0.50 - 0.30
    assert out["signal"]["z"] > 0
    assert out["note"] == BOOK_FIND_NOTE
    assert "不判断" in out["note"] and "search_docs" in out["note"]


def test_result_limit_clamped_and_empty():
    rows = _rows([0.1, 0.2])
    assert len(_book_rows_to_result("q", rows, 99)["books"]) == 2
    assert _book_rows_to_result("q", rows, 0)["books"] == []
    assert _book_rows_to_result("q", rows, -3)["books"] == []
    empty = _book_rows_to_result("q", [], 10)
    assert empty["books"] == []
    assert empty["signal"] == {"corpus_books": 0}


def test_result_short_corpus_and_single_row():
    # 书数不足 5 时 gap 取最后一个，不越界
    out = _book_rows_to_result("q", _rows([0.2, 0.5]), 5)
    assert out["signal"]["gap"] == pytest.approx(0.3)
    # 单本书：标准差为 0 → z 走安全分支
    one = _book_rows_to_result("q", _rows([0.42]), 5)
    assert one["signal"]["z"] == 0.0
    assert one["books"][0]["score"] == pytest.approx(0.58)
