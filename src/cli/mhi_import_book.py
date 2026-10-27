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
      · 收 *.md / *.txt / **MinerU 的 *.zip**（归档内含 markdown.md 才算）；
        zip 先物化到 cache 再按 md 导入，身份/书名挂在 zip 自己身上
      · MinerU 的裸结构 JSON（--format middle_json 的产出）**明确拒绝**并指路
        `--format zip`（官方：standalone structural JSON is not a complete
        result package）
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
    SRC_EXTS,
    MiddleJsonInputError,
    import_book_file,
    import_book_directory,
    reset_interrupts,
    _INTERRUPTS,
)


@click.command("mhi-import-book")
@click.argument("paths", nargs=-1, required=True, type=click.Path(exists=True))
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
def main(
    paths: tuple[str, ...],
    bucket: str | None,
    tags: str,
    recursive: bool,
    force: bool,
    no_dir_tag: bool,
    exclude_dirs: str,
):
    """导入图书/游戏剧情文件到知识库（chunk 层，幂等可断点续传）。

    PATH 可给**多个**（`mhi import A B C`）：它们视作**一组**，共用同一套参数
    （--bucket/--tags/--recursive/--force 一并作用于全部）。书级计数器 `[X/N]`
    的 N 是**全部 PATH 的可导入文件总数**（.md / .txt / MinerU 的 .zip），
    X 跨 PATH 连续累加 —— 一次命令行 = 一次作业。
    （每 PATH 各配一套参数的 ffmpeg 式语法见需求清单，尚未实施。）

    Ctrl+C 两级语义：第一次中断当前书（该书未入库，继续下一本）；
    第二次停止全部（已导书保留）。
    """
    reset_interrupts()
    try:
        ps = [Path(x) for x in paths]
        excluded = {s.strip() for s in exclude_dirs.split(",") if s.strip()}

        # ── 多 PATH = **一组**（用户裁定：追加无限个 PATH，但应用同样的参数设置）──
        # 书级计数器 [X/N] 的 N 是**全部 PATH 的总数**，X 跨 PATH 连续累加
        # （一次命令行 = 一次作业；已与用户确认，不按 PATH 分设）。
        # 先枚举、后执行：`_count_path` 与 `_import_dir` **复用同一把尺**
        # （_safe_glob_src），否则"计数的尺"与"执行的尺"迟早走偏。
        totals = [_count_path(x, recursive, no_dir_tag, excluded) for x in ps]
        grand_total = sum(totals)

        results: list[dict] = []
        seen = 0
        for x, n in zip(ps, totals):
            # 裸结构 JSON 是「这类输入不支持」，不是「这本坏了」：
            # 报清楚、给改法，然后**继续处理其余 PATH**（不因一个坏 PATH 全停）
            try:
                if x.is_dir():
                    results.extend(
                        _import_dir(
                            x, bucket, tags, recursive, force, no_dir_tag,
                            exclude_dirs,
                            index_offset=seen, grand_total=grand_total,
                        )
                    )
                    seen += n
                else:
                    seen += 1
                    # 单文件入口自身不打印书级行 ⟹ 这里补，保持序号连续
                    click.echo(f"[{seen}/{grand_total}] {x.name}", err=True)
                    results.append(
                        import_book_file(str(x), bucket, tags, force=force)
                    )
            except MiddleJsonInputError as exc:
                click.echo(
                    f"\n❌ 拒绝导入：{exc}\n", err=True
                )
                results.append(
                    {"file": str(x), "status": "error",
                     "error": "MinerU 裸结构 JSON，不是完整结果包"}
                )
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


def _count_path(p: Path, recursive: bool, no_dir_tag: bool, excluded: set) -> int:
    """一个 PATH 会处理几个可导入文件（.md/.txt/MinerU zip）—— 供 [X/N] 求 N。

    ⚠️ **必须与 `_import_dir` 的枚举严格同构**，且两边都复用 importer 的
    `_safe_glob_src`。计数的尺与执行的尺一旦走偏，就会出现「总说 13、实际跑了 12」
    —— 本项目踩过的平行宇宙型屎山，不能再造一个。
    """
    from ..importers.book_importer import _safe_glob_src

    if p.is_file():
        return 1
    if not no_dir_tag:
        subdirs = [d for d in p.iterdir() if d.is_dir() and d.name not in excluded]
        if subdirs:
            root_files = [
                f for f in p.iterdir()
                if f.is_file() and f.suffix.lower() in SRC_EXTS
            ]
            return sum(len(_safe_glob_src(d, True)) for d in subdirs) + len(root_files)
    return len(_safe_glob_src(p, recursive))


def _import_dir(p: Path, bucket, tags, recursive, force, no_dir_tag, exclude_dirs,
                index_offset: int = 0, grand_total: int = 0):
    """目录导入：默认一级子目录分派（tag = 子目录名，与 --tags 叠加）。

    · 子目录分派模式：子目录内部一律 recursive=True（书名目录/auto 嵌套结构）
    · 排除名单排除全部子目录 → 自然回退单目录模式（整根导入）
    · 单目录模式（无子目录 / --no-dir-tag）：tag = 根目录名（importer 内置
      category_tag = dir_path.name），manual_tags 叠加不变
    """
    # 枚举用 importer 自己那把尺（_safe_glob_src），**不另写一套 glob** ——
    # 否则"计数的尺"与"执行的尺"迟早走偏，出现"总说 13 实际跑了 12"。
    from ..importers.book_importer import _safe_glob_src

    excluded = {s.strip() for s in exclude_dirs.split(",") if s.strip()}
    if not no_dir_tag:
        subdirs = sorted(
            d for d in p.iterdir() if d.is_dir() and d.name not in excluded
        )
        if subdirs:
            # ── 先枚举、后执行 ──
            # 书级计数器显示 [X/总数]，而总数必须**跨全部子目录 + 根散装文件**一起算。
            # 旧行为是每个 import_directory 各自计数 ⟹ 子目录分派下每组只有 1 本，
            # 分母恒为 1（[1/1]），零信息量。
            root_files = sorted(
                f
                for f in p.iterdir()
                if f.is_file() and f.suffix.lower() in SRC_EXTS
            )
            counts = [len(_safe_glob_src(d, True)) for d in subdirs]
            if not grand_total:          # 单 PATH 直调 ⟹ 自算（旧行为不变）
                grand_total = sum(counts) + len(root_files)

            results: list[dict] = []
            seen = index_offset
            for d, n in zip(subdirs, counts):
                results.extend(
                    import_book_directory(
                        str(d), bucket, tags, recursive=True, force=force,
                        index_offset=seen, grand_total=grand_total,
                    )
                )
                seen += n
            # 根目录直属文件（散装 txt / pandoc 转出的 md）：tags=本批 tags
            # —— LLM 从 source_path 自行推断细粒度语义，无需文件名启发式 tag
            for f in root_files:
                seen += 1
                # 单文件入口自己不打印书级行 ⟹ 这里补，保持序号连续不断档
                click.echo(f"[{seen}/{grand_total}] {f.name}", err=True)
                results.append(
                    import_book_file(str(f), bucket, tags, force=force)
                )
            return results
    return import_book_directory(
        str(p), bucket, tags, recursive=recursive, force=force,
        index_offset=index_offset,
        grand_total=grand_total or len(_safe_glob_src(p, recursive)),
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
