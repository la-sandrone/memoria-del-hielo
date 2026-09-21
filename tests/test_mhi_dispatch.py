"""
测试: src/cli/mhi_dispatch.py —— 子命令转发器 + 项目根薄壳（mhi.cmd / mhi.sh）

不碰数据库：所有用例只走 help/usage 路径或纯函数。
额外护栏：
  · 映射表必须覆盖 src/cli/ 下所有 mhi_*.py（新增 CLI 忘了登记 → 红）
  · mhi.cmd 必须是纯 ASCII + CRLF（cmd.exe 按当前代码页逐行解析批处理，
    非 ASCII 字节会被误读；中文一律由 Python 侧输出）
"""

from pathlib import Path

import pytest

from src.cli import mhi_dispatch as d

PROJECT_ROOT = Path(d.__file__).resolve().parents[2]


# ---------- 映射表 ----------

def test_lookup_short_names_and_aliases():
    assert d._lookup("import") == ("import", "mhi_import_book")
    assert d._lookup("import-book") == ("import", "mhi_import_book")
    assert d._lookup("rm") == ("rm", "mhi_remove_book")
    assert d._lookup("remove") == ("rm", "mhi_remove_book")
    assert d._lookup("RM") == ("rm", "mhi_remove_book")  # 大小写不敏感
    assert d._lookup("  search  ") == ("search", "mhi_search")


def test_lookup_unknown_returns_none():
    assert d._lookup("nope") is None
    assert d._lookup("") is None


def test_mapping_covers_every_cli_module():
    """完整性护栏：src/cli/mhi_*.py 与映射表必须一一对应。"""
    on_disk = {p.stem for p in (PROJECT_ROOT / "src" / "cli").glob("mhi_*.py")}
    on_disk.discard("mhi_dispatch")  # 转发器自己不转发给自己
    mapped = {module for _c, module, _desc, _alias in d.COMMANDS}
    assert mapped == on_disk, (
        f"未登记进映射表的 CLI: {sorted(on_disk - mapped)}；"
        f"映射表里不存在的模块: {sorted(mapped - on_disk)}"
    )


# ---------- 薄壳文件的编码护栏 ----------

def test_mhi_cmd_is_ascii_only_with_crlf():
    raw = (PROJECT_ROOT / "mhi.cmd").read_bytes()
    assert b"\r\n" in raw, "mhi.cmd 必须用 CRLF（cmd.exe 友好）"
    non_ascii = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    assert not non_ascii, f"mhi.cmd 含非 ASCII 字节（cmd 会按当前代码页误读）: {non_ascii[:5]}"


def test_mhi_cmd_never_calls_chcp_and_relies_on_python_encodings():
    """护栏：mhi.cmd 不得调用 chcp，编码交给 Python（PEP 528 + -X utf8）。

    依据（2026-08-29 双场景实测 + 官方文档）：
      · 真控制台：isatty=True 时 stdout.encoding 恒为 utf-8 —— PEP 528 规定
        流是控制台缓冲区时用 _io.WindowsConsoleIO，字节层 UTF-8、交给
        ReadConsoleW/WriteConsoleW，**绕过活动代码页**。
        实测控制台 CP=936 下中文与 emoji（⚠️🔴）显示正常。
      · 管道/重定向：isatty=False → Python 退回 locale 编码（gbk），打印
        U+26A0 直接 UnicodeEncodeError（实测崩溃）；-X utf8（PEP 540）修复。
      · chcp 会清屏（chcp.com 副作用）并修改共享控制台状态 → 一律不用，
        也不用"保存/还原代码页"那套（连临时文件都省了）。
    """
    txt = (PROJECT_ROOT / "mhi.cmd").read_text(encoding="ascii")
    assert "-X utf8" in txt, "管道场景必须靠 -X utf8 把 stdio 钉成 UTF-8"
    offenders = [ln for ln in txt.splitlines()
                 if ln.strip().lower().startswith("chcp")]
    assert not offenders, f"mhi.cmd 不得调用 chcp（清屏 + 改共享控制台状态）: {offenders}"
    assert "_MHI_CPFILE" not in txt, "不应再有临时文件兜底"
    assert 'set "PYTHONIOENCODING' not in txt, "不应改写环境变量（-X utf8 已覆盖）"


def test_mhi_sh_forwards_with_exec_and_preserves_argv():
    txt = (PROJECT_ROOT / "mhi.sh").read_text(encoding="utf-8")
    assert 'exec "$PY" "$ROOT/src/cli/mhi_dispatch.py" "$@"' in txt, (
        "必须 exec + \"$@\"：exec 保证 Ctrl+C 直达 python，\"$@\" 保证参数不被重新解析"
    )
    assert not txt.startswith("#!" + "\n"), "缺少 shebang"


# ---------- 入口行为（只走 help，不连库） ----------

def test_no_args_prints_usage(capsys):
    assert d.main([]) == 0
    out = capsys.readouterr().out
    for canon, _m, _desc, _a in d.COMMANDS:
        assert canon in out


def test_unknown_subcommand_prints_usage_and_exit_2(capsys):
    assert d.main(["nope"]) == 2
    err = capsys.readouterr().err
    assert "未知子命令" in err


def test_help_word_variants(capsys):
    for word in ("help", "list", "--help", "-h"):
        assert d.main([word]) == 0
        assert "mhi" in capsys.readouterr().out


def test_forwards_args_to_target_subcommand(capsys):
    """mhi import --help → 透传到 mhi_import_book 的 click help（不连库）。"""
    assert d.main(["import", "--help"]) == 0
    out = capsys.readouterr().out
    assert "--bucket" in out and "--parallel" in out
    assert "mhi import" in out  # prog_name 前缀正确


def test_help_of_subcommand_form(capsys):
    """mhi help import == mhi import --help。"""
    assert d.main(["help", "import"]) == 0
    assert "--bucket" in capsys.readouterr().out


def test_help_of_unknown_subcommand(capsys):
    assert d.main(["help", "nope"]) == 2
    assert "未知子命令" in capsys.readouterr().err


def test_alias_dispatches_to_same_command(capsys):
    assert d.main(["remove", "--help"]) == 0
    out = capsys.readouterr().out
    assert "--guid" in out and "--dry-run" in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
