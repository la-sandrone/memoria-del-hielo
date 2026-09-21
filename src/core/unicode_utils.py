"""
文件名: unicode_utils.py
摘要: Unicode 数字面量工具 —— 全项目唯一权威实现。
      · 可见码点（visible）: 非空白(Z*)/非控制(Cc/Cf/Cs) 的码点；标点(P*) 与
        PUA(Co) 算可见。切块尺寸口径（max_chars 按可见码点计，空白不计）。
      · PUA 净化: U+E000–U+F8FF → U+FFFD 等长替换。等长论证: PUA 与 U+FFFD
        同为 1 码点 / 1 可见 / UTF-8 3 字节 / 1 字素簇，故净化文本与原文在
        四个口径下逐字符等长 —— 偏移与长度对两者通用（chunk 存净化版，
        全文表存原文，回查零换算）。
      · 字素簇（UAX#29）: regex 模块 \\X 实现，切点完整性下界。
        将来换 PyICU 只改本文件的 grapheme_boundaries / align_floor / align_ceil。
依赖: regex>=2024.5.15（ai-services venv；PyICU 备选，暂不引入）
"""

from __future__ import annotations

import unicodedata
from array import array
from bisect import bisect_left, bisect_right

import regex

# ── PUA（私用区）──────────────────────────────────────────

_PUA_LO = 0xE000
_PUA_HI = 0xF8FF
PUA_REPLACEMENT = "\ufffd"  # U+FFFD REPLACEMENT CHARACTER（标准语义）


def is_pua(ch: str) -> bool:
    """U+E000–U+F8FF（BMP 私用区）。古籍 txt 常用其造生僻字/异体字。"""
    return _PUA_LO <= ord(ch) <= _PUA_HI


def sanitize_pua(text: str) -> tuple[str, list[int]]:
    """PUA → U+FFFD 等长替换。返回 (净化文本, 升序 PUA 码点位置列表)。

    无 PUA 时零拷贝返回原串（快路径）。位置列表用于 chunk 级 has_pua 判定
    （bisect 区间相交），坐标在净化前后一致（等长替换）。
    """
    if not any(is_pua(ch) for ch in text):
        return text, []
    out: list[str] = []
    pos: list[int] = []
    for i, ch in enumerate(text):
        if is_pua(ch):
            out.append(PUA_REPLACEMENT)
            pos.append(i)
        else:
            out.append(ch)
    return "".join(out), pos


# ── 可见码点 ──────────────────────────────────────────────


def is_visible(ch: str) -> bool:
    """可见 = 非空白(Z*)/非控制(Cc/Cf/Cs)。标点算可见（承担语义），PUA 算可见。"""
    cat = unicodedata.category(ch)
    return not (cat.startswith("Z") or cat in ("Cc", "Cf", "Cs"))


def visible_len(text: str) -> int:
    """可见码点计数（切块尺寸口径）。"""
    return sum(1 for ch in text if is_visible(ch))


def nth_visible_index(text: str, n: int, start: int = 0) -> int:
    """从 start 起第 n 个可见码点之后的位置（码点索引）。

    n<=0 → start；可见码点不足 → len(text)。调用方保证 start 为簇边界时，
    返回值用于字符硬切（随后 align_floor 对齐）。
    """
    if n <= 0:
        return start
    seen = 0
    for i in range(start, len(text)):
        if is_visible(text[i]):
            seen += 1
            if seen >= n:
                return i + 1
    return len(text)


def prefix_visible(text: str) -> array:
    """prefix[i] = text[:i] 的可见码点数。array('I')，O(n) 一次预处理，O(1) 查询。"""
    pref = array("I", [0]) * (len(text) + 1)
    c = 0
    for i, ch in enumerate(text):
        if is_visible(ch):
            c += 1
        pref[i + 1] = c
    return pref


def prefix_bytes(text: str) -> array:
    """prefix[i] = text[:i] 的 UTF-8 字节数（文本须已 NFC）。按码点值算变长宽度，O(n)。"""
    pref = array("I", [0]) * (len(text) + 1)
    c = 0
    for i, ch in enumerate(text):
        c += _utf8_width(ord(ch))
        pref[i + 1] = c
    return pref


def _utf8_width(cp: int) -> int:
    if cp < 0x80:
        return 1
    if cp < 0x800:
        return 2
    if cp < 0x10000:
        return 3
    return 4


# ── 字素簇（UAX#29 via regex \X）──────────────────────────


def grapheme_boundaries(text: str) -> array:
    """字素簇边界码点索引（含 0 与 len(text)），升序。O(n) 一次预处理。"""
    out = array("I", [0])
    for m in regex.finditer(r"\X", text):
        out.append(m.end())
    return out


def align_floor(boundaries: array, pos: int) -> int:
    """≤ pos 的最大簇边界（chunk 头回退：不劈开字素，宁可略短）。"""
    if pos <= 0:
        return 0
    i = bisect_right(boundaries, pos)
    return boundaries[i - 1] if i > 0 else 0


def align_ceil(boundaries: array, pos: int) -> int:
    """≥ pos 的最小簇边界（chunk 尾推进：不劈开字素）。"""
    i = bisect_left(boundaries, pos)
    return boundaries[i] if i < len(boundaries) else boundaries[-1]


def grapheme_count(boundaries: array, start: int, end: int) -> int:
    """区间 [start, end) 内的字素簇数。"""
    return bisect_right(boundaries, end) - bisect_right(boundaries, start)
