"""
文件名: tokenizer.py
摘要: 共享分词工具。封装 jieba 中文分词，检测 CJK 字符。
      所有导入器和搜索器使用同一套分词逻辑。
依赖: jieba>=0.42
"""

from __future__ import annotations

import re
from typing import NoReturn

# CJK 字符范围（Unicode）
_CJK_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3000-\u303f\uff00-\uffef]"
)

# 标准空白分隔（英文/ASCII 文本的分词模式）
_WHITESPACE_RE = re.compile(r"\s+")

_jieba_available: bool | None = None


def _check_jieba() -> bool:
    """检查 jieba 是否可导入。结果缓存。"""
    global _jieba_available
    if _jieba_available is not None:
        return _jieba_available
    try:
        import jieba  # noqa: F401
        _jieba_available = True
    except ImportError:
        _jieba_available = False
    return _jieba_available


def has_cjk(text: str) -> bool:
    """检测文本是否包含 CJK（中日韩统一表意文字）字符。"""
    return bool(_CJK_RE.search(text))


def tokenize(text: str) -> str:
    """对文本进行分词，返回空格分隔的 token 字符串。

    - CJK 文本 → jieba 分词（如 jieba 不可用则降级为原始文本）
    - 非 CJK 文本 → 空格分词（不变）
    """
    if not text.strip():
        return ""

    if has_cjk(text):
        if _check_jieba():
            import jieba
            return " ".join(jieba.cut(text))
        else:
            # jieba 未安装：降级——将每个字符单独分，聊胜于无
            import warnings
            warnings.warn(
                "jieba 未安装，CJK 分词降级为逐字符分割。"
                "运行: uv pip install jieba"
            )
            return " ".join(text)
    else:
        # 非 CJK：标准空格分词，去空白归一化
        return " ".join(text.split())


def tokenize_query(query: str) -> str:
    """对搜索查询进行分词，返回 MATCH...AGAINST BOOLEAN MODE 格式。

    CJK 查询 → jieba 分词后每词加 + 和 *（必须包含、前缀匹配）
    非 CJK → 按空格分词后每词加 + 和 *
    """
    if not query.strip():
        return ""

    if has_cjk(query):
        if _check_jieba():
            import jieba
            words = jieba.lcut(query)
        else:
            # 降级：逐字
            words = list(query)
    else:
        words = query.split()

    # 过滤单字符非字母数字（标点、空格等）
    tokens: list[str] = []
    for w in words:
        w = w.strip()
        if not w:
            continue
        if len(w) == 1 and not w.isalnum():
            continue
        if len(w) >= 2:
            tokens.append(f"+{w}*")
        elif w.isalnum():
            tokens.append(f"+{w}*")

    return " ".join(tokens)
