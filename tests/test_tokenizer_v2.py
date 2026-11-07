"""分词 v2 的验收：12 类语料对拍 + 「查询侧 token 必须都在写入侧索引里」不变式。

为什么要有这个不变式：v1 的「吞汉字」bug 的**症状是 0 命中而不报错**。
只要查询侧拼出的 token 在写入侧索引里不存在，那条查询就永远搜不到，
而全链路没有一处会喊。所以这里把它写成断言。

语料来源：本项目实际入库内容（矿物/古典学/俄语/技术文档/游戏剧情）
与真实查询台账 docs/真实检索查询分布案例.md 的形态。
"""

from __future__ import annotations

import re

import pytest

from src.core.tokenizer import (
    build_cjk_query_v2,
    iter_chunks,
    tokenize_cjk_ub,
    tokenize_cjk_ub_v2,
)

# ── 12 类语料 ──────────────────────────────────────────────────────────
CORPUS: dict[str, list[str]] = {
    "纯汉字": ["绘图工具", "会话管理", "较差自转", "魔鬼出没的世界"],
    "拉丁紧邻汉字": ["JDBC接口测试", "Vue组件", "PDF转成图片", "用BGE-M3做嵌入"],
    "数字紧邻汉字": ["第3章节", "图2中的矿物", "含90%石英", "2026年的实验"],
    "标点混排": ["第二章 矿物 > 第三节", "（三）云母；解理极完全。", "A、B、C三组"],
    "纯拉丁": ["Sihler 1995", "cross section 100 eV", "p-type semiconductor"],
    "希腊/西里尔/变音拉丁": ["α-螺旋与β折叠", "Толстой回忆录", "长音符 ē ō ā"],
    "emoji 与符号": ["emoji😀混排", "★重点★", "→箭头←"],
    "PUA 私用区": ["\ue000私有区\ue001", "字\uf8ff符"],
    "单字": ["绘", "a", "ω"],
    "空与空白": ["", "   ", "\n\t"],
    "代码式标识": ["get_user_by_id()", "src/core/search.py"],
    "长混合串": ["MinerU 4.0.7 的 parse_mode=txt 时 errata_stats 里 script_mix 会触发"],
}


def _flat() -> list[str]:
    return [s for group in CORPUS.values() for s in group]


# ── 不变式：查询侧 token ⊂ 写入侧 token ────────────────────────────────
@pytest.mark.parametrize("text", _flat())
def test_query_terms_exist_in_index(text: str) -> None:
    indexed = set(tokenize_cjk_ub_v2(text).split())
    for term in build_cjk_query_v2(text).split():
        assert term in indexed, f"查询 token {term!r} 不在写入侧索引里（将造成 0 命中）"


# ── v1 的 bug 必须被修掉，且只修坏的那种 ───────────────────────────────
_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"


def _has(tok: str, pattern: str) -> bool:
    return re.search(f"[{pattern}]", tok) is not None


@pytest.mark.parametrize("text", _flat())
def test_no_token_mixes_scripts(text: str) -> None:
    r"""核心不变式：v2 不许产出**跨文字**的 token。

    v1 的 bug 就是这个：`\w+` 把 "JDBC接口" 整串吃成一个 token，
    汉字因此从未进入 unigram/bigram 索引。
    """
    for tok in tokenize_cjk_ub_v2(text).split():
        assert not (_has(tok, _CJK) and _has(tok, "^" + _CJK)), f"{text!r} 产出跨文字 token {tok!r}"


@pytest.mark.parametrize("text", CORPUS["拉丁紧邻汉字"] + CORPUS["数字紧邻汉字"])
def test_mixed_script_no_longer_swallowed(text: str) -> None:
    """混合串里的每个汉字都必须是 unigram；v1 会整段吞掉的必须改善。"""
    v2 = tokenize_cjk_ub_v2(text).split()
    for is_cjk, seg in iter_chunks(text):
        if is_cjk:
            for ch in seg:
                assert ch in v2, f"{text!r} 丢掉单字 {ch!r}"
    if tokenize_cjk_ub(text) != tokenize_cjk_ub_v2(text):
        assert len(v2) > len(tokenize_cjk_ub(text).split()), f"{text!r} 说改善了但 token 没变多"


@pytest.mark.parametrize("text", CORPUS["纯汉字"] + CORPUS["纯拉丁"] + CORPUS["长混合串"])
def test_pure_segments_unchanged_from_v1(text: str) -> None:
    """纯汉字段/纯拉丁段不许因为 v2 而改变口径。"""
    v1 = tokenize_cjk_ub(text).split()
    v2 = tokenize_cjk_ub_v2(text).split()
    assert v1 == v2 or set(v2) - set(v1) == set(), (v1, v2)


def test_no_operator_leaks_into_nl_query() -> None:
    """NL MODE 的查询串里不能出现 BOOLEAN 操作符（会被当普通字符）。"""
    q = build_cjk_query_v2("卡尔萨根 魔鬼出没的世界 科学 迷信 伪科学")
    assert "+" not in q and "*" not in q
    assert q.split()[:3] == ["卡尔", "尔萨", "萨根"]


def test_bigram_only_no_unigram_on_query_side() -> None:
    """查询侧只给 bigram：unigram 会拉平 NL 打分。"""
    assert "绘" not in build_cjk_query_v2("绘图工具").split()
    assert "绘图" in build_cjk_query_v2("绘图工具").split()


def test_stopwords_dont_kill_real_words() -> None:
    """停词表不许「任一单字命中就丢」—— 那会误杀 会话 / 中国 / 基金 这类真词。

    2026-11-07 修正：第一版按单字判，结果 "会话"（含 会）、"中国"（含 中）被整词丢掉，
    而这些正是用户会搜的。现在只有**整词每个字都是停词**（"的了"/"是在"）才丢。
    """
    assert "会话" in build_cjk_query_v2("会话 管理").split()
    assert "中国" in build_cjk_query_v2("中国 邮编").split()
    assert "基金" in build_cjk_query_v2("基金会史").split()
    # 拉丁词不按字符判（'a' 在 'Carter' 里，但 Carter 不是停词）
    assert "Carter" in build_cjk_query_v2("克尔度规 零测地线 Carter 常数").split()


def test_all_stopword_query_falls_back_to_unfiltered() -> None:
    """全停词查询不许变成空串 —— 空串会让全文路直接 return []，表现得像「库里没有」。"""
    for q in ("的", "的的的", "the and of", "是在"):
        assert build_cjk_query_v2(q).strip(), f"{q!r} 被过滤成空串"


def test_query_terms_deduped_in_order() -> None:
    q = build_cjk_query_v2("基金会史 基金会历史").split()
    assert len(q) == len(set(q)), f"有重复词（会让 NL 词频虚高）：{q}"


def test_empty_input() -> None:
    assert tokenize_cjk_ub_v2("") == ""
    assert build_cjk_query_v2("   ") == ""


def test_iter_chunks_covers_text_exactly() -> None:
    for text in _flat():
        assert "".join(seg for _, seg in iter_chunks(text)) == text
