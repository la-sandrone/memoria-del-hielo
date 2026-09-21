"""
文件名: mhi_search.py
摘要: CLI: mhi-search <query> [选项] —— 双路检索（chunk 级）。
      默认输出 {"results": [...], "search_meta": {...}}；search_meta.fallback 非 null
      表示显式单路模式（--fulltext-only / --vector-only）无命中、已自动补另一路。
      --plain 输出裸数组（旧形状，给脚本用）。
"""

import json
import click
from ..core.search import search_documents


@click.command("mhi-search")
@click.argument("query")
@click.option("--category", default=None, help="限定类别 (book/chat/onenote)")
@click.option("--bucket", default=None, help="限定桶")
@click.option("--tags", default=None, help="限定 tag（OMNIA = 搜全部数据，与不传同义）")
@click.option("--limit", default=10, type=int, help="返回条数")
@click.option("--fulltext-only", is_flag=True, help="仅全文搜索（无命中会自动补向量路并标注）")
@click.option("--vector-only", is_flag=True, help="仅向量搜索（无命中会自动补全文路并标注）")
@click.option("--plain", is_flag=True, help="只输出结果数组（旧形状；默认输出含 search_meta 的对象）")
def main(query, category, bucket, tags, limit, fulltext_only, vector_only, plain):
    out = search_documents(
        query, category, bucket, tags, limit, fulltext_only, vector_only,
        with_meta=not plain,
    )
    click.echo(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
