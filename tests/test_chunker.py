"""
测试: core/chunker.py — 结构感知切块器
"""

from src.core.chunker import (
    chunk_markdown,
    build_book_card,
    BOOK_CARD_MAX_CHARS,
)
from src.core.unicode_utils import (
    sanitize_pua,
    visible_len,
    nth_visible_index,
    grapheme_boundaries,
    grapheme_count,
    align_floor,
    align_ceil,
)


def test_chapter_and_section_paths():
    """章/节标题（MinerU 拍平为同级 ##）→ 两级 heading_path。"""
    md = (
        "## 第一章 绪论\n\n" + "甲" * 600 + "\n\n"
        "## 第一节 研究对象\n\n" + "乙" * 600 + "\n\n"
    )
    chunks = chunk_markdown(
        md, target_chars=500, max_chars=700, min_chars=100, overlap_chars=80
    )
    assert chunks[0].heading_path == "第一章 绪论"
    assert chunks[1].heading_path == "第一章 绪论 > 第一节 研究对象"
    assert chunks[1].chunk_seq == 2


def test_chapter_without_space():
    """MinerU 常见变体："第一章绪论"（缺空格）也要能识别为章。"""
    md = "## 第一章绪论\n\n" + "甲" * 100
    chunks = chunk_markdown(md, target_chars=500, max_chars=700, min_chars=10, overlap_chars=30)
    assert chunks[0].heading_path == "第一章绪论"


def test_packing_small_sections():
    """微节打包合并，多节块的路径取首节标题。"""
    md = "\n\n".join(f"## 小节{i}\n\n{'丙' * 30}" for i in range(1, 6))
    chunks = chunk_markdown(
        md, target_chars=80, max_chars=100, min_chars=10, overlap_chars=20
    )
    assert len(chunks) >= 2
    assert all(len(c.content) <= 100 for c in chunks)
    assert "小节1" in chunks[0].heading_path


def test_chapter_boundary_never_merges():
    """章边界强制切块：即使两章加起来远小于上限也不合并。"""
    md = (
        "## 第一章 A\n\n" + "甲" * 50 + "\n\n"
        "## 第二章 B\n\n" + "乙" * 50 + "\n\n"
    )
    chunks = chunk_markdown(
        md, target_chars=500, max_chars=500, min_chars=10, overlap_chars=20
    )
    assert len(chunks) == 2
    assert chunks[0].heading_path == "第一章 A"
    assert chunks[1].heading_path == "第二章 B"


def test_long_section_split_with_overlap():
    """超长节切分：多段落书内容，相邻 piece 带句子级 overlap 尾巴。"""
    paragraphs = "\n\n".join("句子内容。" * 8 for _ in range(20))
    md = "## 大节\n\n" + paragraphs
    chunks = chunk_markdown(
        md, target_chars=200, max_chars=250, min_chars=10, overlap_chars=60
    )
    assert len(chunks) >= 4
    assert all(len(c.content) <= 250 for c in chunks)
    # overlap：piece2 的开头出现在 piece1 里
    assert chunks[1].content[:10] in chunks[0].content
    assert all(c.heading_path == "大节" for c in chunks)


def test_giant_paragraph_raw_sliced():
    """无标点巨段：退化为原始字符切片，硬上限不破。"""
    md = "## 巨段\n\n" + "字" * 1000
    chunks = chunk_markdown(
        md, target_chars=200, max_chars=250, min_chars=10, overlap_chars=60
    )
    assert all(len(c.content) <= 250 for c in chunks)
    assert sum(len(c.content) for c in chunks) >= 1000


def test_toc_section_skipped():
    """"目录" 噪音节整体丢弃。"""
    md = "## 目录\n\n目录标记行XYZ\n\n## 第一章 A\n\n正文内容甲。"
    chunks = chunk_markdown(
        md, target_chars=500, max_chars=600, min_chars=10, overlap_chars=30
    )
    assert len(chunks) == 1
    assert "目录标记行XYZ" not in chunks[0].content
    assert chunks[0].heading_path == "第一章 A"


def test_no_headings_fallback():
    """无标题文本：退化为纯段落打包，heading_path 为空。"""
    paras = "\n\n".join("丁" * 50 for _ in range(10))
    chunks = chunk_markdown(
        paras, target_chars=120, max_chars=150, min_chars=10, overlap_chars=30
    )
    assert len(chunks) >= 2
    assert all(len(c.content) <= 150 for c in chunks)
    assert all(c.heading_path == "" for c in chunks)


def test_tiny_tail_merged():
    """flush 分隔出的尾部微块并入前一块。"""
    md = "## A\n\n" + "甲" * 140 + "\n\n## B\n\n" + "乙" * 20
    chunks = chunk_markdown(
        md, target_chars=150, max_chars=180, min_chars=50, overlap_chars=20
    )
    assert len(chunks) == 1
    assert "乙" in chunks[0].content


def test_empty_input():
    assert chunk_markdown("") == []
    assert chunk_markdown("## 目录\n\n仅目录") == []


def test_seq_sequential():
    md = "## A\n\n" + "甲" * 80 + "\n\n## B\n\n" + "乙" * 80
    chunks = chunk_markdown(
        md, target_chars=100, max_chars=120, min_chars=10, overlap_chars=20
    )
    assert [c.chunk_seq for c in chunks] == list(range(1, len(chunks) + 1))


def test_hard_max_structural_guarantee():
    """荒谬参数（overlap > max）下算法仍保证所有 chunk ≤ max_chars。

    物理护栏 ValueError 是防御层，正常路径不可达——算法结构性保证不超限。
    """
    md = "## A\n\n" + "甲" * 100 + "\n\n乙短。"
    chunks = chunk_markdown(
        md, target_chars=10, max_chars=10, min_chars=5, overlap_chars=300
    )
    assert chunks
    assert all(len(c.content) <= 10 for c in chunks)


def test_build_book_card():
    md = "# 我的书\n\n## 第一章 A\n\n内容\n\n## 第二章 B\n\n内容"
    card = build_book_card("我的书", md)
    assert "我的书" in card
    assert "第一章 A" in card
    assert "第二章 B" in card
    assert len(card) <= BOOK_CARD_MAX_CHARS

    long_md = "\n\n".join(f"## 第{i}章 标题{i}" for i in range(500))
    card2 = build_book_card("书名", long_md)
    assert len(card2) <= BOOK_CARD_MAX_CHARS
    assert card2.startswith("书名")


def test_empty_chapter_heading_keeps_context():
    """MinerU 空章节点（章标题后无正文直接进下一节）：

    不产生 chunk、不参与拼装，但后续节的 heading_path 必须带章前缀。
    回归场景：《普通地质学》"## 第一章绪论"后紧跟"## 第一节"——
    修复前空节被 _split_sections 过滤，章上下文丢失，
    前言与第一节跨章拼装（946+1006≈1954 字符）且第一节路径裸奔。
    """
    md = (
        "## 第二版前言\n\n" + "甲" * 600 + "\n\n"
        "## 第一章绪论\n\n"  # 空章节点：标题后直接下一个标题
        "## 第一节 地质学的研究对象\n\n" + "乙" * 600 + "\n\n"
    )
    chunks = chunk_markdown(
        md, target_chars=500, max_chars=700, min_chars=100, overlap_chars=80
    )
    # 章边界强制切块：前言不与第一节拼装
    assert len(chunks) == 2
    assert chunks[0].heading_path == "第二版前言"
    assert chunks[1].heading_path == "第一章绪论 > 第一节 地质学的研究对象"
    assert chunks[1].chunk_seq == 2


# ── Unicode 政策 v2：PUA 净化 / 四口径等长 / 字素簇对齐 / 度量 ──


def test_pua_sanitized_equal_length_four_metrics():
    """PUA → U+FFFD 等长替换：四口径（码点/可见/字节/字素簇）全部等长。"""
    original = "正文" * 50 + "\ue000" + "正文" * 50  # U+E000 私用区
    clean, pos = sanitize_pua(original)
    assert "\ue000" not in clean
    assert "\ufffd" in clean
    assert pos == [100]  # “正文”×50 = 100 码点处
    assert len(clean) == len(original)
    assert visible_len(clean) == visible_len(original)
    assert len(clean.encode("utf-8")) == len(original.encode("utf-8"))
    assert grapheme_count(grapheme_boundaries(clean), 0, len(clean)) == grapheme_count(
        grapheme_boundaries(original), 0, len(original)
    )


def test_sanitize_pua_fast_path_identity():
    """无 PUA 文本零拷贝返回原对象。"""
    text = "普通文本没有私用区字符"
    clean, pos = sanitize_pua(text)
    assert clean is text
    assert pos == []


def test_chunk_has_pua_and_clean_content():
    """chunk 内容净化、has_pua 正确标记。"""
    md = "## 章\n\n" + "正" * 100 + "\ue000" + "文" * 100
    chunks = chunk_markdown(
        md, target_chars=500, max_chars=600, min_chars=10, overlap_chars=30
    )
    assert len(chunks) == 1
    assert "\ue000" not in chunks[0].content
    assert "\ufffd" in chunks[0].content
    assert chunks[0].has_pua is True

    chunks2 = chunk_markdown("## 章\n\n" + "正" * 100, max_chars=600)
    assert chunks2[0].has_pua is False


def test_chunk_metrics_four_consistent():
    """三偏移四长度自洽：纯中文无空白/组合序列时四口径相等，区间不越界。"""
    md = "## 章\n\n" + "地质学的研究对象是地球。" * 30
    chunks = chunk_markdown(
        md, target_chars=200, max_chars=250, min_chars=10, overlap_chars=40
    )
    for c in chunks:
        # 纯中文无空白：码点 = 可见；无组合序列：字素簇 = 码点
        assert c.char_len == c.visible_len == c.grapheme_len
        assert c.byte_len % 3 == 0  # 全 BMP 3 字节字符
        assert 0 < c.char_len <= len(md)
        assert c.char_start + c.char_len <= len(md)
        # overlap chunk：content 含人造 \n\n，覆盖区间长度差 ≤2
        assert len(c.content) - 2 <= c.char_len <= len(c.content)


def test_chunk_offsets_continuous_giant_para():
    """无结构巨段：相邻 chunk 偏移严格连续，拼接还原原文。"""
    body = "字" * 1000
    chunks = chunk_markdown(
        body, target_chars=200, max_chars=250, min_chars=10, overlap_chars=0
    )
    assert len(chunks) == 4
    for i in range(1, len(chunks)):
        assert chunks[i].char_start == chunks[i - 1].char_start + chunks[i - 1].char_len
    assert "".join(c.content for c in chunks) == body
    assert chunks[-1].byte_start + chunks[-1].byte_len == len(body.encode("utf-8"))
    assert chunks[-1].visible_start + chunks[-1].visible_len == visible_len(body)


def test_grapheme_cluster_never_split_on_hard_cut():
    """字符硬切点落在组合序列上时回退到字素簇边界（不劈开 e+◌́）。"""
    body = "字" * 9 + "e\u0301" + "字" * 100
    chunks = chunk_markdown(
        body, target_chars=10, max_chars=10, min_chars=5, overlap_chars=0
    )
    assert "".join(c.content for c in chunks) == body
    for c in chunks:
        assert not c.content.startswith("\u0301")  # 组合标记不得成为 chunk 头
    for c in chunks:
        if "e" in c.content:
            assert "\u0301" in c.content  # e 与 ◌́ 同 chunk
            break
    else:
        raise AssertionError("组合序列丢失")


def test_align_floor_ceil_basic():
    """簇边界对齐工具：floor 回退 / ceil 推进。"""
    b = grapheme_boundaries("e\u0301x")
    assert list(b) == [0, 2, 3]
    assert align_floor(b, 2) == 2
    assert align_floor(b, 1) == 0
    assert align_ceil(b, 1) == 2
    assert align_ceil(b, 3) == 3


def test_visible_len_metrics():
    """可见码点口径：空白不计、标点计、PUA 计。"""
    assert visible_len("甲乙丙丁 空格\te") == 7  # 4 汉字 + 空格 2 字 + e
    assert visible_len("\n\n\r\t\u3000") == 0
    assert visible_len("。，；：？！") == 6
    assert visible_len("\ue000") == 1  # PUA 算可见


def test_nth_visible_index_basic():
    assert nth_visible_index("甲乙 丙", 3) == 4  # 第 3 可见码点 = 丙，位置 4
    assert nth_visible_index("甲乙 丙", 5) == 4  # 不足 → len(text)
    assert nth_visible_index(" 甲乙", 0) == 0


def test_build_book_card_sanitized():
    """书目卡片不携带 PUA（不进 embedding）。"""
    md = "## 章\ue000节\n\n内容"
    card = build_book_card("书名", md)
    assert "\ue000" not in card



# ── PUA 输出级净化 + 清洗前移回归 ─────────────────────────

def test_pua_sanitized_at_chunk_level():
    """PUA 净化在 chunk 输出级：content 为 U+FFFD 版、has_pua=True、
    偏移与入参文本（清洗后全文）精确对齐（等长替换）。"""
    pua = "\ue000"
    md = "## 章\n\n" + "甲" * 50 + pua + "乙" * 50
    chunks = chunk_markdown(
        md, target_chars=500, max_chars=700, min_chars=10, overlap_chars=30
    )
    assert len(chunks) == 1
    c = chunks[0]
    assert pua not in c.content
    assert "\ufffd" in c.content
    assert c.has_pua is True
    # 回查语义：原文窗口（等长净化后）== chunk.content
    window = md[c.char_start : c.char_start + c.char_len]
    assert window.replace(pua, "\ufffd") == c.content


def test_no_pua_fast_path():
    """无 PUA 文本：has_pua=False，content 零损失。"""
    md = "## 章\n\n" + "甲" * 100
    chunks = chunk_markdown(
        md, target_chars=500, max_chars=700, min_chars=10, overlap_chars=30
    )
    assert chunks[0].has_pua is False
    assert "甲" * 100 in chunks[0].content


def test_cleaned_noise_cover_page_no_false_positive():
    """MinerU 封面 OCR 噪音（数字+空格密集、无标题）经 collapse_whitespace
    清洗后切块：不再触发 '码点×2 > 8192' 误杀（清洗前 4345 码点会炸）。"""
    from src.cleaners.text_cleaner import collapse_whitespace

    line = "21117 "
    noise = "DIZHI DA CIDIAN\n" + (line * 3 + "\n") * 400
    cleaned = collapse_whitespace(noise)
    chunks = chunk_markdown(
        cleaned, target_chars=500, max_chars=700, min_chars=10, overlap_chars=30
    )
    assert len(chunks) >= 1
    assert all(visible_len(c.content) <= 700 for c in chunks)
    assert all("  " not in c.content for c in chunks)  # 无残留连续空白


# ── heading_path 截断降级（MinerU 词条当标题 → 列宽兜底）───────
def test_heading_path_truncated_to_column_width():
    """词条类超长标题（401 码点）→ heading_path 截断 ≤512，前缀保留。"""
    long_head = "## " + "进" * 400 + "尾"
    md = long_head + "\n\n正文内容。\n"
    chunks = chunk_markdown(md, target_chars=500, max_chars=700, min_chars=10, overlap_chars=30)
    p = chunks[0].heading_path
    assert len(p) <= 512
    assert p.startswith("进" * 200)
    assert "尾" not in p


def test_heading_truncate_grapheme_safe():
    """截断不劈开字素簇：emoji ZWJ 序列整体保留或整体丢弃。"""
    head = "## " + "甲" * 195 + "👨‍👩‍👧‍👦" + "乙" * 10
    md = head + "\n\n正文。\n"
    chunks = chunk_markdown(md, target_chars=500, max_chars=700, min_chars=10, overlap_chars=30)
    p = chunks[0].heading_path
    assert len(p) <= 512
    assert "\ud800" not in p and "\udfff" not in p  # 无孤立代理
    assert "👨‍👩‍👧‍👦" not in p or p.endswith("👨‍👩‍👧‍👦")  # 簇整体在或整体不在


def test_heading_path_normal_unchanged():
    """短标题路径不受截断影响（零回归）。"""
    md = "# 第一章\n\n## 第一节\n\n正文。\n"
    chunks = chunk_markdown(md, target_chars=500, max_chars=700, min_chars=10, overlap_chars=30)
    assert chunks[0].heading_path == "第一章 > 第一节"


# ── 围栏状态机：代码块内不匹配标题 ─────────────────────────
def test_fenced_code_preprocessor_not_heading():
    """围栏内 C 预处理指令（#include/#endif）不得成为 section 标题。

    回归：Linux-UNIX系统编程手册 第26章 'endif' 伪节导致
    chunk 总码点 15000（可见 2500 + 代码空白 12500）炸物理护栏。
    """
    md = (
        "## 第26章 示例\n\n"
        "```c\n"
        "#include <stdio.h>\n"
        "int main(void) { return 0; }\n"
        "#endif\n"
        "```\n\n"
        "正文段落。\n"
    )
    chunks = chunk_markdown(md, target_chars=500, max_chars=700, min_chars=10, overlap_chars=30)
    assert len(chunks) == 1  # 不产生 include/endif 伪节
    c = chunks[0]
    assert "include" not in c.heading_path
    assert "endif" not in c.heading_path
    assert "#include <stdio.h>" in c.content
    assert "#endif" in c.content


def test_fenced_code_blank_density_no_false_positive():
    """代码块密集文本（缩进+空行）清洗后：总码点不再爆炸。"""
    from src.cleaners.text_cleaner import collapse_whitespace
    code_line = "    fd = open(pathname, flags, mode);          /* system call */\n"
    md = "## 第26章\n\n```c\n" + code_line * 300 + "```\n"
    cleaned = collapse_whitespace(md)
    chunks = chunk_markdown(cleaned, target_chars=2000, max_chars=2500, min_chars=400, overlap_chars=300)
    assert len(chunks) >= 1
    for c in chunks:
        assert len(c.content) <= 8192  # 物理护栏：总码点（token 安全上界）


# ── 双口径切块：可见字素簇 + 总码点物理上限 ────────────────
def test_dual_physical_codepoint_limit():
    """高空白密度文本（缩进代码+对齐注释）：总码点 8000 先到时提前切。

    回归：TLPI '程序清单 26-3' 节 20611 码点/4047 可见（~5 倍空白），
    按可见 2500 切 → 总码点 12750 炸物理护栏。双口径后按码点先到切。
    """
    from src.cleaners.text_cleaner import collapse_whitespace
    # 每行 1 可见 + 34 空白（~35 倍空白密度，总码点必然先到 8000）
    line = "    x" + " " * 30 + "\n"
    md = "## 第26章\n\n```c\n" + line * 500 + "```\n"
    cleaned = collapse_whitespace(md)
    chunks = chunk_markdown(cleaned, target_chars=2000, max_chars=2500, min_chars=400, overlap_chars=300)
    assert len(chunks) >= 1
    for c in chunks:
        assert len(c.content) <= 8000  # MAX_PHYSICAL_CODEPOINTS
