"""
测试: cleaners/text_cleaner.py — 空白规范化（collapse_whitespace）
"""

from src.cleaners.text_cleaner import collapse_whitespace


def test_inline_whitespace_collapsed():
    """行内连续空白（半角/制表符/全角）→ 单空格。"""
    assert collapse_whitespace("甲  乙\t丙\u3000丁") == "甲 乙 丙 丁"


def test_trailing_whitespace_removed():
    """行尾空白删除（含 \r，天然统一换行为 \n）。"""
    assert collapse_whitespace("甲  \n乙  \r\n") == "甲\n乙\n"


def test_leading_indent_preserved():
    """行首缩进保留（Markdown 列表/缩进代码语义）。"""
    assert collapse_whitespace("  甲  乙") == "  甲 乙"
    assert collapse_whitespace("\t甲  乙") == "\t甲 乙"


def test_blank_lines_collapsed():
    """连续空行（≥3）→ 2。"""
    assert collapse_whitespace("甲\n\n\n\n乙") == "甲\n\n乙"
    assert collapse_whitespace("甲\n\n乙") == "甲\n\n乙"  # 2 个不动


def test_fenced_code_preserved():
    """围栏代码块内部原样保留（缩进是语义）。"""
    src = "```python\nif  a  ==  1:\n    pass\n```\n正文  甲"
    assert collapse_whitespace(src) == "```python\nif  a  ==  1:\n    pass\n```\n正文 甲"


def test_fence_unbalanced_rest_preserved():
    """围栏未闭合：剩余部分全部视为代码块（防御行为，不破坏内容）。"""
    src = "```\n甲  乙"
    assert collapse_whitespace(src) == src


def test_fullwidth_space_inline():
    """全角空格 U+3000 行内折叠为半角。"""
    assert collapse_whitespace("甲\u3000\u3000乙") == "甲 乙"


def test_idempotent():
    """幂等：对已折叠文本重复调用无副作用。"""
    src = "甲  乙\n\n\n丙  \n"
    once = collapse_whitespace(src)
    assert collapse_whitespace(once) == once


def test_empty_and_whitespace_only():
    """空文本不变；纯空白文本 → 空行序列（re.sub 只压缩 ≥3 连续换行）。"""
    assert collapse_whitespace("") == ""
    assert collapse_whitespace("   \n \n  ") == "\n\n"


def test_noise_cover_page_lines():
    """MinerU 封面 OCR 噪音特征行：行尾空格删除、行内空白折叠。"""
    src = "DIZHI DA CIDIAN  \n2005 623  14400.920° 171017\n"
    assert collapse_whitespace(src) == "DIZHI DA CIDIAN\n2005 623 14400.920° 171017\n"


def test_fence_inner_trailing_ws_removed():
    """代码块内：行尾空白删、连续空行压缩、行首缩进保留（缩进是语义）。"""
    src = "```c\nint x = 5;   \n\n\n    if (x) {  \n```\n"
    out = collapse_whitespace(src)
    assert out == "```c\nint x = 5;\n\n    if (x) {\n```\n"


def test_fence_inner_idempotent():
    src = "```\n    a = 1   \n\n\n    b = 2\t\n```\n"
    once = collapse_whitespace(src)
    assert collapse_whitespace(once) == once
