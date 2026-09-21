"""
文件名: text_cleaner.py
摘要: 书桶通用文本清洗器 —— 空白规范化（whitespace normalization）。
      变长操作：折叠行内连续空白 → 单空格、删除行尾空白、压缩连续空行，
      PUA 字符原样保留（净化仍在 chunker 内、chunk 输出阶段做等长替换）。
      清洗在 importer 层执行（切块/入库之前）：全文表 documents_book.content
      与 chunk 的偏移体系均基于清洗后文本；PUA 等长净化不改变偏移，
      故 SUBSTRING 回查在清洗后文本上仍精确（见 chunker.py 模块文档）。
      幂等：对已折叠文本重复调用无副作用。
依赖: 标准库 re
"""

from __future__ import annotations

import re

# 围栏代码块起始行（``` 或 ~~~，容忍行首缩进）
_RE_FENCE = re.compile(r"^\s*(?:`{3,}|~{3,})")

# 行内连续空白（含制表符、全角空格 U+3000 等一切 \s）→ 单空格
_RE_INLINE_WS = re.compile(r"\s+")


def collapse_whitespace(text: str) -> str:
    """空白规范化（幂等）。

    规则:
      · 行内连续空白 → 单空格（全角空格 U+3000、制表符一并折叠）
      · 行尾空白 → 删除（\r 也随之消失，天然统一换行为 \n）
      · 连续空行（≥3 个 \n）→ 2 个（段落分隔保留）
      · 行首缩进 → 原样保留（Markdown 列表 / 缩进代码语义）
      · 围栏代码块（``` / ~~~ 包裹）内部 → 删行尾空白、压缩连续空行（行首缩进保留）
      · PUA 字符非空白，不受影响

    动机: MinerU 输出的封面/版权页/表格密集区有大量无语义空白（行尾空格、
    行内多空格），既浪费 BGE-M3 的 token 预算，又让"码点 × 系数"的
    嵌入物理护栏误杀本可入库的 chunk。折叠后文本才是切块与入库基准。
    """
    lines: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if _RE_FENCE.match(line):
            in_fence = not in_fence
            lines.append(line)
            continue
        if in_fence:
            lines.append(line.rstrip())  # 代码块内：行尾空白删（行首缩进保留；空行→""）
            continue
        stripped = line.rstrip()
        if not stripped:
            lines.append("")  # 纯空白行 → 空行（稍后统一压缩连续空行）
            continue
        # 行首缩进原样保留，行内空白折叠
        lead = len(stripped) - len(stripped.lstrip())
        head = stripped[:lead]
        body = stripped[lead:]
        lines.append(head + _RE_INLINE_WS.sub(" ", body))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
