"""
文件名: tag_manager.py
摘要: Tag 增删改查 + 全库体检。直接操作各主表的 tags 字段（英文逗号分割）。
      写入前逐元素校验（哨兵 OMNIA / SQL 保留字 / 结构性字符）；
      audit_tags() 只读体检（空 tags / 哨兵落库 / CSV 破损 / 元素违规）——
      校验与体检规则都来自 core.tags，本模块只是它的数据层调用方。
依赖: core.db, core.tags
"""

from __future__ import annotations

from typing import Any

from .db import get_db
from .tags import audit_tag_element, is_tag_all, split_tags, validate_single_tag

TABLE_MAP = {
    "book": "documents_book",
    "chat": "documents_chat",
    "onenote": "documents_onenote",
}


class TagManager:
    """Tag 管理器。"""

    @staticmethod
    def list_tags(guid: str, category: str) -> list[str]:
        """列出指定文档的所有 tag。"""
        table = TABLE_MAP.get(category)
        if not table:
            raise ValueError(f"无效类别: {category}")

        rows = get_db().execute_sql(
            f"SELECT tags FROM {table} WHERE guid = ?", (guid,)
        )
        if not rows:
            return []
        tags_str = rows[0].get("tags", "")
        return [t.strip() for t in tags_str.split(",") if t.strip()]

    @staticmethod
    def add_tags(guid: str, category: str, new_tags: list[str]) -> list[str]:
        """添加 tag（不重复）。返回更新后的 tag 列表。"""
        current = TagManager.list_tags(guid, category)
        for t in new_tags:
            # 单值校验：上游已按逗号切分，故元素不该再含逗号（含则报错，不静默切碎）。
            # 哨兵 OMNIA / SQL 保留字 / 结构性字符同样在此拦截（core.tags 为唯一真相源）。
            t = validate_single_tag(t, "mhi tag --add")
            if t and t not in current:
                current.append(t)
        TagManager._update_tags(guid, category, current)
        return current

    @staticmethod
    def remove_tags(guid: str, category: str, remove: list[str]) -> list[str]:
        """移除 tag。返回更新后的 tag 列表。"""
        current = TagManager.list_tags(guid, category)
        remove_set = set(t.strip() for t in remove)
        updated = [t for t in current if t not in remove_set]
        TagManager._update_tags(guid, category, updated)
        return updated

    @staticmethod
    def _update_tags(guid: str, category: str, tags: list[str]) -> None:
        table = TABLE_MAP.get(category)
        if not table:
            raise ValueError(f"无效类别: {category}")
        tags_str = ",".join(tags)
        get_db().execute_sql(
            f"UPDATE {table} SET tags = ? WHERE guid = ?",
            (tags_str, guid),
        )


def manage_tags(
    guid: str,
    category: str,
    add: str | None = None,
    remove: str | None = None,
) -> dict[str, list[str]]:
    """CLI 级 tag 管理入口。"""
    mgr = TagManager()
    if add:
        result = mgr.add_tags(guid, category, add.split(","))
    elif remove:
        result = mgr.remove_tags(guid, category, remove.split(","))
    else:
        result = mgr.list_tags(guid, category)
    return {"guid": guid, "tags": result, "category": category}


# ----------------------------------------------------------------------
# 全库体检（只读）
# ----------------------------------------------------------------------

def audit_tags(category: str = "all", sample_limit: int = 20) -> dict[str, Any]:
    """tag 体检：空 tags / 哨兵落库 / CSV 结构破损 / 元素违规（保留字、结构性字符）。

    判据复用 core.tags（唯一真相源），本模块不另写一套规则。
    sample_limit = 每类问题最多列出的样本行数（大库不刷屏）。
    healthy=False 表示发现问题——CLI 以退出码 1 呈现，便于脚本/巡检接入。
    """
    tables = _tables_for(category)
    findings: dict[str, list[dict[str, Any]]] = {
        "empty": [],
        "broken_csv": [],
        "sentinel": [],
        "illegal": [],
    }
    counts = {key: 0 for key in findings}
    rows_total = 0
    single_tag_rows = 0
    distinct_elements: set[str] = set()

    for table in tables:
        rows = get_db().execute_sql(
            f"SELECT guid, bucket, key_metadata, tags FROM {table}"
        )
        rows_total += len(rows)
        for row in rows:
            tags = (row.get("tags") or "").strip()
            where = {
                "table": table,
                "guid": row.get("guid"),
                "title": (row.get("key_metadata") or "")[:60],
            }
            elements = split_tags(tags)
            if not elements:
                # 无任何 tag：tag 过滤搜不到它（OMNIA 全集可见）——漏打 tag 的唯一信号
                counts["empty"] += 1
                _sample(findings["empty"], {**where, "tags": tags}, sample_limit)
                continue
            if len(elements) == 1:
                single_tag_rows += 1
            if tags.startswith(",") or tags.endswith(",") or ",," in tags:
                counts["broken_csv"] += 1
                _sample(findings["broken_csv"], {**where, "tags": tags}, sample_limit)
            for element in elements:
                distinct_elements.add(element)
                if is_tag_all(element):
                    counts["sentinel"] += 1
                    _sample(
                        findings["sentinel"], {**where, "element": element}, sample_limit
                    )
                    continue
                reason = audit_tag_element(element)
                if reason:
                    counts["illegal"] += 1
                    _sample(
                        findings["illegal"],
                        {**where, "element": element, "reason": reason},
                        sample_limit,
                    )

    return {
        "category": category,
        "tables": tables,
        "stats": {
            "rows": rows_total,
            "distinct_elements": len(distinct_elements),
            "single_tag_rows": single_tag_rows,
        },
        "counts": counts,
        "healthy": not any(counts.values()),
        "sample_limit": sample_limit,
        "findings": findings,
    }


def _tables_for(category: str | None) -> list[str]:
    """类别 → 表名列表；all / None / 空串 = 三张主表全扫。"""
    if not category or category == "all":
        return list(TABLE_MAP.values())
    table = TABLE_MAP.get(category)
    if not table:
        raise ValueError(f"无效类别: {category}（可用 book/chat/onenote/all）")
    return [table]


def _sample(bucket: list[dict[str, Any]], item: dict[str, Any], limit: int) -> None:
    """样本收集：每类问题最多留 limit 行。"""
    if len(bucket) < limit:
        bucket.append(item)
