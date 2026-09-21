"""
文件名: mhi_import_book.py
摘要: CLI: mhi-import-book <path> [OPTIONS]（chunk 层，幂等可断点续传）
      · 目录模式默认行为：根目录下存在一级子目录 → 每个子目录一次导入，
        tag = 子目录名（如 <corpus-root>\化学 → 每本书 tag 含"化学"）
      · --tags 本批手动 tag 与子目录 tag 叠加（"逻辑上把多个库串联为一个库"：
        如 --tags <corpus-root> → 每本书 tags = "<corpus-root>,化学"，来源与学科双圈定）
      · --no-dir-tag 禁止子目录自动 tag（整根目录一次导入，tag = 根目录名）
      · --exclude-dirs 排除名单（逗号分割，只作用于一级子目录分派；
        排除全部 → 自然回退单目录模式）
      · 胜利退出顺手清理过期嵌入缓存，并提示库内无任何 tag 的书——
        这是「漏打 tag」的显式探测器（见 core/tags.py / PROJECT_SPEC §4.6）
依赖: click, importers.book_importer, core.db
"""

import click
import os
import sys
from pathlib import Path
from ..core.db import get_db
from ..importers.book_importer import (
    import_book_file,
    import_book_directory,
    reset_interrupts,
    _INTERRUPTS,
)


@click.command("mhi-import-book")
@click.argument("path", type=click.Path(exists=True))
@click.option("--bucket", default=None, help="目标桶（默认: 图书）")
@click.option(
    "--tags", default="", help="本批手动 tag，英文逗号分割（与子目录 tag 叠加）"
)
@click.option(
    "--recursive",
    is_flag=True,
    help="单目录模式下递归处理子目录（子目录分派模式内部一律递归）",
)
@click.option(
    "--force", is_flag=True, help="重新导入已存在的书（旧记录级联删除）"
)
@click.option(
    "--no-dir-tag", is_flag=True, help="禁止一级子目录自动 tag（整根目录一次导入）"
)
@click.option(
    "--exclude-dirs", default="", help="排除的一级子目录名，英文逗号分割"
)
@click.option(
    "--parallel",
    type=int,
    default=2,
    help="并行导入的书数（默认 2；>2 有极大锁竞争风险；1 = 串行 + 阶段进度）",
)
def main(
    path: str,
    bucket: str | None,
    tags: str,
    recursive: bool,
    force: bool,
    no_dir_tag: bool,
    exclude_dirs: str,
    parallel: int,
):
    """导入图书/游戏剧情文件到知识库（chunk 层，幂等可断点续传）。

    Ctrl+C 两级语义：第一次中断当前书（该书未入库，继续下一本）；
    第二次停止全部（已导书保留）。
    """
    if parallel > 2:
        click.echo(
            click.style(
                "警告: --parallel=%d > 2——chunk 表两个 FULLTEXT 索引并发插入互斥，"
                "锁竞争极大（Lock wait timeout 风险），建议降到 2 或 1" % parallel,
                fg="yellow",
            ),
            err=True,
        )

    reset_interrupts()
    try:
        p = Path(path)
        if p.is_dir():
            results = _import_dir(
                p, bucket, tags, recursive, force, no_dir_tag, exclude_dirs, parallel
            )
        else:
            results = [import_book_file(str(p), bucket, tags, force=force)]
        _print_results(results)
        # 胜利退出（无异常）→ 顺手清理过期嵌入缓存；异常/Ctrl+C 不清理
        from ..core.embedding import cleanup_expired_cache

        cleared = cleanup_expired_cache()
        if cleared:
            click.echo(f"🧹 已清理 {cleared} 条过期嵌入缓存")
        _warn_untagged()
    except KeyboardInterrupt:
        # 到达此处 = import_directory 已确认第二次中断（raise）或缝隙中断
        #（子目录循环体之间，无任务在跑）。os._exit 立即终止：
        # sys.exit 会触发 ThreadPoolExecutor.shutdown(wait=True) 等待
        # worker 清理（正在 OCR 的 worker 几十秒到几分钟），停不下来。
        # 已入库的书保留（单书事务），正在写的事务由进程终止回滚，
        # 下次幂等续跑。
        os._exit(130)


def _warn_untagged() -> None:
    """收尾提醒：库内存在无任何 tag 的书。

    这类书在任何 tag 过滤下都搜不到（OMNIA 全集可见）——这是「漏打 tag」的显式信号，
    替代原先"靠搜不到来发现"的隐式机制（实测不工作：3 本 / 3493 chunk 长期未被察觉）。
    只读一条 COUNT；体检失败只提示，绝不影响"导入成功"这个既成事实。
    """
    try:
        rows = get_db().execute_sql(
            "SELECT COUNT(*) AS n FROM documents_book WHERE tags IS NULL OR tags = ''"
        )
        n = rows[0]["n"] if rows else 0
    except Exception as exc:
        click.echo(f"⚠️  tag 体检跳过（{exc}）", err=True)
        return
    if n:
        click.echo(
            f"⚠️  库内 {n} 本书无任何 tag（tag 过滤搜不到、OMNIA 全集可见）：mhi tag --audit",
            err=True,
        )


def _import_dir(p: Path, bucket, tags, recursive, force, no_dir_tag, exclude_dirs, parallel: int = 3):
    """目录导入：默认一级子目录分派（tag = 子目录名，与 --tags 叠加）。

    · 子目录分派模式：子目录内部一律 recursive=True（书名目录/auto 嵌套结构）
    · 排除名单排除全部子目录 → 自然回退单目录模式（整根导入）
    · 单目录模式（无子目录 / --no-dir-tag）：tag = 根目录名（importer 内置
      category_tag = dir_path.name），manual_tags 叠加不变
    """
    excluded = {s.strip() for s in exclude_dirs.split(",") if s.strip()}
    if not no_dir_tag:
        subdirs = sorted(
            d for d in p.iterdir() if d.is_dir() and d.name not in excluded
        )
        if subdirs:
            results: list[dict] = []
            for d in subdirs:
                results.extend(
                    import_book_directory(
                        str(d), bucket, tags, recursive=True, force=force,
                        parallel=parallel,
                    )
                )
            # 根目录直属文件（散装 txt / pandoc 转出的 md）：tags=本批 tags
            # —— LLM 从 source_path 自行推断细粒度语义，无需文件名启发式 tag
            root_files = sorted(
                f
                for f in p.iterdir()
                if f.is_file() and f.suffix.lower() in (".md", ".txt")
            )
            for f in root_files:
                results.append(
                    import_book_file(str(f), bucket, tags, force=force)
                )
            return results
    return import_book_directory(
        str(p), bucket, tags, recursive=recursive, force=force, parallel=parallel
    )


def _print_results(results):
    for r in results:
        status = r.get("status", "?")
        file = r.get("file", r.get("key_metadata", ""))
        guid = r.get("guid", "")
        if status == "ok":
            chunks = r.get("chunks", "?")
            click.echo(f"✅  {file} → {guid}（{chunks} chunks）")
        elif status == "skipped":
            click.echo(f"⏭️  {file}: 已导入，跳过（--force 重导）")
        elif status == "interrupted":
            click.echo(f"🛑  {file}: 用户中断（该书未入库）")
        else:
            click.echo(f"❌  {file}: {r.get('error', 'unknown')}")


if __name__ == "__main__":
    main()
