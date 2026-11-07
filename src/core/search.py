"""
文件名: search.py
摘要: CJK 感知三路搜索引擎（v3: 垂直分表 + 分词 v2 + NL MODE）。
      · book 类别 → documents_book_chunk（元数据+content）
                     + documents_book_chunk_fts（content_tokenized + FULLTEXT）
                     + documents_book_chunk_vec（embedding + HNSW）
                     join 书表做书级聚合（同书多 chunk 命中 → 书信号增强，每书限流）
      · chat / onenote → 整档粒度（口径与 book 同源：v2 分词 + NL MODE）
      · 权重: CJK 向量0.8/全文0.2；非CJK 全文0.6/向量0.4
      · 全文路一律 NATURAL LANGUAGE MODE。BOOLEAN MODE 会把全部 bigram 用 AND
        串起来，长查询必然 0 命中（实测本项目真实查询 5 条里 4 条 0 命中）
      · 子串兜底（LIKE，权重 0.05）：**只在全文与向量都空时**才跑 —— 它救的是
        「分词漏掉的词」，代价是 content 全表扫（65 万行约 16 秒）
      · 降级不静默：某条路抛异常 → 进 search_meta.degraded；靠兜底救回 →
        search_meta.salvaged > 0。调用方必须能区分「库里没有」与「某路挂了」
      · tag 语义在 search_documents() 单点收敛：OMNIA（哨兵）/空串 = 不过滤；
        哨兵与真 tag 混用、SQL 保留字、结构性字符 → TagSyntaxError（见 core.tags）
依赖: core.config, core.db, core.embedding, core.tokenizer, core.tags
"""

from __future__ import annotations

import re
import statistics
import warnings
from typing import Any

from .config import get_config
from .db import get_db
from .embedding import get_embedding_service
from .tags import normalize_query_tags, split_tags
from .tokenizer import (
    build_cjk_query_raw,
    build_cjk_query_v2,
    has_cjk,
    iter_chunks,
    query_term_split,
)

# 搜索表配置（book 走 chunk 表专用路径，不在此列）
SEARCHABLE_TABLES = [
    {"table": "documents_chat",    "category": "chat",    "has_vector": True},
    {"table": "documents_onenote", "category": "onenote", "has_vector": False},
]

# 权重配置
FT_WEIGHT_CJK = 0.2
VEC_WEIGHT_CJK = 0.8
FT_WEIGHT_ASCII = 0.6
VEC_WEIGHT_ASCII = 0.4

# 子串兜底路（LIKE）：它没有相关性信号（命中=1、不命中=0），给高了会挤掉
# BM25/向量命中。职责只有一个 —— 救「分词漏掉的词」，因此**只在其它路全空时**
# 才跑（见 _book_search）。代价是 content 全表扫（654,315 行 / 约 2 GiB）。
LIKE_WEIGHT = 0.05
# 兜底路最多用几个查询词做 OR（每多一个词不增加扫描量，但会放宽命中面）
SALVAGE_MAX_TERMS = 3

# snippet 截取长度
SNIPPET_MAX_LEN = 200

# 书级聚合：同书每多命中一个 chunk 加一次分（封顶 4 次防止单书霸榜）
BOOK_HIT_BONUS = 0.05
BOOK_HIT_BONUS_CAP = 4

# 书级重排：cross-encoder 输入截断长度（每书取 best chunk 前 N 字符）
RERANK_MAX_CHARS = 800
# 候选 chunk 重排：每书按原分取 top-N 进 rerank（书内+书间双层精排）
RERANK_PER_BOOK = 6


def _fallback_target(
    fulltext_only: bool, vector_only: bool, got_results: bool
) -> tuple[str, str] | None:
    """显式单路模式且无命中时的回退目标：(目标模式, 人类可读原因)；不需要回退返回 None。

    为什么要有回退：`fulltext_only=True` 走的是 jieba 分词 + MATCH BOOLEAN MODE，
    对短术语（"像差"）与长术语串命中率很低——空手而归时调用方**无从区分**
    「库里没有」和「这条路打不中」，就是一次静默降级。故显式单路无命中时自动补另一路，
    并在 search_meta.fallback 里标注（不静默）。
    两个 flag 同时给属于调用方错误，不回退（保持"什么都不搜"的原语义）。
    """
    if got_results:
        return None
    if fulltext_only and not vector_only:
        return ("vector_only", "全文路无命中")
    if vector_only and not fulltext_only:
        return ("fulltext_only", "向量路无命中")
    return None


def _final_rank_key(item: dict[str, Any]) -> float:
    """最终排序键：优先 rerank 分，没有就退回融合分（都是取负排降序）。

    ⚠️ 必须显式处理 None，不能写 `item.get("score_rerank", item["score"])`：
    `dict.get(k, default)` 在**键存在且值为 None** 时返回 None，而 _book_search
    在 rerank 没跑成时会显式把 score_rerank 设成 None ⟹ `-None` 抛 TypeError
    ⟹ **rerank 服务一挂，整次检索从「降级」变成「崩溃」**。
    """
    rerank = item.get("score_rerank")
    return -(rerank if rerank is not None else item.get("score", 0.0))


def _get_snippet(text: str) -> str:
    text = " ".join(text.split())
    return text[:SNIPPET_MAX_LEN] if len(text) > SNIPPET_MAX_LEN else text


# ── 书级语义检索（"找书"）────────────────────────────────────
BOOK_FIND_NOTE = (
    "本工具按『书名+目录』的语义邻近度排书：只排序，不判断库内是否存在该题材。"
    "细节型查询（公式/数值/术语串）请改用 search_docs（chunk 级、内容级）；"
    "机器化的存在性判据需要在 chunk 级 + reranker 上单独标定——书级信号对细节查询不敏感。"
)


def _find_books_sql(bucket: str | None, tags: str | None) -> tuple[str, list[Any]]:
    """书级近邻查询的 SQL 构造（纯函数，便于无库单测）。

    形状约定：向量放进派生表解析一次（同 _book_vector，避免 21KB 文本逐行重解析）。
    刻意不带 LIMIT——signal 里的 gap/z 需要全库距离分布；书表规模为数百~数千，
    上万本时应改为分批或预排序。
    """
    sql = (
        "SELECT b.guid, b.key_metadata AS title, b.bucket, b.tags, "
        "  b.expected_chunks AS chunks, VEC_DISTANCE_COSINE(b.embedding, q.qv) AS _distance "
        "FROM documents_book b, (SELECT VEC_FromText(?) AS qv) q "
    )
    params: list[Any] = []
    where: list[str] = []
    if bucket:
        where.append("b.bucket = ?")
        params.append(bucket)
    if tags:
        elements = split_tags(tags)
        if elements:
            conds = " OR ".join(["FIND_IN_SET(?, b.tags)"] * len(elements))
            where.append(f"({conds})")
            params.extend(elements)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY _distance ASC"
    return sql, params


def _book_rows_to_result(query: str, rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    """把按距离升序的整表结果收敛成 {books, signal, note}（纯函数，便于无库单测）。

    score = 1 - cosine 距离（越大越贴近）；signal 只给数字，不下存在性结论。
    """
    dists = [float(r["_distance"]) for r in rows]
    n = max(0, min(int(limit), len(rows)))
    books = [
        {
            "guid": r["guid"],
            "title": r.get("title") or "",
            "bucket": r.get("bucket") or "",
            "tags": r.get("tags") or "",
            "chunks": int(r.get("chunks") or 0),
            "score": round(1.0 - float(r["_distance"]), 4),
        }
        for r in rows[:n]
    ]
    signal: dict[str, Any] = {"corpus_books": len(rows)}
    if dists:
        k = min(5, len(dists)) - 1
        sd = statistics.pstdev(dists) if len(dists) > 1 else 0.0
        signal.update(
            top1_distance=round(dists[0], 4),
            gap=round(dists[k] - dists[0], 4),
            z=round((statistics.mean(dists) - dists[0]) / sd, 2) if sd > 0 else 0.0,
        )
    return {"query": query, "books": books, "signal": signal, "note": BOOK_FIND_NOTE}


class SearchEngine:
    """双路搜索引擎。"""

    def __init__(self) -> None:
        self._db = get_db()
        self._embedding = get_embedding_service()
        cfg = get_config()
        self.max_per_book: int = int(cfg.get("chunking.max_per_book", 2))

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        category: str | None = None,
        bucket: str | None = None,
        tags: str | None = None,
        limit: int = 10,
        fulltext_only: bool = False,
        vector_only: bool = False,
        with_meta: bool = False,
        _allow_fallback: bool = True,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """双路检索。

        with_meta=True 时返回 `{"results": [...], "search_meta": {...}}`，
        `search_meta.fallback` 非 None 表示显式单路模式无命中、已自动补另一路
        （防"静默空手而归"：调用方需能区分「库里没有」与「该路打不中」）。
        """
        is_cjk = has_cjk(query)
        ft_w = FT_WEIGHT_CJK if is_cjk else FT_WEIGHT_ASCII
        vec_w = VEC_WEIGHT_CJK if is_cjk else VEC_WEIGHT_ASCII

        results: list[dict[str, Any]] = []
        # 本轮「哪条路挂了」（degraded）与「有没有靠子串兜底救回」（salvaged）
        # 都必须能让调用方看见 —— 否则「某路抛异常」和「库里没有」是同一个现象。
        degraded: list[dict[str, Any]] = []
        salvaged = 0
        retried_unpruned = False

        # 剪枝的可见性：把「留下了什么、剪掉了什么」交给调用方。
        # 剪枝率异常（剪掉的 ≥ 留下的）说明词表与语料脱节 —— 那是最激进也最危险的时刻。
        kept_terms, pruned_terms = query_term_split(query)
        if pruned_terms and len(pruned_terms) >= len(kept_terms):
            warnings.warn(
                f"停词剪枝比例过高：剪掉 {len(pruned_terms)} 个、保留 {len(kept_terms)} 个"
                f"（query={query[:40]!r}）—— 词表可能与语料脱节"
            )

        # ── book 路径（chunk 级 + 书级聚合）────────────────
        if category in (None, "book"):
            book_results, book_stats = self._book_search(
                query, bucket, tags, limit,
                fulltext_only, vector_only, ft_w, vec_w, degraded,
            )
            results.extend(book_results)
            salvaged = book_stats.get("salvaged", 0)
            retried_unpruned = book_stats.get("retried_unpruned", False)

        # ── chat / onenote 路径（整档粒度，逻辑不变）───────
        tables = [
            t for t in SEARCHABLE_TABLES
            if category is None or t["category"] == category
        ]
        if tables:
            results.extend(
                self._other_search(
                    tables, query, bucket, tags, limit,
                    fulltext_only, vector_only, ft_w, vec_w, degraded,
                )
            )

        # 最终排序：book 结果尊重 rerank 精排（score_rerank 跨书可比），
        # chat/onenote 及 rerank 降级场景回退原融合分
        results.sort(key=_final_rank_key)
        results = results[:limit]

        # 显式单路无命中 → 补另一路，并留下标注（不静默空手）
        fallback: dict[str, Any] | None = None
        target = _fallback_target(fulltext_only, vector_only, bool(results)) if _allow_fallback else None
        if target:
            alt_mode, reason = target
            alt = self.search(
                query, category, bucket, tags, limit,
                fulltext_only=(alt_mode == "fulltext_only"),
                vector_only=(alt_mode == "vector_only"),
                with_meta=False, _allow_fallback=False,
            )
            fallback = {"to": alt_mode, "reason": reason, "returned_after": len(alt)}
            results = alt

        if with_meta:
            mode = "fulltext_only" if fulltext_only else ("vector_only" if vector_only else "hybrid")
            return {
                "results": results,
                "search_meta": {
                    "mode": mode,
                    "returned": len(results),
                    "fallback": fallback,
                    # 非空 = 某条路抛异常了，结果可能是残的（别当「库里没有」）
                    "degraded": degraded,
                    # >0 = FULLTEXT 与向量都没打中，是 LIKE 子串兜底救回来的
                    "salvaged": salvaged,
                    # True = 剪枝后 0 命中，已用未剪词表重试过（词表可能太激进）
                    "retried_unpruned": retried_unpruned,
                    # 剪枝的可见性：kept 是实际用的词；pruned 是词表想丢的词。
                    # 若 len(pruned) >= len(kept)，说明词表几乎丢光了查询 —— 那条会打 WARNING。
                    "query_terms": {"kept": kept_terms, "pruned": pruned_terms},
                },
            }
        return results

    def find_books(
        self,
        query: str,
        bucket: str | None = None,
        tags: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """书级语义检索（"找书"）：书目卡片向量（书名 + 目录）的近邻排序。

        与 chunk 级 search() 并列：本方法回答"哪本书在主题上贴近这个查询"，
        **不**回答"库里有没有这个内容"（细节型查询请走 search_documents）。

        ⚠️ 刻意不做存在性判定——实测标定（104 条真实查询「题材确定在库」+ 10 条确定不在库）：
        top1 距离的 AUROC 0.859（排序够用），但**任何阈值都会大量误伤**——thr=0.49
        抓到 9/10 不在库，却把 28/104（27%）在库查询判成"没有"。根因：书级卡片只有
        书名+目录，对细节型/术语型查询天然不敏感。故只回 signal 数字，由调用方按查询意图判。
        """
        vec_text = "[" + ",".join(str(v) for v in self._embedding.embed_one(query)) + "]"
        sql, params = _find_books_sql(bucket, tags)
        rows = self._db.execute_sql(sql, [vec_text] + params)
        return _book_rows_to_result(query, rows, limit)

    # ------------------------------------------------------------------
    # book 路径（chunk 级）
    # ------------------------------------------------------------------

    def _book_search(
        self,
        query: str,
        bucket: str | None,
        tags: str | None,
        limit: int,
        fulltext_only: bool,
        vector_only: bool,
        ft_w: float,
        vec_w: float,
        degraded: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """book 路径：全文路 + 向量路 + 子串兜底。

        返回 (结果, stats)，stats = {salvaged: 兜底救回条数, retried_unpruned: 是否重试过}。
        """
        # 全文路（chunk 级）
        ft_rows: dict[int, dict[str, Any]] = {}
        retried_unpruned = False
        if not vector_only:
            for row in self._book_fulltext(query, bucket, tags, limit, degraded):
                ft_rows.setdefault(row["chunk_id"], row)
            # 第二层重试：剪枝后 0 命中 —— 剪枝可能正是漏召回的原因。
            # 剪枝只许削弱精度，绝不许影响「能不能捞到」，所以这里宁可带冗余词重打一次。
            if not ft_rows and query_term_split(query)[1]:
                retried_unpruned = True
                warnings.warn("停词剪枝后全文路 0 命中，已用未剪词表重试")
                for row in self._book_fulltext(
                    query, bucket, tags, limit, degraded, prune=False
                ):
                    ft_rows.setdefault(row["chunk_id"], row)

        # 向量路（chunk 级）
        vec_rows: dict[int, dict[str, Any]] = {}
        if not fulltext_only:
            try:
                query_vec = self._embedding.embed_one(query)
            except Exception as exc:
                warnings.warn(f"向量搜索失败，降级为纯全文: {exc}")
                degraded.append({"route": "embed", "reason": str(exc)[:200]})
                query_vec = None
            if query_vec is not None:
                for row in self._book_vector(query_vec, bucket, tags, limit, degraded):
                    vec_rows.setdefault(row["chunk_id"], row)

        # 合并（与整档路径同一套权重逻辑，key = chunk_id）
        merged: dict[int, dict[str, Any]] = {}
        for cid in set(ft_rows) | set(vec_rows):
            ft_row = ft_rows.get(cid)
            vec_row = vec_rows.get(cid)
            base = ft_row or vec_row or {}
            s_ft = ft_row["_score"] if ft_row else 0.0
            s_vec = vec_row["_distance"] if vec_row else 0.0
            sim_vec = max(0.0, 1.0 - s_vec) if s_vec > 0 else 0.0
            if s_ft > 0 and s_vec > 0:
                combined = ft_w * s_ft + vec_w * sim_vec
                source = "both"
            elif s_ft > 0:
                combined = s_ft
                source = "fulltext"
            else:
                combined = vec_w * sim_vec
                source = "vector"
            merged[cid] = self._chunk_row_to_result(
                base, s_ft, s_vec, combined, source
            )

        # 全部路都空 → 子串兜底（只在此刻付出 content 全表扫的代价）
        salvaged = 0
        if not merged:
            for row in self._book_like(query, bucket, tags, limit, degraded):
                merged.setdefault(
                    row["chunk_id"],
                    self._chunk_row_to_result(row, 0.0, 0.0, LIKE_WEIGHT, "like"),
                )
                salvaged += 1
            if salvaged:
                warnings.warn(f"全文与向量路均无命中，子串兜底救回 {salvaged} 条")

        # 书级聚合：同书多 chunk 命中 → 书信号增强
        books: dict[str, dict[str, Any]] = {}
        for item in merged.values():
            bg = item["guid"]
            info = books.setdefault(bg, {"items": [], "best": 0.0})
            info["items"].append(item)
            info["best"] = max(info["best"], item["score"])
        for info in books.values():
            n = len(info["items"])
            bonus = min(BOOK_HIT_BONUS * (n - 1), BOOK_HIT_BONUS * BOOK_HIT_BONUS_CAP)
            info["score"] = info["best"] + bonus
            info["items"].sort(key=lambda x: -x["score"])

        # 候选 chunk 级重排（cross-encoder）：书间 + 书内双层精排；
        # 失败降级聚合分 + 原排序（显式警告，不静默）
        rerank_ok = self._rerank_chunks(query, books)
        if rerank_ok:
            for bg, info in books.items():
                n = len(info["items"])
                bonus = min(
                    BOOK_HIT_BONUS * (n - 1), BOOK_HIT_BONUS * BOOK_HIT_BONUS_CAP
                )
                best_r = max(
                    (it.get("_rerank_score", 0.0) for it in info["items"]),
                    default=0.0,
                )
                info["score"] = best_r + bonus
                info["items"].sort(
                    key=lambda x: -x.get("_rerank_score", x["score"])
                )

        # 输出：书按重排后分数排序；rerank 成功时每书只出候选内 top-N
        output: list[dict[str, Any]] = []
        for bg in sorted(books, key=lambda b: -books[b]["score"]):
            info = books[bg]
            ranked = (
                [it for it in info["items"] if "_rerank_score" in it]
                if rerank_ok
                else info["items"]
            )
            for item in ranked[: self.max_per_book]:
                item["book_hit_count"] = len(info["items"])
                item["book_score"] = round(info["score"], 4)
                item["score_rerank"] = item.get("_rerank_score")
                output.append(item)
        return output, {"salvaged": salvaged, "retried_unpruned": retried_unpruned}

    def _rerank_chunks(
        self, query: str, books: dict[str, dict[str, Any]]
    ) -> bool:
        """候选 chunk 级 cross-encoder 重排（书间 + 书内精排）。

        每书按原分取 top RERANK_PER_BOOK 进 rerank，成功后 items 打
        `_rerank_score`。候选总数 ≤1 无需重排；服务异常时降级
        （返回 False，调用方保留聚合分 + 原排序）。
        """
        candidates: list[tuple[str, dict[str, Any]]] = []
        for bg, info in books.items():
            for item in info["items"][:RERANK_PER_BOOK]:
                candidates.append((bg, item))
        if len(candidates) <= 1:
            return False
        docs = [item["content"][:RERANK_MAX_CHARS] for _, item in candidates]
        try:
            scores = self._embedding.rerank(query, docs)
        except Exception as exc:
            # 可接受的局部降级：排序退化为向量/全文融合分，检索仍可用
            warnings.warn(f"chunk 级重排失败，降级为聚合分: {exc}")
            return False
        for (_, item), s in zip(candidates, scores):
            item["_rerank_score"] = s
        return True

    def _book_fulltext(
        self,
        query: str,
        bucket: str | None,
        tags: str | None,
        limit: int,
        degraded: list[dict[str, Any]],
        prune: bool = True,
    ) -> list[dict[str, Any]]:
        r"""书桶全文搜索（chunk 级）：侧表 + NATURAL LANGUAGE MODE。

        口径（2026-11-07 改）：
          · 查询串一律 build_cjk_query_v2 —— 汉字只给 bigram、非汉字给整词、
            过停词表；**不带 `+` / `*` 操作符**（那是 BOOLEAN 语法）。
          · 走 NATURAL LANGUAGE MODE。BOOLEAN MODE 把所有 bigram 用 AND 串起来，
            要求**同一条 chunk 同时含全部分词** —— 长查询必然落空。本项目真实
            查询实测（docs/真实检索查询分布案例.md 那 190 条的形态）：
              「卡尔萨根 魔鬼出没的世界 科学 迷信 伪科学」  BOOLEAN 0 命中
                                                            NL 41,387 命中，top-1 对
              「克尔度规 零测地线 Carter 常数 分离变量」      BOOLEAN 0 命中
                                                            NL 17,549 命中，top-1 对
              「helium 3D excitation electron impact cross section」
                                                            BOOLEAN 0 命中，NL 14,260
            5 条里 4 条 0 命中 —— 「快」是因为它什么都没查。
          · CJK 与非 CJK 都打 content_tokenized：它是 content 的投影，拉丁词在
            里面是整词（tokenize_cjk_ub_v2 的 \w+ 分支），所以 content 上的
            那个 FULLTEXT 索引已无存在理由 → 迁移时退役（省约 4.6 GB）。
        """
        # prune=True ：正常口径（剪掉纯虚词的 bigram）
        # prune=False：**第二层重试**——剪枝可能是 0 命中的原因，此时宁可多带冗余词
        #              也不能漏召回（拿回召回换一点精度，这笔账永远划算）
        tokens = build_cjk_query_v2(query) if prune else build_cjk_query_raw(query)
        if not tokens.strip():
            return []

        sql = (
            f"SELECT c.chunk_id, c.book_guid, c.chunk_seq, c.heading_path, "
            f"  IF(c.has_pua, SUBSTRING(b.content, c.char_start + 1, c.char_len), c.content) AS content, "
            f"  MATCH(f.content_tokenized) AGAINST(? IN NATURAL LANGUAGE MODE) AS _score, "
            f"  b.key_metadata AS book_title, b.bucket, b.tags, "
            f"  b.source_path AS book_path "
            f"FROM documents_book_chunk_fts f "
            f"JOIN documents_book_chunk c ON c.chunk_id = f.chunk_id "
            f"JOIN documents_book b ON b.guid = c.book_guid "
            f"WHERE MATCH(f.content_tokenized) AGAINST(? IN NATURAL LANGUAGE MODE)"
        )
        params: list[Any] = [tokens, tokens]

        if bucket:
            sql += " AND b.bucket = ?"
            params.append(bucket)
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            if tag_list:
                conds = " OR ".join(["FIND_IN_SET(?, b.tags)" for _ in tag_list])
                sql += f" AND ({conds})"
                params.extend(tag_list)

        sql += " ORDER BY _score DESC LIMIT ?"
        params.append(limit * 3)

        try:
            return self._db.execute_sql(sql, params)
        except Exception as exc:
            # 不静默：既要警告（日志/CLI 可见），也要进 degraded（MCP 调用方可见）。
            # 「某路挂了」与「库里没有」必须能被区分开。
            warnings.warn(f"书桶全文搜索失败: {exc}")
            degraded.append({"route": "fulltext", "reason": str(exc)[:200]})
            return []

    def _book_vector(
        self,
        query_vec: list[float],
        bucket: str | None,
        tags: str | None,
        limit: int,
        degraded: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """书桶向量搜索（chunk 级，向量已搬到侧表 documents_book_chunk_vec）。

        两个必须保留的形状约束（改这条 SQL 前先跑向量基准复测）：
        1. 查询向量放进派生表解析一次：`(SELECT VEC_FromText(?) AS qv) q`。
           写成 `VEC_DISTANCE_COSINE(c.embedding, VEC_FromText(?))` 时它是逐行求值的
           表达式——21KB 向量文本会被重新解析 16 万次（实测 0.206ms/行 ≈ 33s）。
        2. 排序与 payload 分两段（内层派生表只排 chunk_id+distance，外层按 id 回填
           content 等列）。把 content（TEXT）放进排序的 SELECT 列表会强制临时表落盘
           （实测 6.06s vs 0.39s，13×）。
        JOIN 会让向量索引失效、走精确扫描——但精确扫描在 8G 缓冲池下已是 0.4s，
        且结果是精确 KNN（无近似损失、无候选数上限），故不需要 ANN/两阶段方案。

        分表后（2026-11-07）：向量落在 documents_book_chunk_vec，侧表一行只有
        (chunk_id, embedding) = 用户口径的 4 KB，而原先它躺在行宽约 10.6 KB 的
        主表里（content 3.3KB + tokenized 3.2KB + 向量 4KB，一个 16KB 页只装 ~1.5 行）。
        精确扫描要读的字节数因此降到约 1/3，且缓冲池不再被无关列挤占。
        形状不变（仍带 JOIN → 仍走精确扫描），所以这是**纯粹的变快**，不引入
        近似召回损失。
        """
        # **⚠️ 这是近似检索（ANN），结果不保证精确**
        #
        # MariaDB 的 `VECTOR KEY` 是 HNSW —— 近似最近邻，recall 可以低于 100%，
        # 而且它不会告诉你漏了什么。2026-10-25 在 memoria 的 documents_book_chunk
        # （62.5 万点）上用自查询实测：
        #
        #     自查询 top-1 命中自己： 12/13 = 92%
        #     自己在 top-10 里：      13/13 = 100%
        #     （mhnsw_ef_search=20；调到 400 无任何变化，该参数在 12.3.2 上未见效）
        #
        # 排除过程：IGNORE INDEX 暴力扫描 → 精确正确；Python 手算余弦 → 0.000000；
        # EXPLAIN → 确实走 idx_vec_chunk。所以数据与距离函数都对，是索引给了近似答案。
        #
        # 两条纪律：
        #   1. 可以用它做语义召回（top-k 足够，实测 top-10 100%）——本函数正是此用途；
        #   2. 绝不要用它做精确查找（自查询/判重/找重复/验证是否已入库），那会撞上那 8%。
        #      精确查找走 FULLTEXT 或 LIKE。
        #
        # 表越小 recall 越高（图小、连通性好），但不要假设小表就是 100%。
        # 另注：VEC_DISTANCE（不带 _COSINE）会报 index is not found；建索引时
        # DISTANCE=cosine 必须显式写，否则静默降级为 mhnsw_default_distance（euclidean）。

        vec_text = "[" + ",".join(str(v) for v in query_vec) + "]"
        sql = (
            f"SELECT c.chunk_id, c.book_guid, c.chunk_seq, c.heading_path, "
            f"  IF(c.has_pua, SUBSTRING(b.content, c.char_start + 1, c.char_len), c.content) AS content, "
            f"  t._distance, b.key_metadata AS book_title, b.bucket, b.tags, "
            f"  b.source_path AS book_path "
            f"FROM (SELECT v.chunk_id AS cid, VEC_DISTANCE_COSINE(v.embedding, q.qv) AS _distance "
            f"        FROM documents_book_chunk_vec v "
            f"        JOIN documents_book_chunk c2 ON c2.chunk_id = v.chunk_id "
            f"        JOIN documents_book b2 ON b2.guid = c2.book_guid, "
            f"             (SELECT VEC_FromText(?) AS qv) q "
        )
        params: list[Any] = [vec_text]

        where: list[str] = []
        if bucket:
            where.append("b2.bucket = ?")
            params.append(bucket)
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            if tag_list:
                conds = " OR ".join(["FIND_IN_SET(?, b2.tags)" for _ in tag_list])
                where.append(f"({conds})")
                params.extend(tag_list)
        if where:
            sql += " WHERE " + " AND ".join(where)

        sql += (
            f" ORDER BY _distance ASC LIMIT ?) t "
            f"JOIN documents_book_chunk c ON c.chunk_id = t.cid "
            f"JOIN documents_book b ON b.guid = c.book_guid "
            f"ORDER BY t._distance ASC"
        )
        params.append(limit * 3)

        try:
            return self._db.execute_sql(sql, params)
        except Exception as exc:
            warnings.warn(f"书桶向量搜索失败: {exc}")
            degraded.append({"route": "vector", "reason": str(exc)[:200]})
            return []

    def _book_like(
        self,
        query: str,
        bucket: str | None,
        tags: str | None,
        limit: int,
        degraded: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """子串兜底（LIKE）：**只在其它路全空时**才跑。

        为什么要它：分词漏掉的词，FULLTEXT 与向量都救不回来。典型是「拉丁紧邻
        汉字」的串（v1 把它们整段吞进一个 token，抽样 19.6% 的行受影响）。迁移会
        修掉存量，但新型混合串还会有 —— 兜底路是兜底，不是主路。

        为什么只在全空时跑：`LIKE '%词%'` 在 content 上用不了索引，是全表扫
        （654,315 行 / 约 2 GiB）。常态查询付不起这个代价；空手而归时才值得付。

        为什么权重只有 LIKE_WEIGHT=0.05：布尔子串**没有相关性信号**（命中=1、
        不命中=0）。给高了会把 BM25/向量的好结果挤下去 —— melquiades 那场实测过，
        LIKE 以 score=1.0 参与排序时会抢占 BM25/向量命中。

        为什么按 LENGTH(content) 排序：子串命中没有相关性可言，但「更短的 chunk」
        是唯一廉价的焦点代理。按 guid / chunk_id 排序等于随机（会把无关长段落排前面）。
        """
        terms = self._salvage_terms(query)
        if not terms:
            return []
        like_sql = " OR ".join(["c.content LIKE ?" for _ in terms])
        params: list[Any] = [f"%{t}%" for t in terms]

        sql = (
            f"SELECT c.chunk_id, c.book_guid, c.chunk_seq, c.heading_path, "
            f"  IF(c.has_pua, SUBSTRING(b.content, c.char_start + 1, c.char_len), c.content) AS content, "
            f"  b.key_metadata AS book_title, b.bucket, b.tags, b.source_path AS book_path "
            f"FROM documents_book_chunk c "
            f"JOIN documents_book b ON b.guid = c.book_guid "
            f"WHERE ({like_sql})"
        )
        if bucket:
            sql += " AND b.bucket = ?"
            params.append(bucket)
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            if tag_list:
                conds = " OR ".join(["FIND_IN_SET(?, b.tags)" for _ in tag_list])
                sql += f" AND ({conds})"
                params.extend(tag_list)
        sql += " ORDER BY LENGTH(c.content) ASC, c.chunk_id ASC LIMIT ?"
        params.append(limit * 3)

        try:
            return self._db.execute_sql(sql, params)
        except Exception as exc:
            warnings.warn(f"子串兜底搜索失败: {exc}")
            degraded.append({"route": "like", "reason": str(exc)[:200]})
            return []

    @staticmethod
    def _salvage_terms(query: str) -> list[str]:
        """挑做子串匹配的词：最长的 CJK 串 / ≥3 字符的拉丁词，最多 SALVAGE_MAX_TERMS 个。

        单字做 LIKE 会命中半个库。拉丁侧跨语种子串本来就无意义（用户口径：
        「跨语种 LIKE 显然没办法」），故只保留长度 ≥3 的词。
        """
        cjk: list[str] = []
        latin: list[str] = []
        for is_cjk, seg in iter_chunks(query):
            if is_cjk:
                if len(seg) >= 2:
                    cjk.append(seg)
            else:
                latin.extend(re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}", seg))
        return sorted(set(cjk + latin), key=len, reverse=True)[:SALVAGE_MAX_TERMS]

    @staticmethod
    def _chunk_row_to_result(
        row: dict[str, Any],
        s_ft: float,
        s_vec: float,
        combined: float,
        source: str,
    ) -> dict[str, Any]:
        return {
            "guid": row.get("book_guid", ""),
            "chunk_id": row.get("chunk_id"),
            "chunk_seq": row.get("chunk_seq"),
            "category": "book",
            "bucket": row.get("bucket", ""),
            "tags": row.get("tags", ""),
            "book_title": row.get("book_title", ""),
            "key_metadata": row.get("book_title", ""),
            "source_path": row.get("book_path", ""),
            "heading_path": row.get("heading_path", ""),
            "content": row.get("content", ""),
            "snippet": _get_snippet(row.get("content", "")),
            "score_ft": round(s_ft, 4),
            "score_vec": round(s_vec, 4),
            "score": round(combined, 4),
            "source": source,
        }

    # ------------------------------------------------------------------
    # chat / onenote 路径（整档粒度，逻辑不变）
    # ------------------------------------------------------------------

    def _other_search(
        self,
        tables: list[dict[str, Any]],
        query: str,
        bucket: str | None,
        tags: str | None,
        limit: int,
        fulltext_only: bool,
        vector_only: bool,
        ft_w: float,
        vec_w: float,
        degraded: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        ft_results: dict[str, dict[str, Any]] = {}
        vec_results: dict[str, dict[str, Any]] = {}

        # 全文搜索
        if not vector_only:
            for tbl in tables:
                rows = self._fulltext_search(tbl, query, bucket, tags, limit, degraded)
                for row in rows:
                    g = row["guid"]
                    if g not in ft_results:
                        ft_results[g] = self._row_to_result(row, tbl["category"])
                    ft_results[g]["score_ft"] = row.get("_score", 0.0)

        # 向量搜索
        if not fulltext_only:
            vec_tables = [t for t in tables if t["has_vector"]]
            if vec_tables:
                try:
                    query_vec = self._embedding.embed_one(query)
                except Exception as exc:
                    warnings.warn(f"向量搜索失败，降级为纯全文: {exc}")
                    degraded.append({"route": "embed", "reason": str(exc)[:200]})
                    vec_tables = []
                for tbl in vec_tables:
                    rows = self._vector_search(tbl, query_vec, bucket, tags, limit, degraded)
                    for row in rows:
                        g = row["guid"]
                        if g not in vec_results:
                            vec_results[g] = self._row_to_result(row, tbl["category"])
                        vec_results[g]["score_vec"] = row.get("_distance", 0.0)

        # 合并
        merged: dict[str, dict[str, Any]] = {}
        for guid in set(ft_results) | set(vec_results):
            entry = ft_results.get(guid, vec_results.get(guid, {}))
            s_ft = entry.get("score_ft", 0.0)
            s_vec = entry.get("score_vec", 0.0)
            sim_vec = max(0.0, 1.0 - s_vec) if s_vec > 0 else 0.0
            if s_ft > 0 and s_vec > 0:
                combined = ft_w * s_ft + vec_w * sim_vec
                source = "both"
            elif s_ft > 0:
                combined = s_ft
                source = "fulltext"
            else:
                combined = vec_w * sim_vec
                source = "vector"
            merged[guid] = {
                **entry,
                "score_ft": round(s_ft, 4),
                "score_vec": round(s_vec, 4),
                "score": round(combined, 4),
                "source": source,
            }
        return list(merged.values())

    @staticmethod
    def _row_to_result(row: dict[str, Any], category: str) -> dict[str, Any]:
        return {
            "guid": row.get("guid", ""),
            "bucket": row.get("bucket", ""),
            "category": category,
            "key_metadata": row.get("key_metadata", ""),
            "tags": row.get("tags", ""),
            "snippet": _get_snippet(row.get("content", "")),
            "score_ft": 0.0,
            "score_vec": 0.0,
            "score": 0.0,
            "source": "",
        }

    # ── 整档全文/向量搜索（chat/onenote，原逻辑不变）────────

    def _fulltext_search(
        self,
        tbl: dict[str, Any],
        query: str,
        bucket: str | None,
        tags: str | None,
        limit: int,
        degraded: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        # 与 book 路径同一口径：build_cjk_query_v2 + NATURAL LANGUAGE MODE。
        # （chat/onenote 目前 0 行，但口径必须同源，否则一启用就是「搜不到」。）
        tokens = build_cjk_query_v2(query)
        if not tokens.strip():
            return []

        table = tbl["table"]
        sql = (
            f"SELECT guid, bucket, tags, key_metadata, content, "
            f"  MATCH(content_tokenized) AGAINST(? IN NATURAL LANGUAGE MODE) AS _score "
            f"FROM {table} "
            f"WHERE MATCH(content_tokenized) AGAINST(? IN NATURAL LANGUAGE MODE)"
        )
        params: list[Any] = [tokens, tokens]

        if bucket:
            sql += " AND bucket = ?"
            params.append(bucket)
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            if tag_list:
                conditions = " OR ".join(["FIND_IN_SET(?, tags)" for _ in tag_list])
                sql += f" AND ({conditions})"
                params.extend(tag_list)

        sql += " ORDER BY _score DESC LIMIT ?"
        params.append(limit * 2)

        try:
            return self._db.execute_sql(sql, params)
        except Exception as exc:
            warnings.warn(f"全文搜索失败 [{table}]: {exc}")
            degraded.append({"route": f"fulltext:{table}", "reason": str(exc)[:200]})
            return []

    def _vector_search(
        self,
        tbl: dict[str, Any],
        query_vec: list[float],
        bucket: str | None,
        tags: str | None,
        limit: int,
        degraded: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        # **⚠️ 这是近似检索（ANN），结果不保证精确**
        #
        # MariaDB 的 VECTOR KEY 是 HNSW —— 近似最近邻，recall 可以低于 100%，
        # 而且它不会告诉你漏了什么。2026-10-25 在 documents_book_chunk（62.5 万点）
        # 上用自查询实测：top-1 命中自己 12/13 = 92%，自己在 top-10 里 13/13 = 100%。
        # （mhnsw_ef_search=20；调到 400 无任何变化，该参数在 12.3.2 上未见效。）
        #
        # 排除过程：IGNORE INDEX 暴力扫描 → 精确正确；Python 手算余弦 → 0.000000；
        # EXPLAIN → 确实走 idx_vec_chunk。所以数据与距离函数都对，是索引给了近似答案。
        #
        # 两条纪律：
        #   1. 可以用它做语义召回（top-k 足够，实测 top-10 100%）——本函数正是此用途；
        #   2. 绝不要用它做精确查找（自查询/判重/找重复/验证是否已入库），会撞上那 8%。
        #      精确查找走 FULLTEXT 或 LIKE。
        #
        # 表越小 recall 越高（图小、连通性好），但不要假设小表就是 100%。
        table = tbl["table"]

        vec_text = "[" + ",".join(str(v) for v in query_vec) + "]"

        # 同 _book_vector：向量进派生表解析一次；排序只带 guid+distance，
        # payload（key_metadata/content 等 TEXT 列）留到外层按 guid 回填
        sql = (
            f"SELECT t.g AS guid, c.bucket, c.tags, c.key_metadata, c.content, t._distance "
            f"FROM (SELECT c2.guid AS g, VEC_DISTANCE_COSINE(c2.embedding, q.qv) AS _distance "
            f"        FROM {table} c2, (SELECT VEC_FromText(?) AS qv) q "
        )
        params: list[Any] = [vec_text]

        where: list[str] = []
        if bucket:
            where.append("c2.bucket = ?")
            params.append(bucket)
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            if tag_list:
                conds = " OR ".join(["FIND_IN_SET(?, c2.tags)" for _ in tag_list])
                where.append(f"({conds})")
                params.extend(tag_list)

        if where:
            sql += " WHERE " + " AND ".join(where)

        sql += (
            f" ORDER BY _distance ASC LIMIT ?) t "
            f"JOIN {table} c ON c.guid = t.g "
            f"ORDER BY t._distance ASC"
        )
        params.append(limit * 2)

        try:
            return self._db.execute_sql(sql, params)
        except Exception as exc:
            warnings.warn(f"向量搜索失败 [{table}]: {exc}")
            degraded.append({"route": f"vector:{table}", "reason": str(exc)[:200]})
            return []


# ── 全局单例 ──────────────────────────────────────────────

_engine: SearchEngine | None = None


def get_search_engine() -> SearchEngine:
    global _engine
    if _engine is None:
        _engine = SearchEngine()
    return _engine


def search_documents(
    query: str,
    category: str | None = None,
    bucket: str | None = None,
    tags: str | None = None,
    limit: int = 10,
    fulltext_only: bool = False,
    vector_only: bool = False,
    with_meta: bool = False,
) -> list[dict[str, Any]] | dict[str, Any]:
    # 默认 tag fallback：显式 tags（含空串=不过滤）> 配置文件 [search].default_tags
    if tags is None:
        tags = get_config().get("search.default_tags") or None
    # tag 语义规范化（唯一收敛点，CLI 与 MCP 都走这里）：
    #   OMNIA（哨兵）/ 空串 → None（不生成 tag 谓词）；
    #   哨兵与真 tag 混用 / 保留字 / 结构性字符 → TagSyntaxError。
    tags = normalize_query_tags(tags)
    return get_search_engine().search(
        query, category, bucket, tags, limit, fulltext_only, vector_only,
        with_meta=with_meta,
    )


def find_books(
    query: str,
    bucket: str | None = None,
    tags: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """书级语义检索入口（CLI / MCP 共用）。

    tag 语义与 search_documents 完全同规则：显式 tags > 配置 [search].default_tags，
    经 normalize_query_tags 折叠（OMNIA / 空串 = 不过滤）。
    """
    if tags is None:
        tags = get_config().get("search.default_tags") or None
    tags = normalize_query_tags(tags)
    return get_search_engine().find_books(query, bucket, tags, limit)
