"""
文件名: mhi_tag.py
摘要: CLI: mhi-tag <guid> [OPTIONS] —— tag 增删查 + 全库体检（--audit）。
依赖: click, core.tag_manager
"""

import json
import sys

import click

from ..core.tag_manager import audit_tags, manage_tags


@click.command("mhi-tag")
@click.argument("guid", required=False)
@click.option("--add", default=None, help="添加 tag（英文逗号分割）")
@click.option("--remove", default=None, help="移除 tag")
@click.option("--list", "list_flag", is_flag=True, help="列出当前 tag")
@click.option(
    "--audit", "audit_flag", is_flag=True,
    help="tag 体检（只读）：空 tags / 哨兵落库 / CSV 破损 / 保留字与非法字符；"
         "发现异常时退出码 1",
)
@click.option(
    "--sample", default=20, show_default=True,
    help="体检每类问题最多列出的样本行数",
)
@click.option(
    "--category", default=None,
    help="类别 (book/chat/onenote)；--audit 默认 all（三表全扫）",
)
def main(guid, add, remove, list_flag, audit_flag, sample, category):
    if audit_flag:
        report = audit_tags(category or "all", sample_limit=sample)
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["healthy"]:
            detail = "，".join(
                f"{name}={count}" for name, count in report["counts"].items() if count
            )
            click.echo(f"⚠️  tag 体检未通过：{detail}（详见 findings）", err=True)
            sys.exit(1)
        return

    if not guid:
        raise click.UsageError("缺少 GUID（要全库体检请改用 --audit）")
    cat = category or "book"
    if list_flag:
        result = manage_tags(guid, cat)
    elif add:
        result = manage_tags(guid, cat, add=add)
    elif remove:
        result = manage_tags(guid, cat, remove=remove)
    else:
        result = manage_tags(guid, cat)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
