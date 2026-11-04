"""测试: src/cli/mhi_annotate.py 的直连模式（--guid/--path/--untrusted）。

原则：不碰真库。FakeDB 只回答三种 SQL（全库索引 / 取 errata_stats / UPDATE），
其余一律显式失败 —— 静默忽略会让「测试通过但什么都没写」蒙混过关。
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from src.cli import mhi_annotate as an
from src.core.text_errata import UNTRUSTED_DEFAULT_NOTE


class _FakeDB:
    def __init__(self, books: list[dict]):
        self.books = books
        self.updates: list[tuple[str, str]] = []

    def execute_sql(self, sql: str, params=()):
        s = " ".join(sql.split())
        if s.startswith("SELECT guid, source_path, key_metadata"):
            return [
                {"guid": b["guid"], "source_path": b.get("source_path", ""),
                 "key_metadata": b.get("key_metadata", "")}
                for b in self.books
            ]
        if s.startswith("SELECT errata_stats FROM documents_book WHERE guid"):
            for b in self.books:
                if b["guid"] == params[0]:
                    return [{"errata_stats": b.get("errata_stats", "{}")}]
            return []
        if s.startswith("UPDATE documents_book SET errata_stats"):
            # 参数顺序与 SQL 占位符一致：(errata_json, guid)
            self.updates.append((params[0], params[1]))
            return []
        raise AssertionError(f"FakeDB 没实现这条 SQL: {s}")


BOOK = {
    "guid": "a1b2c3d4-0000-0000-0000-000000000001",
    "key_metadata": "Vulgar Latin",
    "source_path": "/mnt/<drive>/Vulgar Latin (József Herman).zip",
    "errata_stats": json.dumps({"criteria_version": "v1", "chars": 10,
                                "words": 2, "script_mix": 1}),
}


def _invoke(args):
    return CliRunner().invoke(an.main, args)


def _patch(monkeypatch, books):
    db = _FakeDB(books)
    monkeypatch.setattr(an, "get_db", lambda: db)
    return db


# ---------- 互斥与必填 ----------

def test_from_and_guid_are_mutually_exclusive(monkeypatch):
    _patch(monkeypatch, [BOOK])
    res = _invoke(["--from", "/tmp/manifest.jsonl", "--guid", BOOK["guid"],
                   "--untrusted"])
    assert res.exit_code != 0
    assert "互斥" in res.output


def test_needs_from_or_direct():
    res = _invoke([])
    assert res.exit_code != 0
    assert "--guid" in res.output


def test_direct_requires_untrusted(monkeypatch):
    _patch(monkeypatch, [BOOK])
    res = _invoke(["--guid", BOOK["guid"]])
    assert res.exit_code != 0
    assert "--untrusted" in res.output


# ---------- 直连模式：dry-run 不写库 ----------

def test_direct_dry_run_does_not_write(monkeypatch):
    db = _patch(monkeypatch, [BOOK])
    res = _invoke(["--guid", BOOK["guid"], "--untrusted"])
    assert res.exit_code == 0
    assert "点名 1 本" in res.output
    assert "注记新增 1 条" in res.output
    assert db.updates == []


def test_direct_apply_writes_note(monkeypatch):
    db = _patch(monkeypatch, [BOOK])
    res = _invoke(["--guid", BOOK["guid"], "--untrusted", "--apply"])
    assert res.exit_code == 0
    assert len(db.updates) == 1
    new_json, guid = db.updates[0]
    assert guid == BOOK["guid"]
    saved = json.loads(new_json)
    notes = saved["notes"]
    assert len(notes) == 1
    assert notes[0]["source"] == "annotate:--untrusted"
    assert notes[0]["text"] == UNTRUSTED_DEFAULT_NOTE
    # 原有的机械计数不能被这次注记抹掉
    assert saved["script_mix"] == 1


def test_custom_text_overrides_default(monkeypatch):
    db = _patch(monkeypatch, [BOOK])
    res = _invoke(["--guid", BOOK["guid"], "--untrusted", "本批为扫描件", "--apply"])
    assert res.exit_code == 0
    saved = json.loads(db.updates[0][0])
    assert saved["notes"][0]["text"] == "本批为扫描件"


def test_unknown_guid_reports_failure(monkeypatch):
    _patch(monkeypatch, [BOOK])
    res = _invoke(["--guid", "ffffffff-0000", "--untrusted", "--apply"])
    assert res.exit_code == 1
    assert "匹配失败" in res.output


# ---------- --path 归一 ----------

def test_path_match_normalizes_windows_form(monkeypatch):
    db = _patch(monkeypatch, [BOOK])
    res = _invoke(["--path", r"R:\Vulgar Latin (József Herman).zip",
                   "--untrusted", "--apply"])
    assert res.exit_code == 0
    assert len(db.updates) == 1


def test_path_miss_is_reported(monkeypatch):
    _patch(monkeypatch, [BOOK])
    res = _invoke(["--path", "/mnt/<drive>/不存在的书.zip", "--untrusted"])
    assert res.exit_code == 1
    assert "匹配失败" in res.output


def test_multiple_targets_all_get_note(monkeypatch):
    other = dict(BOOK, guid="a1b2c3d4-0000-0000-0000-000000000002",
                 key_metadata="Herman", source_path="/mnt/<drive>/other.zip")
    db = _patch(monkeypatch, [BOOK, other])
    res = _invoke(["--guid", BOOK["guid"], "--guid", other["guid"],
                   "--untrusted", "--apply"])
    assert res.exit_code == 0
    assert len(db.updates) == 2
    for new_json, _guid in db.updates:
        assert json.loads(new_json)["notes"]


# ---------- 短选项（手伤场景：一条命令压到最短） ----------

def test_short_options_work(monkeypatch):
    db = _patch(monkeypatch, [BOOK])
    res = _invoke(["-g", BOOK["guid"], "-u", "-y"])
    assert res.exit_code == 0
    assert len(db.updates) == 1
    assert json.loads(db.updates[0][0])["notes"][0]["text"] == UNTRUSTED_DEFAULT_NOTE


def test_short_path_option(monkeypatch):
    db = _patch(monkeypatch, [BOOK])
    res = _invoke(["-p", BOOK["source_path"], "-u", "-y"])
    assert res.exit_code == 0
    assert len(db.updates) == 1
