"""
文件名: mhi_cleanup_emb_cache.py
摘要: CLI: mhi-cleanup-emb-cache —— 手动清理嵌入缓存（MariaDB embedding_cache）。
      默认：仅清理过期条目（expires_at < NOW()）；
      --all：清空整表（所有向量重新计算）；
      --stats：只统计不删除。
依赖: click, core.db, core.embedding (cleanup_expired_cache)
"""

import click
from ..core.db import get_db


@click.command("mhi-cleanup-emb-cache")
@click.option("--all", "all_flag", is_flag=True, help="清空整张缓存表（谨慎）")
@click.option("--stats", is_flag=True, help="只统计，不删除")
def main(all_flag: bool, stats: bool):
    db = get_db()

    total = db.execute_sql("SELECT COUNT(*) AS n FROM embedding_cache")
    expired = db.execute_sql(
        "SELECT COUNT(*) AS n FROM embedding_cache "
        "WHERE expires_at IS NOT NULL AND expires_at < NOW()"
    )
    size = db.execute_sql(
        "SELECT ROUND(SUM(LENGTH(vector_json)) / 1024 / 1024, 1) AS mb "
        "FROM embedding_cache"
    )
    total_n = total[0]["n"]
    expired_n = expired[0]["n"]
    size_mb = size[0]["mb"] or 0.0
    click.echo(f"缓存条目: {total_n:,} | 已过期: {expired_n:,} | 数据量: {size_mb:.1f} MB")

    if stats:
        return

    if all_flag:
        if not click.confirm(f"确认清空全部 {total_n:,} 条嵌入缓存？"):
            return
        db.execute_sql("TRUNCATE TABLE embedding_cache")
        click.echo("已清空全部嵌入缓存。")
        return

    if expired_n == 0:
        click.echo("没有过期条目需要清理。")
        return

    db.execute_sql(
        "DELETE FROM embedding_cache "
        "WHERE expires_at IS NOT NULL AND expires_at < NOW()"
    )
    click.echo(f"已清理 {expired_n:,} 条过期缓存。")


if __name__ == "__main__":
    main()
