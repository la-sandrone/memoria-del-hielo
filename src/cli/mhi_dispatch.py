"""
文件名: mhi_dispatch.py
摘要: 子命令转发器 —— `mhi <子命令> [参数...]` → `src.cli.mhi_<模块> [参数...]`。
      项目根的薄壳（mhi.cmd / mhi.sh）把参数原样交给这里，映射表只此一份，
      避免在 cmd / bash 里各写一套子命令表导致漂移（平行宇宙）。
      调用方式（两个平台同构）：
          python <项目根>/src/cli/mhi_dispatch.py <子命令> [参数...]
      自举 sys.path（脚本方式运行时 sys.path[0] 是 src/cli，不是项目根）；
      不切 cwd —— 相对路径参数按调用者所在目录解析，config.toml 由
      Config._find_config() 的 __file__ 兜底找到。
      退出码：转发目标进程的原样透传（click 的 0/1/2…）；
              未知子命令 / 无法导入目标模块 → 2。
依赖: 仅标准库（importlib, sys, pathlib）
"""

from __future__ import annotations

import importlib
import sys
import unicodedata
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# (子命令, 目标模块, 中文说明, 别名...)
COMMANDS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("import", "mhi_import_book", "导入图书/游戏剧情（md/txt/epub/pdf）", ("import-book",)),
    ("search", "mhi_search", "检索知识库（向量 + 全文混合）", ()),
    ("find-book", "mhi_find_book", "书级语义检索（找书：哪本书讲 X）", ("find",)),
    ("tag", "mhi_tag", "管理书/聊天的 tag", ()),
    ("rm", "mhi_remove_book", "删除已入库的书（路径 或 --guid）⚠️破坏性", ("remove",)),
    ("images", "mhi_cleanup_images", "清理引用计数为 0 的图片 ⚠️破坏性", ("cleanup-images",)),
    ("emb", "mhi_cleanup_emb_cache", "清理嵌入缓存（过期/全部/统计）⚠️破坏性", ("cleanup-emb-cache",)),
    ("chat", "mhi_import_chat", "导入 AI 对话记录", ("import-chat",)),
    ("onenote", "mhi_import_onenote", "导入 OneNote 笔记", ("import-onenote",)),
    ("clean-chat", "mhi_clean_chat", "清洗单个对话文件", ()),
    ("reset", "mhi_reset_db", "删库重建（读 scripts/init_db.sql）🔴高危", ("reset-db",)),
)

HELP_WORDS = ("help", "list", "-h", "--help")


def _lookup(name: str) -> tuple[str, str] | None:
    """子命令/别名 → (规范名, 模块名)；未知返回 None。"""
    key = name.strip().lower()
    for canon, module, _desc, aliases in COMMANDS:
        if key == canon or key in aliases:
            return canon, module
    return None


def _disp_width(text: str) -> int:
    """终端显示宽度（CJK 全角算 2）——str.ljust 按字符数补空格会把表格顶歪。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _disp_width(text))


def print_usage() -> None:
    print("mhi —— Memoria del Hielo CLI 转发器")
    print()
    print("用法: mhi <子命令> [参数...]")
    print("      等价于 python -m src.cli.mhi_<模块> [参数...]")
    print()
    w_cmd, w_mod = 24, 24
    print(_pad("子命令", w_cmd) + _pad("目标模块", w_mod) + "说明")
    print("─" * 78)
    for canon, module, desc, aliases in COMMANDS:
        alias_txt = f"（别名 {'/'.join(aliases)}）" if aliases else ""
        print(_pad(f"{canon}{alias_txt}", w_cmd) + _pad(module, w_mod) + desc)
    print()
    print("帮助: mhi help            → 本表")
    print("      mhi help <子命令>   → 该子命令的参数帮助（等价 mhi <子命令> --help）")


def run_subcommand(module_name: str, prog_name: str, args: list[str]) -> int:
    """导入目标模块并以其 click 命令执行（同进程，Ctrl+C 语义不经过转发层）。"""
    try:
        module = importlib.import_module(f"src.cli.{module_name}")
    except Exception as exc:  # 导入失败要显式崩，不静默降级
        print(f"[mhi] 无法导入 src.cli.{module_name}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    command = getattr(module, "main", None)
    if command is None or not hasattr(command, "main"):
        print(f"[mhi] src.cli.{module_name} 没有 click 命令入口 main", file=sys.stderr)
        return 2
    try:
        command.main(args=list(args), prog_name=prog_name, standalone_mode=True)
    except SystemExit as exc:  # click 的退出码原样透传
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].strip().lower() in HELP_WORDS:
        # `mhi help <子命令>` → 转发为该子命令的 --help
        if len(argv) > 1 and argv[0].strip().lower() in ("help", "list"):
            hit = _lookup(argv[1])
            if hit is None:
                print(f"[mhi] 未知子命令: {argv[1]}", file=sys.stderr)
                print_usage()
                return 2
            canon, module = hit
            return run_subcommand(module, f"mhi {canon}", ["--help"])
        print_usage()
        return 0

    hit = _lookup(argv[0])
    if hit is None:
        print(f"[mhi] 未知子命令: {argv[0]}", file=sys.stderr)
        print_usage()
        return 2
    canon, module = hit
    return run_subcommand(module, f"mhi {canon}", argv[1:])


if __name__ == "__main__":
    sys.exit(main())
