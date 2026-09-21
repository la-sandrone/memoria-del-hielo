"""
文件名: mhi_cleanup_images.py
"""

import click
from ..core.db import get_db


@click.command("mhi-cleanup-images")
@click.option("--yes", is_flag=True, help="确认删除")
def main(yes):
    db = get_db()
    orphans = db.execute_sql(
        "SELECT md5_hash, file_path, ref_count FROM images WHERE ref_count = 0"
    )
    if not orphans:
        click.echo("没有需要清理的图片。")
        return

    click.echo(f"发现 {len(orphans)} 个引用计数为 0 的图片：")
    for img in orphans:
        click.echo(f"  {img['md5_hash']}  {img['file_path']}")

    if not yes:
        click.echo("使用 --yes 确认删除。")
        return

    import os
    deleted = 0
    for img in orphans:
        path = img.get("file_path", "")
        try:
            if path and os.path.exists(path):
                os.remove(path)
                deleted += 1
        except OSError as exc:
            click.echo(f"  ⚠️  删除失败 {path}: {exc}")

    db.execute_sql("DELETE FROM images WHERE ref_count = 0")
    click.echo(f"已删除 {deleted} 个文件，清理 {len(orphans)} 条记录。")


if __name__ == "__main__":
    main()
