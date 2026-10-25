"""
文件名: mcp_server.py
摘要: MCP Server — mcp-memoria.del.hielo
      注册 7 个只读工具：检索（search_docs 找内容 / find_books 找书 / read_book_chunk 钻取）
      + 发现（list_buckets / list_tags / list_books）+ 会话（set_default_tags）。
      设计依据: MCP 工具面只暴露模型在对话中能正确触发的动作；
      导入/管理/清理等运维操作下沉 CLI（mhi-import-* / mhi-tag / mhi-clean-*），不在 MCP 面。
依赖: fastmcp
      core.* (config/search/db)
"""

from __future__ import annotations

import json
import logging
from collections import Counter

from fastmcp import FastMCP

from .core.config import load_config
from .core.search import find_books as find_books_impl
from .core.search import search_documents
from .core.db import get_db
from .core.tags import TAG_ALL, normalize_query_tags
from .core.text_errata import errata_payload, render_errata_note

logger = logging.getLogger(__name__)

# 初始化配置（启动时加载）
load_config()

mcp = FastMCP("mcp-memoria.del.hielo")

# 本场对话默认查找 tag（stdio 进程生命周期 = 客户端会话；None = 未设置，回退配置文件）
_session_tags: str | None = None

# ── 检索工具 ──────────────────────────────────────────────


@mcp.tool
def search_docs(
    query: str,
    category: str | None = None,
    bucket: str | None = None,
    tags: str | None = None,
    limit: int = 10,
    fulltext_only: bool = False,
    vector_only: bool = False,
) -> str:
    """搜索知识库内容（chunk 级双路：全文 + 向量）。典型耗时 0.3–1.5 秒，可直接调用。

    ⚠️ 文本可信度：本库正文来自 PDF 的 md 产出，**文本层不保证等于原文**（字体映射
    可以撒谎：生僻文种可能被读成看着正常的 ASCII；实测某本书 27% 的页面上有这种片段）。
    **引用前先看上下文是否说得通；不要逐字照抄生僻字/符号。**

    ⚠️ **向量路是近似检索（ANN / HNSW）**：62.5 万点实测自查询 top-1 命中率约 92%、
    top-10 约 100%，且 `mhnsw_ef_search` 调到 400 也未见效。**可用于语义召回，
    不适合「找这一条 / 判重 / 验证是否已入库」这类精确查找**——那会撞上漏检。
    精确匹配请用 `fulltext_only=True`。

    ⚠️ **关于俄文 / 希腊文 / 带变音拉丁文**：**非 CJK 查询走 `content` 列的 FULLTEXT
    （MariaDB 默认空格分词），不依赖 tokenized，正常工作**（实测俄文可命中）。
    `content_tokenized` 里这些文字为空，只影响「中文查询命中它们」这类混合场景——
    而那类场景本来就该走**向量路**（bge-m3 多语言共享语义空间，实测中文查询直接
    命中俄文书的对应章节）。
    **残留问题**：希腊文/拉丁文的**屈折变化**（`Πλάτων` vs `Πλάτωνος`）会让精确词匹配
    失败，需要词形还原（lemmatization）——那是另一件工程，与 tokenized 无关。

    book 类别返回 chunk 级结果（含 book_title / heading_path / chunk_seq /
    book_hit_count），命中后用 read_book_chunk 钻取原文；
    chat/onenote 返回整档结果。自动合并去重。

    **返回结构**：`{"results": [...], "search_meta": {"mode", "returned", "fallback"}}`
      · `search_meta.fallback` **非 null 表示发生了自动回退**：你传了 fulltext_only 或
        vector_only，而那一路没命中，系统自动补了另一路——形如
        `{"to": "vector_only", "reason": "全文路无命中", "returned_after": 12}`。
        看到它就知道：**不能据此判断"库里没有"**，只是原来的搜索方式打不中。
      · `results` 为空且 fallback 为 null → 双路都无命中（这才是"库里大概率没有"的信号）。

    找"书"（而不是找段落）请用 find_books；按 bucket/tag 列表请用 list_books。

    参数:
        query: 搜索关键词（多词并列即可，如「月球极区 水冰 永久阴影」）。
        category: 限定类别（book/chat/onenote，不指定则搜全部）。
        bucket: 限定桶（可用 list_buckets 查看）。
        tags: 限定 tag（可用 list_tags 查看）；OMNIA = 搜全部数据（与不传/空串同义）。
        limit: 返回条数（默认 10）。
        fulltext_only: 仅全文搜索（unigram+bigram + BOOLEAN MODE；中文单字/任意子串均可命中，
                       无命中时会自动补向量路并标注 fallback——细节型查询建议直接用默认双路）。
        vector_only: 仅向量搜索（无命中时会自动补全文路并标注 fallback）。
    """
    # 默认 tag fallback 链：显式 tags > 本场会话默认 > 配置文件默认（search.py 内部）
    if tags is None:
        tags = _session_tags
    out = search_documents(
        query, category, bucket, tags, limit, fulltext_only, vector_only,
        with_meta=True,
    )
    return json.dumps(out, ensure_ascii=False, indent=2)


@mcp.tool
def find_books(
    query: str,
    bucket: str | None = None,
    tags: str | None = None,
    limit: int = 10,
) -> str:
    """书级语义检索（"找书"）：返回主题上最贴近查询的书（按书目卡片 = 书名 + 目录 的向量距离排序）。

    ⚠️ **这是 HNSW 近似检索**（ANN）：排序可信但**不保证召回完整**，
    且它**只排序、不判断库内是否存在该题材**——阈值判定会大量误伤（见下）。

    ⚠️ 文本可信度：本库正文来自 PDF 的 md 产出，**文本层不保证等于原文**（字体映射
    可以撒谎：生僻文种可能被读成看着正常的 ASCII；实测某本书 27% 的页面上有这种片段）。
    **引用前先看上下文是否说得通；不要逐字照抄生僻字/符号。**

    三个检索/发现工具怎么选：
      · find_books  —— **主题型**：「库里有没有讲 X 的书」「先给我一批相关书目」（本工具）
      · search_docs —— 找**具体内容/段落**、要引用原文（chunk 级，正文；细节型查询如某公式/数值走这个）
      · list_books  —— 按 bucket/tag **枚举**清单（无语义）

    ⚠️ 本工具**只排序，不判断库内是否存在该题材**——请自己读 signal 数字与 books 内容再下结论。
    实测标定（104 条真实查询「题材确定在库」+ 10 条确定不在库）：「top1 距离」AUROC 0.859
    （排序够用），但**任何阈值都会大量误伤**——thr=0.49 抓到 9/10 不在库，却把 28/104（27%）
    在库查询判成"没有"。根因：书目卡片只有书名+目录，对细节型/术语型查询天然不敏感。
    （需要机器化的存在性判据时，应在 chunk 级 + reranker 上单独标定，不在本工具职责内。）

    参数:
        query: 主题描述，如「月球极区的水冰」「光学干涉仪的设计原理」。
        bucket: 限定桶（可选）。
        tags: 限定 tag（可选）；OMNIA = 搜全部数据（与不传同义）。
        limit: 返回书数（默认 10）。
    """
    if tags is None:
        tags = _session_tags
    return json.dumps(
        find_books_impl(query, bucket, tags, limit),
        ensure_ascii=False,
        indent=2,
    )


def _load_errata(prov: dict) -> dict:
    """把 documents_book.errata_stats 解成 dict。坏 JSON 不静默吞：返回 {} 并记日志。

    驱动多数情况下给回的是 JSON 字符串（列是 longtext 别名）。
    """
    raw = prov.get("errata_stats")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            got = json.loads(raw)
        except (ValueError, TypeError) as exc:
            logger.warning("errata_stats 解析失败（按未记录处理）: %s", exc)
            return {}
        return got if isinstance(got, dict) else {}
    return {}


def _text_trust_note(prov: dict) -> str:
    """按书生成一句「文本可信度」提示。

    依据：本库正文来自 PDF 的 md 产出，**文本层不保证等于原文**。
    数据源是 documents_book.errata_stats（一列 JSON）；**判据与词表都在
    core.text_errata**，本函数只负责把 DB 里的 JSON 解出来交给它渲染 ——
    渲染逻辑不在这里复制一份，否则两处措辞必然漂移。

    ⚠️ 空 `{}` 表示「未计算」，与「算了是 0」是两件事，消费方不要混。
    """
    return render_errata_note(
        _load_errata(prov), (prov.get("mineru_parse_mode") or "").strip()
    )


@mcp.tool
def read_book_chunk(
    book_guid: str,
    chunk_seq: int,
    context: int = 1,
) -> str:
    """读取书中一个 chunk 的全文（S2B 钻取：小粒度搜、大粒度读）。

    ⚠️ 文本可信度：本库正文来自 PDF 的 md 产出，**文本层不保证等于原文**（字体映射
    可以撒谎：生僻文种可能被读成看着正常的 ASCII；实测某本书 27% 的页面上有这种片段）。
    **引用前先看上下文是否说得通；不要逐字照抄生僻字/符号。**

    返回体里与可信度相关的三个字段：
      · `text_provenance` —— 这本书是哪个引擎/档位/读数方式产出的。
      · `text_errata`     —— 本书的**误差统计**（计数 + 分流说明）。
      · `text_trust`      —— 把上面两项渲染成给模型的提醒，**并附本次触发的每个
        信号键的释义**（键的自文档化随 payload 一起给，不在本 docstring 里穷举
        —— 误差种类是开放的，写死在这里必然与判据漂移）。
        空 `{}` 表示「未计算」，与「算了是 0」是两件事。

    search_docs 命中 book 结果后按需展开：返回该 chunk 及前后各
    context 个邻近 chunk 的全文，带章节路径。

    参数:
        book_guid: 书的 UUID（search_docs 结果的 guid 字段）。
        chunk_seq: chunk 序号（search_docs 结果的 chunk_seq 字段）。
        context: 前后各扩展几个 chunk（0-5，默认 1）。
    """
    context = max(0, min(context, 5))
    lo = max(1, chunk_seq - context)
    hi = chunk_seq + context
    db = get_db()
    rows = db.execute_sql(
        "SELECT c.chunk_seq, c.heading_path, c.char_start, "
        "       IF(c.has_pua, SUBSTRING(b.content, c.char_start + 1, c.char_len), c.content) AS content, "
        "       b.key_metadata AS book_title, "
        "       b.source_path AS book_path, "
        "       b.mineru_form, b.mineru_parse_mode, b.errata_stats "
        "FROM documents_book_chunk c "
        "JOIN documents_book b ON b.guid = c.book_guid "
        "WHERE c.book_guid = ? AND c.chunk_seq BETWEEN ? AND ? "
        "ORDER BY c.chunk_seq",
        (book_guid, lo, hi),
    )
    if not rows:
        return json.dumps(
            {"error": "chunk 未找到（检查 book_guid / chunk_seq）"},
            ensure_ascii=False,
        )
    # 溯源字段同一本书每行都一样：从首行取出提到顶层，避免逐 chunk 重复
    _PROV_KEYS = ("mineru_form", "mineru_parse_mode", "errata_stats")
    provenance = {k: rows[0].get(k) for k in _PROV_KEYS}
    for r in rows:
        for k in _PROV_KEYS:
            r.pop(k, None)

    errata = _load_errata(provenance)
    payload: dict = {
        "chunks": rows,
        "book_path": rows[0].get("book_path", ""),
        "text_provenance": {k: v for k, v in provenance.items()
                            if k != "errata_stats"},
    }
    errata_doc = errata_payload(
        errata, (provenance.get("mineru_parse_mode") or "").strip()
    )
    if errata_doc:
        payload["text_errata"] = errata_doc
    note = _text_trust_note(provenance)
    if note:
        payload["text_trust"] = note
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ── 会话工具 ──────────────────────────────────────────────


@mcp.tool
def set_default_tags(tags: str = "") -> str:
    """设置本场对话默认查找的 tag（逗号分割）。

    设置后 search_docs 不传 tags 时按此过滤（会话级，仅对当前连接生效，
    不写入配置文件）。传空串清除，回退配置文件 [search].default_tags。

    tag 语义（权威定义见 src/core/tags.py）：
      · OMNIA = 「搜全部数据」哨兵，与空串同义（都不生成 tag 谓词）；永不落库。
      · 哨兵与具体 tag 混用（如 "OMNIA,化学"）报错：ALL OR <tag> 恒等于 ALL，无意义。
      · 具体 tag 不得是 SQL 保留字，也不得含引号/分号/反斜杠/控制字符。

    参数:
        tags: 逗号分割的 tag 列表；空串 = 清除本场默认；OMNIA = 搜全部数据。
    """
    global _session_tags
    # OMNIA / 空串 → None（都表示"不做 tag 过滤"）；混用、保留字、结构性字符 → TagSyntaxError
    _session_tags = normalize_query_tags(tags)
    return json.dumps(
        {
            "session_default_tags": _session_tags,
            "message": "本场对话默认 tag 已设置（空串 = 清除，回退配置文件默认）",
            "hint": f"{TAG_ALL} = 搜全部数据（与空串同义，均不生成 tag 谓词）",
        },
        ensure_ascii=False,
        indent=2,
    )


# ── 发现工具（元数据枚举）────────────────────────────────


@mcp.tool
def list_buckets() -> str:
    """列出知识库的所有桶及其文档数。

    桶是类别级容器（图书/游戏剧情/AI对话/OneNote笔记…），
    检索时可用 search_docs 的 bucket 参数圈定范围。
    """
    db = get_db()
    out: list[dict] = []
    for table in ("documents_book", "documents_chat", "documents_onenote"):
        rows = db.execute_sql(
            f"SELECT bucket, COUNT(*) AS n FROM {table} GROUP BY bucket ORDER BY n DESC"
        )
        for r in rows:
            out.append(
                {
                    "bucket": r["bucket"],
                    "documents": r["n"],
                    "table": table,
                }
            )
    return json.dumps(out, ensure_ascii=False, indent=2)


@mcp.tool
def list_tags(bucket: str | None = None) -> str:
    """列出知识库的标签及出现次数（标签云，按次数降序）。

    标签是逗号分割的自由文本（如 "化学,地质学,解析素材3"），
    可用 search_docs 的 tags 参数圈定范围。

    参数:
        bucket: 限定桶（可选，如 "图书"）。
    """
    db = get_db()
    counter: Counter[str] = Counter()
    for table in ("documents_book", "documents_chat", "documents_onenote"):
        if bucket:
            rows = db.execute_sql(
                f"SELECT tags FROM {table} WHERE bucket = ?", (bucket,)
            )
        else:
            rows = db.execute_sql(f"SELECT tags FROM {table}")
        for r in rows:
            raw = r["tags"] or ""
            for t in raw.split(","):
                t = t.strip()
                if t:
                    counter[t] += 1
    return json.dumps(
        [{"tag": t, "count": n} for t, n in counter.most_common()],
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool
def list_books(
    bucket: str | None = None,
    tags: str | None = None,
    limit: int = 50,
) -> str:
    """列出图书清单（书名/guid/章节数/tags），可按桶与标签过滤。

    tags 为 LIKE 粗匹配（任一指定标签命中即入选）；OMNIA = 搜全部数据（与不传同义）。

    参数:
        bucket: 限定桶（可选）。
        tags: 限定标签，英文逗号分割（可选）；OMNIA = 搜全部。
        limit: 最大返回条数（默认 50，最大 200）。
    """
    limit = max(1, min(limit, 200))
    db = get_db()
    sql = (
        "SELECT b.guid, b.key_metadata AS title, b.bucket, b.tags, "
        "       (SELECT COUNT(*) FROM documents_book_chunk c "
        "         WHERE c.book_guid = b.guid) AS chunks "
        "FROM documents_book b"
    )
    conds: list[str] = []
    params: list[object] = []
    if bucket:
        conds.append("b.bucket = ?")
        params.append(bucket)
    # tag 语义规范化（core.tags 为唯一真相源）：OMNIA/空串 → None（不过滤）；混用/保留字/非法字符 → 报错
    tag_filter = normalize_query_tags(tags)
    if tag_filter:
        for t in tag_filter.split(","):
            conds.append("b.tags LIKE ?")
            params.append(f"%{t}%")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY b.created_at DESC LIMIT ?"
    params.append(limit)
    rows = db.execute_sql(sql, tuple(params))
    return json.dumps(rows, ensure_ascii=False, indent=2)


# ── 入口 ──────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
