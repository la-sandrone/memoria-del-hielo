#!/usr/bin/env python3
# 清理孤儿脚注：page_footnotes 中 book_guid 已不存在的记录。
# 成因：历史 force 重导只删 book 不删脚注（当时 page_footnotes 无 FK 约束）。
#       2026-09-21 起已有 fk_footnotes_book 级联，但历史遗留的孤儿仍可能残留。
# 用法: python scripts/purge_orphan_footnotes.py [--dry-run|--execute]
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.core.db import get_db


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    db = get_db()

    rows = db.execute_sql(
        """SELECT f.id, f.book_guid, f.page_idx, LEFT(f.footnote_text, 40) AS txt
           FROM page_footnotes f
           LEFT JOIN documents_book b ON b.guid = f.book_guid
           WHERE b.guid IS NULL
           ORDER BY f.id"""
    )
    print(f"孤儿脚注 {len(rows)} 条:")
    for r in rows[:20]:
        print(f"  #{r['id']}  book={r['book_guid'][:8]}  "
              f"p{r['page_idx']}  {r['txt']!r}")
    if len(rows) > 20:
        print(f"  ... 还有 {len(rows) - 20} 条")
    if not args.execute:
        print("[dry-run] 未删除。确认后加 --execute。")
        return 0

    ids = [r["id"] for r in rows]
    if not ids:
        return 0
    try:
        with db.transaction() as conn:
            with conn.cursor(dictionary=True) as cur:
                cur.execute(
                    "DELETE FROM page_footnotes WHERE id IN (%s)"
                    % ",".join("?" for _ in ids),
                    tuple(ids),
                )
                print(f"已删除 {cur.rowcount} 条孤儿脚注")
    except Exception:
        print("!! 事务失败已回滚", file=sys.stderr)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
