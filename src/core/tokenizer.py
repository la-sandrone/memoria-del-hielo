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

# ── CJK 全文检索口径（unigram + bigram）用的「纯汉字」范围 ──────────────
# 与上面的 _CJK_RE 不同：那里含标点/全角符号（用于"这段文本算不算 CJK"），
# 这里只取汉字——标点成 token 会污染索引。
_CJK_HAN = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
# ★ 2026-10-25：非 CJK 分支原来只认 `[A-Za-z0-9_]+`，西里尔/希腊/带变音拉丁
#   匹配不到 ⇒ 整段丢弃（古典学专著里的俄文成批空白）。改为两段 alternation，
#   顺序即优先级：① CJK → unigram+bigram；② \w+ → 所有 Unicode 字母整词保留。
_CJK_CHUNK_RE = re.compile(f"([{_CJK_HAN}]+)|(\\w+)", re.UNICODE)
_CJK_HAN_RE = re.compile(f"[{_CJK_HAN}]")

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


# ══════════════════════════════════════════════════════════════════════════
# CJK 全文检索口径：unigram + bigram（2026-10-25 起，**不走 jieba**）
# ══════════════════════════════════════════════════════════════════════════

def tokenize_cjk_ub(text: str) -> str:
    """把文本切成 CJK unigram + bigram 串（写入 FULLTEXT 索引用）。

    规则：
      · 连续汉字串 → **每个汉字**一个 token + **每对相邻汉字**一个 token
        "绘图工具" → 绘 图 工 具 绘图 图工 工具
      · 英文/数字/下划线串 → 原样一个 token
      · 其余字符（标点等）→ 丢弃

    为什么不用 jieba（上面的 tokenize()）：
      依据 Lucene `CJKBigramFilter(outputUnigrams=true)` 与 Elasticsearch 内置
      `cjk_bigram`；学术侧见 SIGIR'98《Comparing Representations in Chinese
      Information Retrieval》。bigram 与词切分效果相当甚至更好，且**不依赖词典**。
      jieba 会把专名/新词/代码标识符切错（实测：62 万 chunk 里搜「会话」，
      jieba 口径 78 命中 vs 原文 111 条——差的 33 行是被切成「会 话」）。

    为什么必须带 unigram：只 bigram 时搜「绘」找不到「绘图」（索引里没有 1 字符 token）。

    ★ 前置条件：MariaDB 侧 `innodb_ft_min_token_size = 1`（已配）。
      取默认值 3 时 1-2 字符 token 在索引阶段被静默丢弃。
    """
    if not text:
        return ""

    out: list[str] = []
    for cjk, other in _CJK_CHUNK_RE.findall(text):
        chunk = cjk or other
        if cjk:
            out.extend(chunk)                                          # unigram
            out.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))   # bigram
        else:
            out.append(chunk)
    return " ".join(out)


def build_cjk_boolean(query: str) -> str:
    """把用户查询转成 MATCH...AGAINST 的 BOOLEAN 表达式（写入侧口径的镜像）。

    · 单字 CJK → `+字`
    · 多字 CJK → 全部 bigram 用 AND 串起来（"+绘图 +图工 +工具"）
    · 英文/数字 → `+原词`

    与 tokenize_cjk_ub 必须保持同一套切分规则，否则查询侧拼出的 token
    在索引里根本不存在（症状：明明有却搜不到，且不报任何错）。
    """
    terms: list[str] = []
    for cjk, other in _CJK_CHUNK_RE.findall(query or ""):
        chunk = cjk or other
        if cjk:
            if len(chunk) == 1:
                terms.append("+" + chunk)
            else:
                terms.extend("+" + chunk[i:i + 2] for i in range(len(chunk) - 1))
        else:
            terms.append("+" + chunk)
    return " ".join(terms)
