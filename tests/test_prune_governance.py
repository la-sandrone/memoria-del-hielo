"""停词剪枝的治理：可见 / 重试 / 不削弱召回。

背景（2026-11-07）：第一版剪枝按「任一单字命中就丢」，把 会话 / 中国 / 基金 这类真词
整词丢掉；而「全停词」的查询会被剪成空串，全文路直接 return [] —— 调用方看到的
和「库里没有」一模一样。这里锁住三条纪律：

  1. **粒度对齐**：字符级词表只判单字 token 与「整词全由虚字构成」。
  2. **可见**：search_meta.query_terms 带出 kept / pruned；剪枝率异常要 WARNING。
  3. **不削弱召回**：剪枝后 0 命中必须用未剪词表重试一层，之后才轮到 LIKE 兜底。
"""

from __future__ import annotations

import pytest

from src.core.search import SearchEngine
from src.core.tokenizer import build_cjk_query_raw, build_cjk_query_v2, query_term_split

# ── 1. 粒度对齐 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("query,word", [
    ("会话 管理", "会话"),
    ("中国 邮编", "中国"),
    ("基金会史", "基金"),
    ("对话 系统", "对话"),
    ("上学 路上", "上学"),
])
def test_real_words_survive_pruning(query: str, word: str) -> None:
    assert word in build_cjk_query_v2(query).split()


def test_pure_function_bigram_is_prunable() -> None:
    """整词全是虚字才允许丢（在的 / 是在 这类）。"""
    kept, pruned = query_term_split("在的 是在")
    assert pruned, "纯虚词 bigram 没有被判定为可剪"
    assert kept, "全被剪时必须退回未剪词表（不许返回空）"


def test_pruning_never_empties_the_query() -> None:
    for q in ("的的的", "是在", "the and of", "的"):
        kept, _ = query_term_split(q)
        assert kept, f"{q!r} 被剪成空串 —— 会让全文路静默 return []"


def test_pruned_list_is_reported_even_when_all_pruned() -> None:
    """全被剪时 kept 退回未剪词表，但 pruned 照样要带出来。

    否则「剪得最狠的那一次」恰好是唯一不告警的一次。
    """
    kept, pruned = query_term_split("在的 是在 的的")
    assert pruned
    assert len(pruned) >= len(kept), "调用方正是用这个不等式判异常"


def test_dedup_keeps_order() -> None:
    terms = build_cjk_query_v2("基金会史 基金会历史").split()
    assert len(terms) == len(set(terms))


def test_raw_builder_skips_pruning() -> None:
    assert build_cjk_query_raw("会话 在的").split() == ["会话", "在的"]


# ── 2. 可见：剪枝率异常要告警 ────────────────────────────────────────


class _OfflineEngine(SearchEngine):
    """绕过 __init__（不连库）；全文路返回罐装结果，向量路直接抛。"""

    def __init__(self, ft_hits: list[dict]):
        self.ft_hits = ft_hits
        self.calls: list[bool] = []
        self.max_per_book = 3          # 真实 SearchEngine 从 config 取，这里绕过了 __init__

    def _book_fulltext(self, query, bucket, tags, limit, degraded, prune=True):
        self.calls.append(prune)
        # 罐装行必须带 _score：合并阶段要读它（真实 _book_fulltext 一定给）
        return [dict(h, _score=1.0) for h in self.ft_hits] if not prune else []

    def _book_vector(self, query_vec, bucket, tags, limit, degraded):  # noqa: ANN001
        return []

    @property
    def _embedding(self):  # type: ignore[override]
        raise RuntimeError("离线测试，不嵌入")


def test_high_prune_ratio_warns() -> None:
    eng = _OfflineEngine(ft_hits=[])
    with pytest.warns(UserWarning, match="停词剪枝比例过高"):
        eng.search("在的 是在 的的", with_meta=True)


def test_meta_exposes_query_terms() -> None:
    eng = _OfflineEngine(ft_hits=[{"chunk_id": 1, "book_guid": "b", "content": "x",
                                   "bucket": "", "tags": "", "book_title": "T",
                                   "book_path": "", "heading_path": "", "chunk_seq": 1}])
    meta = eng.search("会话 管理", with_meta=True)["search_meta"]
    assert meta["query_terms"]["kept"] == ["会话", "管理"]
    assert meta["query_terms"]["pruned"] == []
    assert meta["retried_unpruned"] is False


# ── 3. 不削弱召回：0 命中时用未剪词表重试一层 ────────────────────────


def test_zero_hit_after_pruning_retries_without_pruning() -> None:
    eng = _OfflineEngine(ft_hits=[{"chunk_id": 7, "book_guid": "b", "content": "x",
                                   "bucket": "", "tags": "", "book_title": "T",
                                   "book_path": "", "heading_path": "", "chunk_seq": 1}])
    with pytest.warns(UserWarning, match="未剪词表重试"):
        out = eng.search("在的 会话", with_meta=True)
    assert eng.calls == [True, False], f"重试层次不对：{eng.calls}"
    assert out["search_meta"]["retried_unpruned"] is True
    assert out["results"], "重试救回的结果没进结果集"


def test_no_retry_when_nothing_pruned() -> None:
    """没有可剪的词就不该多打一次（那是白花的一次全文查询）。"""
    eng = _OfflineEngine(ft_hits=[])
    eng.search("会话 管理", with_meta=True)
    assert eng.calls == [True], f"不必要的重试：{eng.calls}"
