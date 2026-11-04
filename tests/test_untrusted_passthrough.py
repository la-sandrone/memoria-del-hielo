"""测试: --untrusted 注记在导入链路上的**透传**。

为什么单测这个：这条链有 5 层（CLI → import_book_directory/import_book_file
→ import_directory → import_candidate → import_file），任何一层漏写关键字，
注记就静默消失 —— 而**注记丢了不会报错**，只会让那本书在 MCP 里少一句警告。
用替身把每层都点一遍，防的就是这种静默降级。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.importers import book_importer as bi


class _Recorder:
    """BookImporter 替身：只记录被调用时收到的关键字参数。"""

    calls: list[dict] = []

    def import_file(self, *args, **kwargs):
        self.calls.append({"fn": "import_file", "kwargs": kwargs})
        return {"status": "ok"}

    def import_candidate(self, *args, **kwargs):
        self.calls.append({"fn": "import_candidate", "kwargs": kwargs})
        return {"status": "ok"}

    def import_directory(self, *args, **kwargs):
        self.calls.append({"fn": "import_directory", "kwargs": kwargs})
        return [{"status": "ok"}]


@pytest.fixture
def patched(monkeypatch):
    """把 BookImporter 换成替身。**不做 autouse** —— 签名一致性用例要读真类。"""
    _Recorder.calls = []
    monkeypatch.setattr(bi, "BookImporter", _Recorder)
    return None


def test_import_book_file_passes_note_for_md(patched, tmp_path: Path):
    p = tmp_path / "book.md"
    p.write_text("x", encoding="utf-8")
    bi.import_book_file(str(p), untrusted_note="NOTE")
    assert _Recorder.calls[0]["fn"] == "import_file"
    assert _Recorder.calls[0]["kwargs"]["untrusted_note"] == "NOTE"


def test_import_book_file_passes_note_for_zip(patched, tmp_path: Path):
    p = tmp_path / "book.zip"
    p.write_bytes(b"PK\x03\x04")
    bi.import_book_file(str(p), untrusted_note="NOTE")
    assert _Recorder.calls[0]["fn"] == "import_candidate"
    assert _Recorder.calls[0]["kwargs"]["untrusted_note"] == "NOTE"


def test_import_book_directory_passes_note(patched, tmp_path: Path):
    bi.import_book_directory(str(tmp_path), untrusted_note="NOTE")
    assert _Recorder.calls[0]["fn"] == "import_directory"
    assert _Recorder.calls[0]["kwargs"]["untrusted_note"] == "NOTE"


def test_default_is_empty_string_not_none():
    """缺省必须是 "" 而不是 None —— import_file 里是 `if untrusted_note:`，
    None 也能过，但库里会多一种"没传"的表示，属于给自己挖两种真相。"""
    import inspect

    for fn in (bi.import_book_file, bi.import_book_directory,
               bi.BookImporter.import_directory, bi.BookImporter.import_candidate,
               bi.BookImporter.import_file):
        sig = inspect.signature(fn)
        assert sig.parameters["untrusted_note"].default == "", fn.__qualname__
