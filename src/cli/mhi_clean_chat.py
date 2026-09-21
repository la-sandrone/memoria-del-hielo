"""
文件名: mhi_clean_chat.py
"""

import click
from pathlib import Path
from ..cleaners.chat_cleaner import clean_chat_file


@click.command("mhi-clean-chat")
@click.argument("input", type=click.Path(exists=True))
@click.option("--output", default=None, help="输出路径（默认覆盖输入）")
@click.option("--summary", is_flag=True, help="额外输出摘要")
def main(input, output, summary):
    result = clean_chat_file(Path(input), Path(output) if output else None, summary)
    click.echo(f"清洗完成: {result['cleaned_length']} 字符")
    if summary and "summary" in result:
        click.echo(f"摘要: {result['summary']}")


if __name__ == "__main__":
    main()
