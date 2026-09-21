"""
文件名: mhi_find_book.py
摘要: CLI: mhi-find-book <query> [--bucket] [--tags] [--limit] —— 书级语义检索（找书）。
      与 mhi-search 并列：本命令回答「哪本书在主题上贴近」，不回答「库里有没有这个内容」
      （细节型查询请用 mhi search）；也不判断库内是否存在该题材（只回 signal 数字）。
依赖: click, core.search
"""

import json
import click
from ..core.search import find_books


@click.command("mhi-find-book")
@click.argument("query")
@click.option("--bucket", default=None, help="限定桶")
@click.option("--tags", default=None, help="限定 tag（OMNIA = 搜全部数据，与不传同义）")
@click.option("--limit", default=10, type=int, show_default=True, help="返回书数")
def main(query, bucket, tags, limit):
    result = find_books(query, bucket, tags, limit)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
