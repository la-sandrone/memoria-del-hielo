"""
测试: 显式单路检索（--fulltext-only / --vector-only）无命中时的自动回退。

不碰数据库：用 _StubEngine 覆盖 _book_search/_other_search 返回罐装结果，
只验证 search() 的回退接线与 search_meta 形状；真实检索路径由 CLI/MCP 实测覆盖。
护栏（防"静默空手而归"）：
  · 单路无命中必须回退并**标注** fallback（调用方要能区分「库里没有」与「该路打不中」）
  · 双路（hybrid）无命中时 fallback 必须是 null —— 那才是"真没有"的信号
  · with_meta=False 必须保持旧的 list 契约（向后兼容）
"""

from src.core.search import SearchEngine, _fallback_target

FT = [{"guid": "ft1", "score": 0.9, "source": "ft"}]
VEC = [{"guid": "vec1", "score": 0.8, "source": "vec"}]


class _StubEngine(SearchEngine):
    """绕过 __init__（不连库）；按 flag 返回罐装结果。"""

    def __init__(self, ft_hits, vec_hits):
        self.ft_hits = ft_hits
        self.vec_hits = vec_hits

    def _book_search(self, query, bucket, tags, limit, fulltext_only, vector_only,
                     is_cjk, ft_w, vec_w):
        out = []
        if not vector_only:
            out += [dict(h) for h in self.ft_hits]
        if not fulltext_only:
            out += [dict(h) for h in self.vec_hits]
        return out

    def _other_search(self, *args, **kwargs):
        return []


def test_pure_helper_decides_fallback():
    assert _fallback_target(True, False, False) == ("vector_only", "全文路无命中")
    assert _fallback_target(False, True, False) == ("fulltext_only", "向量路无命中")
    assert _fallback_target(True, False, True) is None     # 有结果 → 不回退
    assert _fallback_target(False, False, False) is None   # 双路空 → 无路可退
    assert _fallback_target(True, True, False) is None     # 两个 flag 同给 = 调用方错误


def test_fulltext_only_empty_falls_back_to_vector_with_marker():
    eng = _StubEngine(ft_hits=[], vec_hits=VEC)
    out = eng.search("q", fulltext_only=True, with_meta=True)
    assert out["search_meta"]["mode"] == "fulltext_only"          # mode = 你请求的
    assert out["search_meta"]["fallback"] == {
        "to": "vector_only", "reason": "全文路无命中", "returned_after": 1,
    }
    assert out["search_meta"]["returned"] == 1
    assert out["results"][0]["guid"] == "vec1"


def test_vector_only_empty_falls_back_to_fulltext():
    eng = _StubEngine(ft_hits=FT, vec_hits=[])
    out = eng.search("q", vector_only=True, with_meta=True)
    assert out["search_meta"]["fallback"]["to"] == "fulltext_only"
    assert out["results"][0]["guid"] == "ft1"


def test_no_fallback_when_hit():
    eng = _StubEngine(ft_hits=FT, vec_hits=VEC)
    assert eng.search("q", fulltext_only=True, with_meta=True)["search_meta"]["fallback"] is None
    hy = eng.search("q", with_meta=True)
    assert hy["search_meta"] == {"mode": "hybrid", "returned": 2, "fallback": None}


def test_empty_hybrid_is_the_real_no_hit_signal():
    eng = _StubEngine(ft_hits=[], vec_hits=[])
    out = eng.search("q", with_meta=True)
    assert out["results"] == []
    assert out["search_meta"]["returned"] == 0
    assert out["search_meta"]["fallback"] is None   # 双路都跑了：null 才是"真没有"


def test_both_flags_set_stays_empty():
    eng = _StubEngine(ft_hits=FT, vec_hits=VEC)
    out = eng.search("q", fulltext_only=True, vector_only=True, with_meta=True)
    assert out["results"] == []
    assert out["search_meta"]["fallback"] is None


def test_with_meta_false_keeps_legacy_list_contract():
    eng = _StubEngine(ft_hits=FT, vec_hits=VEC)
    assert isinstance(eng.search("q", fulltext_only=True), list)
    assert isinstance(eng.search("q", fulltext_only=True, with_meta=True), dict)
