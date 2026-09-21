"""
文件名: mhi_reset_db.py
"""

import click
from pathlib import Path
from ..core.config import get_config


@click.command("mhi-reset-db")
@click.option("--yes", is_flag=True, help="确认删库重建")
def main(yes):
    if not yes:
        click.echo("⚠️  此操作将删除并重建整个数据库！")
        click.echo("使用 --yes 确认。")
        click.echo("注意：嵌入缓存目录不受影响。")
        return

    cfg = get_config()
    db_name = cfg.get("database.name", "memoria_del_hielo")
    sql_path = cfg.config_path.parent / "scripts" / "init_db.sql"

    if not sql_path.is_file():
        click.echo(f"❌  未找到 DDL 脚本: {sql_path}")
        return

    import mariadb
    conn = mariadb.connect(
        host=cfg.get("database.host"),
        port=cfg.get("database.port"),
        user=cfg.get("database.user"),
        password=cfg.database_password,
    )
    cursor = conn.cursor()

    try:
        cursor.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
        cursor.execute(f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci")
        cursor.execute(f"USE `{db_name}`")

        ddl = sql_path.read_text(encoding="utf-8")
        for statement in ddl.split(";"):
            stmt = statement.strip()
            if stmt:
                cursor.execute(stmt)
        conn.commit()
        click.echo(f"✅  数据库 {db_name} 已重建。")
    except Exception as exc:
        conn.rollback()
        click.echo(f"❌  重建失败: {exc}")
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()
