"""
测试: core/unicode_utils.py 的康熙部首窄归一（fold_kangxi_radicals）+ 生成表一致性。

两层判据：
  · 与权威对齐 —— 重新问 ICU，断言 src/core/kangxi_table.py 逐字节一致
    （PyICU 缺失时 skip，绝不静默通过）
  · 与不变式对齐 —— 四口径等长、幂等、快路径、偏移通用、不误伤标点
"""

from __future__ import annotations

import subprocess
import sys
import unicodedata as ud
from pathlib import Path

import pytest
import regex

from src.core import kangxi_table
from src.core.unicode_utils import (
    fold_kangxi_radicals,
    is_kangxi_radical,
    kangxi_positions,
    prefix_bytes,
    prefix_visible,
    grapheme_boundaries,
    visible_len,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "scripts" / "gen_kangxi_radical_table.py"
TARGET = REPO_ROOT / "src" / "core" / "kangxi_table.py"

KANGXI_LO, KANGXI_HI = 0x2F00, 0x2FDF
CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF))

# 月背征途 版权页原句（真实污染样本，取自 v3/v4 两代一致的产出）
DIRTY = "⽉背征途:中国探⽉国家队记录⼈类⾸次登陆⽉球背⾯全过程"
CLEAN = "月背征途:中国探月国家队记录人类首次登陆月球背面全过程"


def _dims(ch: str) -> tuple[int, int, int, int]:
    cat = ud.category(ch)
    vis = 0 if (cat.startswith("Z") or cat in ("Cc", "Cf", "Cs")) else 1
    return (len(ch), vis, len(ch.encode("utf-8")), len(regex.findall(r"\X", ch)))


# ── 与权威（ICU）对齐 ────────────────────────────────────


def _has_pyicu() -> bool:
    try:
        import icu  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _has_pyicu(), reason="需要 PyICU（生成权威表用）")
def test_generated_table_matches_icu():
    """生成器 --check：现表必须与 ICU 逐字节一致。"""
    r = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert r.returncode == 0, (
        f"kangxi_table.py 与 ICU 不一致，需重跑 --write\n"
        f"stdout: {r.stdout}\nstderr: {r.stderr}"
    )


def test_generator_and_target_paths_exist():
    assert GENERATOR.is_file(), f"生成器缺失: {GENERATOR}"
    assert TARGET.is_file(), f"生成表缺失: {TARGET}"


def _stdlib_nfkc_map() -> dict[int, str]:
    """仅用标准库从当前解释器的 Unicode 数据推导本块映射。"""
    out: dict[int, str] = {}
    for cp in range(KANGXI_LO, KANGXI_HI + 1):
        ch = chr(cp)
        dec = ud.normalize("NFKC", ch)
        if dec == ch or len(dec) != 1:
            continue
        if any(lo <= ord(dec) <= hi for lo, hi in CJK_RANGES):
            out[cp] = dec
    return out


def test_table_targets_agree_with_stdlib_nfkc():
    """**跨平台硬断言**：现表每一条的目标，必须等于标准库的 NFKC 结果。

    这条在 Windows 上照跑（不需要 PyICU），专治"生成文件被手改/搬运出错"。
    方向是单向的（只查表内条目），故不会因为未来 Unicode 新分配码点而误报。
    """
    for cp, expected in kangxi_table.KANGXI_FOLD.items():
        got = ud.normalize("NFKC", chr(cp))
        assert got == expected, (
            f"U+{cp:04X} {chr(cp)}: 现表写 {expected!r}(U+{ord(expected):04X})，"
            f"本机 unicodedata({ud.unidata_version}) NFKC 给 {got!r}"
        )


def test_table_coverage_matches_stdlib_nfkc():
    """覆盖度对齐：本块里标准库认为可映射的码点，表里一个都不能漏。

    与上一条不同，这条**故意是版本敏感的**：若将来 Unicode 给 U+2FD6–U+2FDF
    新分配了带分解的码点，它会红——那正是我们要的信号（重跑生成器 --write）。
    生成器需要 PyICU，Linux 侧跑；Windows 侧红了就照这条信息去 Linux 侧重生成。
    """
    derived = _stdlib_nfkc_map()
    missing = {cp: v for cp, v in derived.items() if cp not in kangxi_table.KANGXI_FOLD}
    extra = {cp: v for cp, v in kangxi_table.KANGXI_FOLD.items() if cp not in derived}
    assert not missing and not extra, (
        f"表与 unicodedata({ud.unidata_version}) 覆盖不一致\n"
        f"  漏掉 {sorted(hex(c) for c in missing)}\n"
        f"  多出 {sorted(hex(c) for c in extra)}\n"
        f"  处置：在装有 PyICU 的环境重跑 "
        f"`python scripts/gen_kangxi_radical_table.py --write`"
    )


def test_generated_module_is_stdlib_only():
    """生成表必须是纯字面量——Windows 侧要能零依赖 import（PyICU 无 Windows wheel）。"""
    src = TARGET.read_text(encoding="utf-8")
    imports = [
        ln.strip() for ln in src.splitlines()
        if ln.startswith(("import ", "from ")) and "__future__" not in ln
    ]
    assert imports == [], f"生成表引入了非标准库依赖: {imports}"


# ── 表自身的不变式 ──────────────────────────────────────


def test_table_shape_and_alignment():
    assert len(kangxi_table._SRC) == len(kangxi_table._DST)
    assert len(kangxi_table._SRC) == kangxi_table.KANGXI_FOLD_SIZE
    assert len(kangxi_table.KANGXI_FOLD) == kangxi_table.KANGXI_FOLD_SIZE
    for cp in map(ord, kangxi_table._SRC):
        assert KANGXI_LO <= cp <= KANGXI_HI, f"U+{cp:04X} 不在康熙部首块"
    assert kangxi_table.KANGXI_FOLD == dict(
        zip(map(ord, kangxi_table._SRC), kangxi_table._DST)
    )


def test_targets_are_cjk_ideographs():
    for ch in kangxi_table._DST:
        cp = ord(ch)
        assert any(lo <= cp <= hi for lo, hi in CJK_RANGES), (
            f"目标 U+{cp:04X} 不是 CJK 汉字（等长/语义前提）"
        )


def test_four_dimension_equal_length():
    """等长是偏移零换算的前提 —— 逐条断言，不留例外。"""
    for src, dst in kangxi_table.KANGXI_FOLD.items():
        a, b = chr(src), dst
        assert _dims(a) == _dims(b), f"U+{src:04X} {a} → {b}: {_dims(a)} != {_dims(b)}"


# ── 函数行为 ────────────────────────────────────────────


def test_fold_real_world_sample():
    assert fold_kangxi_radicals(DIRTY) == CLEAN


def test_fold_is_idempotent():
    once = fold_kangxi_radicals(DIRTY)
    assert fold_kangxi_radicals(once) == once


def test_fold_fast_path_returns_same_object():
    """干净文本零拷贝返回（不新建 str 对象）。"""
    s = "月球背面着陆，嫦娥一号"
    assert fold_kangxi_radicals(s) is s


def test_fold_length_never_changes():
    assert len(fold_kangxi_radicals(DIRTY)) == len(DIRTY)
    blob = DIRTY * 37 + "abc，！（）；？…℃"
    assert len(fold_kangxi_radicals(blob)) == len(blob)


def test_fold_does_not_touch_fullwidth_punctuation():
    """全文 NFKC 的反例：这些字符必须原样保留（否则标点语义与长度都变）。"""
    s = "甲，乙！丙（丁）；戊？己…庚℃"
    assert fold_kangxi_radicals(s) == s
    for ch in "，！（）；？…℃":
        assert not is_kangxi_radical(ch)


def test_fold_leaves_cjk_radicals_supplement_alone():
    """边界：CJK 部首补充的简体部首 Unicode 不给等价（⺟⻳ 两个例外也不入表）。"""
    s = "⻜⻋⻓⻆⻛⻰⻅⻔⻝⻢⺟⻳"
    assert fold_kangxi_radicals(s) == s


def test_offsets_are_interchangeable():
    """四口径偏移对净化前后通用 —— chunk 坐标可以两边混用。"""
    folded = fold_kangxi_radicals(DIRTY)
    assert len(folded) == len(DIRTY)
    assert visible_len(folded) == visible_len(DIRTY)
    assert list(prefix_visible(folded)) == list(prefix_visible(DIRTY))
    assert list(prefix_bytes(folded)) == list(prefix_bytes(DIRTY))
    assert list(grapheme_boundaries(folded)) == list(grapheme_boundaries(DIRTY))


def test_is_kangxi_radical_and_positions():
    assert is_kangxi_radical("⽉") and is_kangxi_radical("⼀")
    assert not is_kangxi_radical("月") and not is_kangxi_radical("⻜")
    pos = kangxi_positions(DIRTY)
    assert len(pos) == sum(1 for ch in DIRTY if is_kangxi_radical(ch))
    assert pos == sorted(pos)
    assert all(is_kangxi_radical(DIRTY[i]) for i in pos)
