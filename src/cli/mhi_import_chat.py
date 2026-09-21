"""
文件名: mhi_import_chat.py
摘要: CLI: mhi-import-chat <path> [OPTIONS]
"""

import click
from pathlib import Path
from ..importers.chat_importer import import_chat_file, import_chat_directory


@click.command("mhi-import-chat")
@click.argument("path", type=click.Path(exists=True))
@click.option("--bucket", default=None, help="目标桶（默认: AI对话）")
@click.option("--tags", default="", help="手动 tag")
@click.option("--no-clean", is_flag=True, help="跳过清洗（调试用）")
@click.option("--summary", default=None, help="手动指定摘要")
def main(path, bucket, tags, no_clean, summary):
    p = Path(path)
    if p.is_dir():
        results = import_chat_directory(str(p), bucket, tags, no_clean, summary)
    else:
        results = [import_chat_file(str(p), bucket, tags, no_clean, summary)]
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
