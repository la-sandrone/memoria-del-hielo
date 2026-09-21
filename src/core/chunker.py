"""
文件名: chunker.py
摘要: 结构感知文本切块器（书桶专用）v2 —— Unicode 政策落地。
      · 清洗前置: importer 层空白折叠（变长）；PUA 净化在 chunk 输出级等长替换，偏移不变
      · 尺寸口径: 可见码点（max_chars 物理护栏按可见计，空白不计入载荷）
      · 切点完整性: 字素簇边界对齐（UAX#29 via regex \\X；字符硬切点显式对齐，
        段落/句子边界假设天然为簇边界 —— 病态文本风险接受并文档化）
      · 度量输出: 三偏移（char/visible/byte）+ 四长度（char/visible/byte/grapheme）
        + has_pua；多节合并 chunk 的偏移语义 = 原文连续窗口 [start, start+len)，
        回查 SUBSTRING 取该窗口（对单节 chunk 完全精确，合并 chunk 含节间标题，更全）
      · 结构逻辑不变: 标题切节 → 章边界强制切块 → 微节打包 → 超长节段落/句子切分
依赖: core.unicode_utils, regex（UAX#29）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .unicode_utils import (
    align_floor,
    grapheme_boundaries,
    grapheme_count,
    nth_visible_index,
    prefix_bytes,
    prefix_visible,
    sanitize_pua,
    visible_len,
)

# ── 常量 ──────────────────────────────────────────────────

# 标题行（#{1,6}），容忍 MinerU 无空格变体（"##第一章绪论"）
_RE_HEADING = re.compile(r"^(#{1,6})\s*(.*?)\s*$")

# 章级标题推断（best-effort）：第X篇/部/章、Part N、Chapter N。
# MinerU 把章和节都拍平成 ##，层级只能从标题文本模式重建。
_RE_CHAPTER = re.compile(
    r"^(第\s*[0-9一二三四五六七八九十百千零两]+\s*[篇部章]"
    r"|part\s*\d+"
    r"|chapter\s*\d+)",
    re.IGNORECASE,
)

# 句子边界（零宽切分，用于 overlap 尾巴对齐）
_RE_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])")
# 句子边界定位（_split_sentences 用，返回 end 位置）
_RE_SENTENCE_BOUNDARY = re.compile(r"[。！？!?；;]")
_RE_FENCE = re.compile(r"^\s*(?:`{3,}|~{3,})")  # 围栏代码块（``` / ~~~，容忍行首缩进）
# 噪音节黑名单：MinerU 有时输出目录节，内容与正文标题重复
SKIP_HEADINGS = {"目录"}

# 书目卡片（书名 + 目录文本）字符上限，同样受 8K 物理约束
BOOK_CARD_MAX_CHARS = 2000

# 嵌入物理上限（BGE-M3 = 8192 token）；系数 1: XLM-R SentencePiece(250k) 下 CJK 1 码点≈1 token
EMBED_MAX_TOKENS = 8192
CHARS_PER_TOKEN_WORST = 1  # 空白已由 importer 清洗折叠；emoji byte-fallback 病态场景由文本类型规避
MAX_PHYSICAL_CODEPOINTS = 8000  # 切块层物理目标（总码点，留 2% 空挡；码点数是 token 的安全上界）

@dataclass
class Chunk:
    """一个切块。

    chunk_seq: 书内序号（从 1 连续编号）
    heading_path: 章节路径，如 "第二章 矿物 > 第三节 常见矿物"
    content: 切块正文（净化版：PUA → U+FFFD；≤ max_chars 可见码点）
    char_start / char_len:      码点坐标（SUBSTRING 回查用，1-based 转换 +1）
    visible_start / visible_len:可见码点坐标（切块口径，护栏对账）
    byte_start / byte_len:      NFC 后 UTF-8 字节坐标（C 侧直读）
    grapheme_len:               字素簇数（统计口径，无对应偏移——MariaDB 无字素概念）
    has_pua:                    本 chunk 原文区间内是否有 PUA（有则回查全文表取原文）
    """

    chunk_seq: int
    heading_path: str
    content: str
    char_start: int
    char_len: int
    visible_start: int
    visible_len: int
    byte_start: int
    byte_len: int
    grapheme_len: int
    has_pua: bool


@dataclass
class _Section:
    """标题到下一个标题之间的内容。heading="" 表示文件头部无标题区。

    char_start 精确指向首个非空白 block 的码点起点（_split_sections 保证）。
    """

    heading: str
    is_chapter: bool
    char_start: int
    blocks: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(self.blocks).strip()


# ── 对外接口 ──────────────────────────────────────────────


def chunk_markdown(
    md_text: str,
    target_chars: int = 2000,
    max_chars: int = 2500,
    min_chars: int = 400,
    overlap_chars: int = 300,
) -> list[Chunk]:
    """将文本切为 chunk 序列（尺寸参数均为可见码点数）。

      target_chars: 打包软目标（buffer 装到这个量就切块）
      max_chars:    硬上限（结构性保证，超限即抛异常）
      min_chars:    尾部微块并入前一块的阈值
      overlap_chars: 超长节内切分时相邻 piece 的重叠上限（码点窗口近似）

    返回 chunk_seq 从 1 连续编号的 Chunk 列表（含三偏移四长度度量）。
    """
    # 1. 工作文本 = 入参（importer 已完成空白清洗；PUA 净化在 chunk 输出级做）

    sections = _split_sections(md_text)
    if not sections:
        return []

    # 2. 一次预处理：可见/字节前缀 + 字素簇边界（O(n)）
    vpref = prefix_visible(md_text)
    bpref = prefix_bytes(md_text)
    gb = grapheme_boundaries(md_text)

    # (heading_path, content, char_start, char_end)  —— [char_start, char_end) 为
    # 原文真实覆盖区间（overlap chunk 的 content 含人造分隔符，区间不含）
    raw: list[tuple[str, str, int, int]] = []
    buffer: list[_Section] = []
    chapter = ""

    def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        content = "\n\n".join(s.text() for s in buffer)
        if content.strip():
            raw.append(
                (
                    _buffer_path(buffer, chapter),
                    content,
                    buffer[0].char_start,
                    buffer[-1].char_start + len(buffer[-1].text()),
                )
            )
        buffer = []

    for sec in sections:
        sec_len = visible_len(sec.text())
        if sec.is_chapter:
            flush()  # 章边界：绝不跨章合并
            chapter = sec.heading
        if sec_len == 0:
            continue  # 空节（孤立标题）：上下文已更新，无内容可切
        if sec_len > max_chars:
            flush()
            path = _section_path(sec, chapter)
            for piece, piece_start, piece_end in _split_long_text(
                sec.text(),
                target_chars,
                max_chars,
                overlap_chars,
                global_start=sec.char_start,
                boundaries=gb,
            ):
                raw.append((path, piece, piece_start, piece_end))
            continue
        if buffer and _buffer_len(buffer) + sec_len > target_chars:
            flush()
        buffer.append(sec)
    flush()

    # 尾部微块并入前一块（触发条件：flush 分隔了本应相邻的微节；可见口径）
    if len(raw) >= 2 and 0 < visible_len(raw[-1][1]) < min_chars:
        prev_path, prev_content, prev_start, prev_end = raw[-2]
        tail_path, tail_content, _, tail_end = raw[-1]
        if visible_len(prev_content) + 2 + visible_len(tail_content) <= max_chars:
            merged = prev_content + "\n\n" + tail_content
            raw[-2] = (prev_path, merged, prev_start, tail_end)
            raw.pop()

    # 编号 + 度量 + 物理护栏（宁可崩，不可静默超限）
    chunks: list[Chunk] = []
    for i, (path, content, start, end) in enumerate(raw, 1):
        content, pua_positions = sanitize_pua(content)  # chunk 输出级净化（等长，偏移不变）
        if visible_len(content) > max_chars:
            raise ValueError(
                f"chunk 超过 max_chars 硬上限: {visible_len(content)} 可见码点 > {max_chars} @ {path!r}"
            )
        if len(content) * CHARS_PER_TOKEN_WORST > EMBED_MAX_TOKENS:
            raise ValueError(
                f"chunk 超过嵌入物理上限(BGE-M3 8192 token): {len(content)} 码点 @ {path!r}"
            )
        if end > len(md_text):
            raise ValueError(
                f"chunk 覆盖区间越界: end={end} > len={len(md_text)} @ {path!r}"
            )
        chunks.append(
            Chunk(
                chunk_seq=i,
                heading_path=path,
                content=content,
                char_start=start,
                char_len=end - start,
                visible_start=vpref[start],
                visible_len=vpref[end] - vpref[start],
                byte_start=bpref[start],
                byte_len=bpref[end] - bpref[start],
                grapheme_len=grapheme_count(gb, start, end),
                has_pua=bool(pua_positions),
            )
        )
    return chunks


def build_book_card(
    book_title: str,
    md_text: str,
    max_chars: int = BOOK_CARD_MAX_CHARS,
) -> str:
    """书目卡片 = 书名 + 全部标题行构成的目录（截断）。

    用于书级 embedding（"找书"查询）：目录是全书语义的天然浓缩。
    入口净化（PUA 不进 embedding）。
    """
    clean_text, _ = sanitize_pua(md_text)
    headings: list[str] = []
    for line in clean_text.split("\n"):
        m = _RE_HEADING.match(line)
        if m:
            h = m.group(2).strip()
            if h and h not in SKIP_HEADINGS:
                headings.append(h)
    toc = "\n".join(headings)
    card = f"{book_title}\n{toc}" if toc else book_title
    return card[:max_chars]


# ── 内部：节解析 ──────────────────────────────────────────


def _split_sections(md_text: str) -> list[_Section]:
    """按标题行切节。SKIP_HEADINGS 命中的节整体丢弃。

    char_start 精确指向首个非空白 block 的码点起点：节内前导空白行
    （MinerU 标题后常见）被跳过不进入 blocks，保证 content 与偏移严格对应。
    """
    sections: list[_Section] = []
    current: _Section | None = None
    skipping = in_fence = False
    offset = 0

    for line in md_text.split("\n"):
        line_len = len(line) + 1; in_fence ^= bool(_RE_FENCE.match(line))  # 围栏状态机：代码块内不匹配标题
        m = _RE_HEADING.match(line) if not in_fence else None
        if m:
            heading = _clip(m.group(2).strip(), MAX_HEADING_CHARS)
            if heading in SKIP_HEADINGS:
                skipping = True
                current = None
            else:
                skipping = False
                current = _Section(
                    heading=heading,
                    is_chapter=bool(_RE_CHAPTER.match(heading)),
                    char_start=offset,
                )
                sections.append(current)
        else:
            if skipping:
                pass  # 丢弃目录节内容
            else:
                if current is None:
                    current = _Section("", False, offset)
                    sections.append(current)
                if not current.blocks and not line.strip():
                    pass  # 跳过前导空白行：保持 char_start 精确指向首个非空白行
                else:
                    if not current.blocks:
                        current.char_start = offset
                    current.blocks.append(line)
        offset += line_len

    # 不过滤空节：MinerU 常见"章标题后无正文直接进下一节"
    # （如"## 第一章绪论"后紧跟"## 第一节"）。空章节点必须保留，
    # 否则其 is_chapter 语义（章边界 flush + chapter 上下文）会随过滤一起丢失。
    return sections


# ── 内部：路径构造 ────────────────────────────────────────


def _section_path(sec: _Section, chapter: str) -> str:
    """单节 chunk 的 heading_path。"""
    if sec.is_chapter:
        return _clip(sec.heading, MAX_HEADING_CHARS)
    if sec.heading:
        return _clip(f"{chapter} > {sec.heading}" if chapter else sec.heading, MAX_HEADING_PATH)
    return chapter


def _buffer_path(buffer: list[_Section], chapter: str) -> str:
    """多节合并 chunk 的 heading_path：有章上下文取章名，否则取首节标题。"""
    if len(buffer) == 1:
        return _section_path(buffer[0], chapter)
    return _clip(chapter if chapter else (buffer[0].heading or ""), MAX_HEADING_PATH)


def _buffer_len(buffer: list[_Section]) -> int:
    return visible_len("\n\n".join(s.text() for s in buffer))


# ── 内部：超长节切分 ──────────────────────────────────────


def _split_long_text(
    text: str,
    target_chars: int,
    max_chars: int,
    overlap_chars: int,
    global_start: int,
    boundaries: object,
) -> list[tuple[str, int, int]]:
    """超长节 → (piece, 覆盖起点, 覆盖终点) 列表（全书码点坐标）。

    打包边界 target、硬上限 max（均可见码点口径），相邻 piece 带 overlap 尾巴
    （码点窗口 + 句子对齐 + 字素簇对齐）。global_start = text 在全书中的码点起点。
    覆盖终点 = 最后 unit 的终点（overlap chunk 的 content 含人造 "\n\n" 分隔符，
    区间不含 —— 回查窗口因此返回无缝原文）。
    """
    # 段落切分（手写 find 以保留精确偏移；跳过纯空白段）
    # 单段超限 → 立即降级 _split_giant_paragraph（句子级 + 字符硬切 + 簇对齐）
    # 所有返回坐标为全书码点坐标（global_start + 段内偏移）
    units: list[tuple[str, int, int]] = []
    i, n = 0, len(text)
    while i < n:
        j = text.find("\n\n", i)
        if j == -1:
            j = n
        para = text[i:j]
        stripped = para.strip()
        if stripped:
            lead = len(para) - len(para.lstrip())
            u_start = global_start + i + lead
            if visible_len(stripped) > max_chars:
                units.extend(
                    _split_giant_paragraph(stripped, max_chars, u_start, boundaries)
                )
            else:
                units.append((stripped, u_start, u_start + len(stripped)))
        i = j + 2

    pieces: list[tuple[str, int, int]] = []
    cur: list[tuple[str, int, int]] = []  # (unit_text, 全书起点, 全书终点)

    def cur_text() -> str:
        return "\n\n".join(u for u, _, _ in cur)

    def cur_start() -> int:
        return cur[0][1] if cur else 0

    def cur_end() -> int:
        return cur[-1][2] if cur else 0

    for u, u_start, u_end in units:
        if cur and (visible_len(cur_text()) + 2 + visible_len(u) > target_chars or len(cur_text()) + 2 + len(u) > MAX_PHYSICAL_CODEPOINTS):
            joined = cur_text()
            old_start = cur_start()
            pieces.append((joined, old_start, cur_end()))
            tail, tail_start = _tail_text(joined, overlap_chars, old_start, boundaries)
            if tail:
                # tail 是 joined 的近似后缀（起点已含字素簇回退）；终点 = joined 终点
                cur = [(tail, tail_start, old_start + len(joined))]
                if visible_len(cur_text()) + 2 + visible_len(u) > max_chars or len(cur_text()) + 2 + len(u) > MAX_PHYSICAL_CODEPOINTS:
                    cur = []  # 尾巴装不下巨单元就丢（病态场景，无重叠可接受）
            else:
                cur = []
        cur.append((u, u_start, u_end))
    if cur:
        pieces.append((cur_text(), cur_start(), cur_end()))
    return pieces


def _split_giant_paragraph(
    para: str,
    max_chars: int,
    global_start: int,
    boundaries: object,
) -> list[tuple[str, int, int]]:
    """无段落边界的巨段 → (piece, 覆盖起点, 覆盖终点) 列表（全书码点坐标）。

    global_start = 段在全书中的码点起点。巨段内 piece 连续切分（无 overlap），
    终点 = 起点 + 内容长。
    句子级积累；单句仍超限 → 可见码点级切片，切点对齐字素簇边界
    （全局坐标对齐，保证不劈开组合序列）。
    """
    parts: list[tuple[str, int, int]] = []
    cur = ""
    cur_start = global_start
    for s, s_start in _split_sentences(para):
        if visible_len(s) > max_chars:
            if cur:
                parts.append((cur, cur_start, cur_start + len(cur)))
                cur = ""
            pos = 0
            while pos < len(s):
                end = min(nth_visible_index(s, max_chars, start=pos), pos + MAX_PHYSICAL_CODEPOINTS)  # 双口径：可见上限与物理码点上限先到者
                g_end = align_floor(boundaries, global_start + s_start + end) - (
                    global_start + s_start
                )
                if g_end <= pos:
                    g_end = pos + 1  # 防御：簇边界必 ≥ pos（pos 自身是边界）
                parts.append(
                    (
                        s[pos:g_end],
                        global_start + s_start + pos,
                        global_start + s_start + g_end,
                    )
                )
                pos = g_end
            continue
        if cur and (visible_len(cur) + visible_len(s) > max_chars or len(cur) + len(s) > MAX_PHYSICAL_CODEPOINTS):
            parts.append((cur, cur_start, cur_start + len(cur)))
            cur = ""
        if not cur:
            cur_start = global_start + s_start
        cur += s
    if cur:
        parts.append((cur, cur_start, cur_start + len(cur)))
    return parts


def _split_sentences(para: str) -> list[tuple[str, int]]:
    """按句子边界切分（标点含边界）。返回 (句子, 段内码点起点)。"""
    out: list[tuple[str, int]] = []
    start = 0
    for m in _RE_SENTENCE_BOUNDARY.finditer(para):
        end = m.end()
        out.append((para[start:end], start))
        start = end
    if start < len(para):
        out.append((para[start:], start))
    return out


def _tail_text(
    text: str,
    overlap_chars: int,
    global_start: int,
    boundaries: object,
) -> tuple[str, int]:
    """取末尾 overlap 码点窗口，句子对齐（丢弃被截断的半句）+ 字素簇对齐。

    返回 (近似后缀, 全局码点起点)——簇对齐回退后起点必须与内容配套，
    调用方不得用长度反推。overlap 是语义尾巴的工程近似：窗口起点按
    strip 后长度推算，偏差仅限空白字符。
    """
    if overlap_chars <= 0 or not text:
        return "", global_start
    tail = text[-overlap_chars:]
    parts = _RE_SENTENCE_SPLIT.split(tail)
    if len(parts) > 1:
        tail = "".join(parts[1:])
    tail = tail.strip()
    if not tail:
        return "", global_start
    win_start = len(text) - len(tail)
    g_start = align_floor(boundaries, global_start + win_start)
    return text[g_start - global_start :], g_start




    return i < len(pua_positions) and pua_positions[i] < end


# ── 内部：heading_path 截断降级 ──────────────────────────────
# heading_path 列宽 varchar(512)（utf8mb4 字符单位）。MinerU 把词典
# 词条整行识别成标题时可达近千字符（实测 958），单级截断 + 总长兜底，
# 宁可略短不劈开字素簇（防孤立代理对入库）。
MAX_HEADING_CHARS = 200
MAX_HEADING_PATH = 512


def _clip(s: str, limit: int) -> str:
    """截断到 ≤limit 码点且不劈开字素簇（heading_path 降级；短文本零拷贝）。"""
    if len(s) <= limit:
        return s
    cut = align_floor(grapheme_boundaries(s[: limit + 1]), limit)
    return s[:cut]
