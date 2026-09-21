"""
文件名: mhi_remove_book.py
摘要: CLI: mhi-remove-book [<path>] [--guid G1,G2] [--yes] [--dry-run]
      删除已入库的书，两种【互斥】的选择模式：
        · 路径模式（PATH）：文件 = 精确匹配该书；目录 = 前缀匹配该目录下全部书；
          前缀匹配用 LOCATE（避开 LIKE 反斜杠转义坑）。
        · guid 模式（--guid）：逗号分割、亦可用重复选项给出；主键精确匹配
          （guid 列 collation utf8mb4_uca1400_ai_ci → 大小写不敏感）。
      两模式统一为「先解析成 guid 列表 → 再按 guid 删除」，删除逻辑单轨，
      避免 LOCATE / IN 各写一套导致的行为分叉。
      chunks 级联删除（FK fk_chunk_book）；page_footnotes 也已由 fk_footnotes_book 级联
      （2026-09-21 起活库已补齐该 FK，init_db.sql 里同样声明），但删除语句仍显式先删
      一遍 footnotes——不依赖级联行为，对未打 FK 的历史库同样正确；
      images 保留（全局去重表，重导时 MD5 命中复用）。
      guid 未命中：命中 0 个 → exit 1；部分命中 → 必须 --yes 才继续。
依赖: click, core.db, importers.book_importer(_to_windows_path)
"""

import sys

import click

from pathlib import Path

from ..core.db import get_db
from ..importers.book_importer import _to_windows_path


# 目标行列表的列定义集中一处——避免多处 SELECT 列顺序漂移
_TARGET_COLUMNS = (
    "b.guid, b.key_metadata, b.source_path, "
    "(SELECT COUNT(*) FROM documents_book_chunk c "
    " WHERE c.book_guid = b.guid) AS chunks "
)


def _parse_guids(values: tuple) -> list:
    """展开 --guid 取值 → 去重后的 guid 列表。

    两种写法可混用（GUID 无空格，无需引号）：
        --guid a,b,c
        --guid a --guid b --guid c
    逐项 strip、丢弃空串、保序去重（同一 guid 重复给出只删一次）。
    """
    result = []
    seen = set()
    for raw in values:
        for part in str(raw).split(","):
            g = part.strip()
            if g and g not in seen:
                seen.add(g)
                result.append(g)
    return result


def _select_by_guids(db, guids):
    """guid 模式：主键精确匹配（IN）。"""
    sql = (
        "SELECT " + _TARGET_COLUMNS +
        "FROM documents_book b WHERE b.guid IN (%s) "
        "ORDER BY b.key_metadata"
        % ",".join("?" for _ in guids)
    )
    return db.execute_sql(sql, tuple(guids))


def _select_by_path(db, key):
    """路径模式：LOCATE 前缀匹配（文件路径 = 目录路径 + 文件名）。"""
    sql = (
        "SELECT " + _TARGET_COLUMNS +
        "FROM documents_book b WHERE LOCATE(?, b.source_path) = 1 "
        "ORDER BY b.key_metadata"
    )
    return db.execute_sql(sql, (key,))


def _missing_guids(requested, found):
    """找出未命中的 guid（按 lower 比对——库列大小写不敏感，回报要与人一致）。"""
    found_lower = {r["guid"].lower() for r in found}
    return [g for g in requested if g.lower() not in found_lower]


def _build_delete_statements(guids):
    """构造删除语句（不执行）。返回 [(sql, params), ...]。

    顺序固定：先 page_footnotes（显式删，不依赖 fk_footnotes_book 的级联），后 documents_book
    （chunks 由 fk_chunk_book 级联）。破坏性测试只断言本函数的 SQL 文本，
    不真删数据；真正执行在 main() 的事务里。
    """
    placeholders = ",".join("?" for _ in guids)
    params = tuple(guids)
    return [
        (
            "DELETE FROM page_footnotes WHERE book_guid IN (%s)" % placeholders,
            params,
        ),
        (
            "DELETE FROM documents_book WHERE guid IN (%s)" % placeholders,
            params,
        ),
    ]


@click.command("mhi-remove-book")
@click.argument("path", required=False, type=click.Path(exists=True))
@click.option(
    "--guid",
    "guid_values",
    multiple=True,
    help="要删除的 guid，逗号分割；可重复给出（与 PATH 互斥）",
)
@click.option("--yes", is_flag=True, help="跳过确认直接删除")
@click.option("--dry-run", is_flag=True, help="只列出匹配的书，不删除")
def main(path, guid_values, yes, dry_run):
    """删除已入库的书。

    PATH 为文件 = 精确匹配该书；PATH 为目录 = 删除该目录下的全部书
    （前缀匹配）。或用 --guid 指定 guid（逗号分割，可重复）。

    两种模式互斥，必须给其一。破坏性操作，默认需要确认。
    """
    requested = _parse_guids(guid_values)
    if path is not None and requested:
        raise click.UsageError("PATH 与 --guid 互斥，只能给其一")
    if path is None and not requested:
        raise click.UsageError("必须给 PATH 或 --guid 之一")

    db = get_db()
    missing = []
    if requested:
        rows = _select_by_guids(db, requested)
        if not rows:
            click.echo(f"无匹配 guid：{', '.join(requested)}")
            sys.exit(1)
        missing = _missing_guids(requested, rows)
    else:
        p = Path(path)
        key = _to_windows_path(str(p))
        if p.is_dir():
            key = key.rstrip("\\") + "\\"  # 目录前缀匹配
        rows = _select_by_path(db, key)
        if not rows:
            click.echo(f"无匹配：{key}")
            sys.exit(0)

    total_chunks = sum(r["chunks"] for r in rows)
    click.echo(f"匹配 {len(rows)} 本（{total_chunks} chunks）：")
    for r in rows:
        click.echo(f"  - [{r['guid']}] {r['key_metadata']}（{r['chunks']} chunks）")
        click.echo(f"      {r['source_path']}")

    if dry_run:
        click.echo("（--dry-run：未删除）")
        sys.exit(0)

    if missing:
        click.echo(
            f"⚠️  {len(missing)} 个 guid 未命中（已删或拼错？）：{', '.join(missing)}"
        )
        if not yes:
            click.echo("请核对后重跑；如确认只删已命中的书，加 --yes 再执行。")
            sys.exit(1)

    if not yes and not click.confirm("确认删除？"):
        click.echo("已取消")
        sys.exit(0)

    targets = [r["guid"] for r in rows]  # 按库里真实 guid 删，而非用户输入
    with db.transaction() as conn:
        with conn.cursor() as cur:
            for sql, params in _build_delete_statements(targets):
                cur.execute(sql, params)
    click.echo(f"已删除 {len(rows)} 本（images 保留，重导时 MD5 复用）")


if __name__ == "__main__":
    main()
