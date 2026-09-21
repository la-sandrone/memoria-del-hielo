"""
测试: src/cli/mhi_remove_book.py（guid 模式 + 模式互斥 + 删除语句构造）

原则：破坏性测试不碰真库。凡涉及删除的断言只校验
`_build_delete_statements()` 产出的 SQL 文本与参数；
涉及 CLI 流程的用假 DB（FakeDB），一旦进入 transaction() 立即失败。
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from src.cli import mhi_remove_book as rb


# ---------- 纯函数：guid 解析 ----------

def test_parse_guids_comma_and_repeat():
    """逗号分割与重复选项可混用，strip + 去空 + 保序去重。"""
    assert rb._parse_guids(("a,b", " b , ,c", "a", "")) == ["a", "b", "c"]


def test_parse_guids_empty():
    assert rb._parse_guids(()) == []
    assert rb._parse_guids(("", ",,")) == []


def test_parse_guids_case_preserved():
    """大小写原样保留（库列 ci，匹配宽容，但输入不做规范化）。"""
    assert rb._parse_guids(("ABC-DEF",)) == ["ABC-DEF"]


# ---------- 删除语句构造（破坏性：只列 SQL，不执行） ----------

def test_build_delete_statements_sql():
    stmts = rb._build_delete_statements(["g1", "g2"])
    assert stmts == [
        (
            "DELETE FROM page_footnotes WHERE book_guid IN (?,?)",
            ("g1", "g2"),
        ),
        (
            "DELETE FROM documents_book WHERE guid IN (?,?)",
            ("g1", "g2"),
        ),
    ]


def test_build_delete_statements_parametrized():
    """guid 只出现在 params 里，绝不字符串插值进 SQL（注入安全）。"""
    guids = ["x'; DROP TABLE documents_book; --", "g2"]
    for sql, params in rb._build_delete_statements(guids):
        assert "DROP TABLE" not in sql
        assert params == tuple(guids)
        assert sql.count("?") == len(guids)


def test_build_delete_statements_order():
    """先删 footnotes 再删 book（活库 footnotes 无 FK，顺序不能反）。"""
    stmts = rb._build_delete_statements(["g1"])
    assert stmts[0][0].startswith("DELETE FROM page_footnotes")
    assert stmts[1][0].startswith("DELETE FROM documents_book")


# ---------- 未命中比对 ----------

def test_missing_guids_case_insensitive():
    found = [{"guid": "abc-123"}]
    assert rb._missing_guids(["ABC-123"], found) == []
    assert rb._missing_guids(["abc-123", "zzz"], found) == ["zzz"]


# ---------- CLI：模式互斥 / 缺参 ----------

def _invoke(args):
    return CliRunner().invoke(rb.main, args)


def test_cli_requires_one_selector():
    res = _invoke([])
    assert res.exit_code == 2
    assert "必须给 PATH 或 --guid 之一" in res.output


def test_cli_empty_guid_counts_as_absent():
    res = _invoke(["--guid", ""])
    assert res.exit_code == 2
    assert "必须给 PATH 或 --guid 之一" in res.output


def test_cli_path_and_guid_mutually_exclusive():
    res = _invoke([str(Path(__file__).resolve()), "--guid", "abc"])
    assert res.exit_code == 2
    assert "互斥" in res.output


# ---------- CLI：dry-run 走通（假 DB，严禁真删） ----------

class _FakeDB:
    """execute_sql 返回固定行；一旦进入 transaction() 立刻炸。"""

    def __init__(self, rows):
        self._rows = rows
        self.last_sql = None

    def execute_sql(self, sql, params=None, dictionary=True):
        self.last_sql = sql
        return self._rows

    def transaction(self):  # pragma: no cover - 破坏性路径不得进入
        raise AssertionError("破坏性测试不得进入删除事务")


def _patch_db(monkeypatch, rows):
    fake = _FakeDB(rows)
    monkeypatch.setattr(rb, "get_db", lambda: fake)
    return fake


def test_cli_guid_dry_run_lists_targets(monkeypatch):
    rows = [
        {
            "guid": "abc-123",
            "key_metadata": "《测试书》",
            "source_path": "E:\\tmp\\测试书.md",
            "chunks": 7,
        }
    ]
    _patch_db(monkeypatch, rows)
    res = _invoke(["--guid", "abc-123", "--dry-run"])
    assert res.exit_code == 0
    assert "abc-123" in res.output
    assert "《测试书》" in res.output
    assert "E:\\tmp\\测试书.md" in res.output
    assert "未删除" in res.output


def test_cli_guid_all_missing_exits_1(monkeypatch):
    _patch_db(monkeypatch, [])
    res = _invoke(["--guid", "nope", "--dry-run"])
    assert res.exit_code == 1
    assert "无匹配 guid" in res.output


def test_cli_guid_partial_missing_requires_yes(monkeypatch):
    """部分命中且未给 --yes：报未命中清单并以 1 退出，不得进入删除。"""
    rows = [
        {
            "guid": "abc-123",
            "key_metadata": "《测试书》",
            "source_path": "E:\\tmp\\测试书.md",
            "chunks": 7,
        }
    ]
    _patch_db(monkeypatch, rows)
    res = _invoke(["--guid", "abc-123,zzz"])
    assert res.exit_code == 1
    assert "未命中" in res.output
    assert "zzz" in res.output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
