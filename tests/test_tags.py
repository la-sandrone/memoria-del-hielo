"""
测试: src/core/tags.py —— tag 语法权威层（哨兵 OMNIA / 读路径折叠 / 写路径拦截）

不碰数据库：全部为纯函数用例。
护栏：
  · 哨兵与空串同义（都不生成 tag 谓词）；哨兵与真 tag 混用必须报错
    （ALL OR <tag> 恒等于 ALL，无意义）
  · 保留字与 SQL 结构性字符一律拦截，但只按「元素级精确匹配」拦——
    Inorganic 含 IN、Matchov 含 MATCH 都不得误伤（否则真实书名 tag 会被大面积误杀）
"""

import pytest

from src.core.tags import (
    TAG_ALL,
    TagSyntaxError,
    audit_tag_element,
    is_tag_all,
    normalize_query_tags,
    split_tags,
    validate_single_tag,
    validate_write_tags,
)


# ---------- 读路径：哨兵 / 空串同义 ----------

@pytest.mark.parametrize("value", [None, "", "   ", ",", "OMNIA", "omnia", "  Omnia "])
def test_query_tags_all_forms_are_noop(value):
    """不传 / 空串 / 哨兵（任意大小写与空白）→ None（不生成 tag 谓词）。"""
    assert normalize_query_tags(value) is None


def test_query_tags_plain_and_multi():
    assert normalize_query_tags("化学") == "化学"
    assert normalize_query_tags(" 化学 , 光学系列 ") == "化学,光学系列"


@pytest.mark.parametrize("value", ["OMNIA,化学", "化学,OMNIA", "omnia,化学"])
def test_query_tags_sentinel_mixed_raises(value):
    with pytest.raises(TagSyntaxError, match="搜全部数据"):
        normalize_query_tags(value)


def test_query_tags_rejects_keyword_and_punctuation():
    with pytest.raises(TagSyntaxError):
        normalize_query_tags("select")
    with pytest.raises(TagSyntaxError):
        normalize_query_tags("a'b")


# ---------- 写路径：放行（含库里真实 tag 形态）----------

@pytest.mark.parametrize("value", [
    "著名科幻",
    "物理、天文、数学",
    "Inorganic",
    "Matchov",
    "s11",
    "The Science of Solar System Ices 2013",
    "0004-637X_2F824_2F2_2F101",
    "j.cell.2016.12.014",
])
def test_write_tags_accepts_real_world_values(value):
    assert validate_write_tags(value) == value


def test_write_tags_normalizes_and_drops_empty():
    assert validate_write_tags(" a ,, b , ") == "a,b"
    assert validate_write_tags("") == ""
    assert validate_write_tags(None) == ""


@pytest.mark.parametrize("value", [
    "OMNIA",          # 哨兵
    "omnia",          # 哨兵（小写）
    "化学,OMNIA",      # 混入哨兵
    "in",             # SQL 保留字
    "DATA",           # 保留字（大写）
    "a;b",            # 分号
    "a`b",            # 反引号
    "a\\b",           # 反斜杠
    "a\nb",           # 换行
])
def test_write_tags_rejects(value):
    with pytest.raises(TagSyntaxError):
        validate_write_tags(value)


# ---------- 单值来源（目录名自动 tag）----------

def test_single_tag_ok_and_empty():
    assert validate_single_tag("物理、天文、数学", "目录名") == "物理、天文、数学"
    assert validate_single_tag("", "目录名") == ""
    assert validate_single_tag(None, "目录名") == ""


def test_single_tag_comma_is_fatal():
    """目录名含逗号会被静默切成两个 tag —— 必须报错，不许静默降级。"""
    with pytest.raises(TagSyntaxError, match="逗号"):
        validate_single_tag("Modern Quantum Mechanics, Sakurai", "目录名")


def test_single_tag_sentinel_rejected():
    with pytest.raises(TagSyntaxError, match="OMNIA"):
        validate_single_tag(TAG_ALL, "目录名")


# ---------- 辅助 ----------

def test_is_tag_all():
    assert is_tag_all("OMNIA")
    assert is_tag_all("  omnia ")
    assert not is_tag_all("OMNIAX")


def test_split_tags():
    assert split_tags("a,, b ,") == ["a", "b"]
    assert split_tags("") == []
    assert split_tags(None) == []


# ---------- 元素级体检判据（audit 与写路径校验共用）----------

@pytest.mark.parametrize("value,needle", [
    ("OMNIA", "保留哨兵"),
    ("select", "保留字"),
    ("a;b", "非法字符"),
    ("a`b", "非法字符"),
])
def test_audit_tag_element_reports_reason(value, needle):
    reason = audit_tag_element(value)
    assert reason is not None
    assert needle in reason


@pytest.mark.parametrize("value", [
    "著名科幻",
    "物理、天文、数学",
    "Inorganic",
    "s11",
    "The Science of Solar System Ices 2013",
])
def test_audit_tag_element_clean(value):
    assert audit_tag_element(value) is None


# ---------- 全库体检的分类逻辑（假 DB，不碰真库）----------

class _FakeDB:
    ROWS = [
        {"guid": "g1", "bucket": "图书", "key_metadata": "正常书", "tags": "著名科幻"},
        {"guid": "g2", "bucket": "图书", "key_metadata": "无tag书", "tags": ""},
        {"guid": "g3", "bucket": "图书", "key_metadata": "破CSV", "tags": ",a,b"},
        {"guid": "g4", "bucket": "图书", "key_metadata": "哨兵落库", "tags": "OMNIA"},
        {"guid": "g5", "bucket": "图书", "key_metadata": "保留字", "tags": "历史,select"},
    ]

    def execute_sql(self, sql, params=None):
        return list(self.ROWS)


def test_audit_tags_classifies_and_flags_unhealthy(monkeypatch):
    from src.core import tag_manager

    monkeypatch.setattr(tag_manager, "get_db", lambda: _FakeDB())
    report = tag_manager.audit_tags("book")

    assert report["tables"] == ["documents_book"]
    assert report["stats"]["rows"] == 5
    assert report["stats"]["single_tag_rows"] == 2          # 著名科幻 / OMNIA
    assert report["counts"] == {"empty": 1, "broken_csv": 1, "sentinel": 1, "illegal": 1}
    assert report["healthy"] is False
    assert report["findings"]["empty"][0]["guid"] == "g2"
    assert report["findings"]["sentinel"][0]["guid"] == "g4"
    assert report["findings"]["illegal"][0]["element"] == "select"
    assert report["findings"]["broken_csv"][0]["guid"] == "g3"


def test_audit_tags_healthy_library(monkeypatch):
    from src.core import tag_manager

    class GoodDB:
        def execute_sql(self, sql, params=None):
            return [
                {"guid": "g1", "bucket": "图书", "key_metadata": "A", "tags": "著名科幻"},
                {"guid": "g2", "bucket": "图书", "key_metadata": "B", "tags": "历史,化学"},
            ]

    monkeypatch.setattr(tag_manager, "get_db", lambda: GoodDB())
    report = tag_manager.audit_tags("all")
    assert report["healthy"] is True
    assert report["counts"] == {"empty": 0, "broken_csv": 0, "sentinel": 0, "illegal": 0}
    assert report["stats"]["distinct_elements"] == 3        # 著名科幻/历史/化学（三表同源假数据）
    assert len(report["tables"]) == 3
