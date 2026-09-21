"""
文件名: mhi_import_onenote.py
"""

import click
from pathlib import Path
from ..importers.onenote_importer import import_onenote_file, import_onenote_directory


@click.command("mhi-import-onenote")
@click.argument("path", type=click.Path(exists=True))
@click.option("--bucket", default=None, help="目标桶（默认: OneNote笔记）")
@click.option("--tags", default="", help="手动 tag")
@click.option("--auto-tags/--no-auto-tags", default=True, help="从子目录名自动 tag")
def main(path, bucket, tags, auto_tags):
    p = Path(path)
    if p.is_dir():
        results = import_onenote_directory(str(p), bucket, tags, auto_tags)
    else:
        results = [import_onenote_file(str(p), bucket, tags, auto_tags)]
    for r in results:
        status = r.get("status", "?")
        file = r.get("file", r.get("key_metadata", ""))
        guid = r.get("guid", "")
        if status == "ok":
            click.echo(f"✅  {file} → {guid}")
        else:
            click.echo(f"❌  {file}: {r.get('error', 'unknown')}")


if __name__ == "__main__":
    main()
