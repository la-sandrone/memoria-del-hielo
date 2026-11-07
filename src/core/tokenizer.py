"""
文件名: tokenizer.py
摘要: 共享分词工具。封装 jieba 中文分词，检测 CJK 字符。
      所有导入器和搜索器使用同一套分词逻辑。
依赖: jieba>=0.42
"""

from __future__ import annotations

import re
from collections.abc import Iterator
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


# ══════════════════════════════════════════════════════════════════════════
# 分词 v2（2026-11-07）：修「拉丁紧邻汉字被 \w+ 整段吞掉」
# ══════════════════════════════════════════════════════════════════════════
#
# v1 的正则是 `([汉]+)|(\w+)`。`\w` 在 Unicode 模式下**包含汉字**，
# 所以遇到 "JDBC接口" 这种拉丁紧邻汉字的串，左分支在位置 0 失败、右分支
# `\w+` 把**整串**吃掉 —— 只产出一个 token "JDBC接口"，里面的汉字既无
# unigram 也无 bigram。症状：那些汉字在索引里等于不存在，搜「接口」永远
# 0 命中，而且**不报任何错**。
#
# 实测（本项目 documents_book_chunk 的 723 行分层抽样）：
#   142/723 = 19.6% 的行含这种「被吞掉」的串。
# melquiades 那边同一条正则、同一个病：39,409 行 = 19.1%。
#
# v2 的做法：**先按汉字/非汉字切段**，再各套各的规则。汉字段永远是汉字段，
# 不给 \w+ 机会把它吞掉。
_SEG_RE = re.compile(f"[{_CJK_HAN}]+|[^{_CJK_HAN}]+")
_CJK_SEG_RE = re.compile(f"[{_CJK_HAN}]+")
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def iter_chunks(text: str) -> Iterator[tuple[bool, str]]:
    """把文本切成 (是否汉字段, 段)；汉字段与非汉字段交替，覆盖全文。

    唯一权威切分点：写入侧 tokenize_cjk_ub_v2 与查询侧 build_cjk_query_v2
    都必须经过这里，否则两侧口径会漂 —— 漂了就是「明明有却搜不到」。
    """
    for m in _SEG_RE.finditer(text or ""):
        seg = m.group(0)
        yield (True, seg) if _CJK_SEG_RE.fullmatch(seg) else (False, seg)


def tokenize_cjk_ub_v2(text: str) -> str:
    r"""写入侧：汉字段 → unigram + bigram；非汉字段 → \w+ 整词。

    与 v1 的差异只在「混合串」上：
      v1  "JDBC接口测试" → "JDBC接口测试"          （1 个 token，汉字不可检索）
      v2  "JDBC接口测试" → "JDBC 接 口 测 试 接口 口测 测试"
      v1  "第3章节"      → "第 3章节"
      v2  "第3章节"      → "第 3 章 节 章节"
    纯汉字段与纯拉丁段的输出与 v1 完全一致（已用 12 类语料对拍）。
    """
    if not text:
        return ""
    out: list[str] = []
    for is_cjk, seg in iter_chunks(text):
        if is_cjk:
            out.extend(seg)
            out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
        else:
            out.extend(_WORD_RE.findall(seg))
    return " ".join(out)


# 停词表：只挡「几乎每篇都有」的词。汉字侧按**单字**收录，bigram 里含停词
# 字就丢（"的世"/"没的"这类不携带区分度，留在查询里只会拉平 NL 打分）。
DEFAULT_STOPWORDS: frozenset[str] = frozenset(
    "的 了 和 与 在 是 我 你 他 她 它 这 那 有 为 以 及 等 个 之 其 并 或 而 "
    "被 把 就 都 也 很 到 说 要 会 着 过 对 从 向 于 中 上 下 不 没 无 又 才 "
    "the a an of and or in on at to for is are was were be been it its this that".split()
)


def load_stopwords() -> frozenset[str]:
    """停词表：config.toml 的 [search].stopwords（逗号分割）覆盖内置默认。"""
    try:
        from .config import get_config
        raw = get_config().get("search.stopwords", "")
    except Exception:
        return DEFAULT_STOPWORDS
    words = {w.strip().lower() for w in str(raw or "").split(",") if w.strip()}
    return frozenset(words) if words else DEFAULT_STOPWORDS


def _is_stop(term: str, stopwords: frozenset[str]) -> bool:
    if term.lower() in stopwords:
        return True
    # 汉字侧**只有整词的每个字都是停词才算停词**（"的了" / "是在"）。
    # 不能"任一单字命中就丢" —— 会误杀真词：会话 / 中国 / 邮编 / 对话 / 上学，
    # 这些 bigram 里都含高频虚字。拉丁侧压根不按字符判（"Carter" 含 a）。
    if _CJK_SEG_RE.fullmatch(term):
        return len(term) > 1 and all(ch in stopwords for ch in term)
    return False


def build_cjk_query_v2(query: str) -> str:
    """查询侧（NATURAL LANGUAGE MODE 用）：汉字段只取 bigram，非汉字段取整词。

    三条都要，缺一条就回到「搜不到」：
      1. **只 bigram、不取 unigram** —— unigram 几乎命中每个 chunk，
         NL 打分被拉平；查询侧加 unigram 只降不升。
      2. 与写入侧共用 iter_chunks —— 口径不漂。
      3. 不拼 `+` / `*` —— 那是 BOOLEAN MODE 的语法，NL 模式下会被当普通字符。

    实测（本项目真实查询，BOOLEAN AND → 本函数 + NL MODE）：
      「卡尔萨根 魔鬼出没的世界 科学 迷信 伪科学」        0 → 41,387，top-1 对
      「克尔度规 零测地线 Carter 常数 分离变量」          0 → 17,549，top-1 对
      「较差自转 罗斯贝数 行星 流体 对流 泰勒普劳德曼 纬向流」 0 → 12,828，top-1 对
      「helium 3D excitation electron impact cross section」   0 → 14,260
    """
    kept, _ = query_term_split(query)
    return " ".join(kept)


def query_term_split(query: str) -> tuple[list[str], list[str]]:
    """把查询切成 (保留词, 被剪词)，两者都去重保序。

    **唯一权威切分点**：build_cjk_query_v2 / build_cjk_query_raw 都走这里，
    剪枝的**可见性**（search_meta.query_terms）也来自这里 —— 剪枝必须是可审计对象，
    不能是藏在常量里的隐式行为。

    两条纪律：
      1. 剪枝只允许削弱精度，永远不允许削弱召回 —— 若全被剪光，退回未剪词表。
         返回空串会让全文路直接 return []，调用方看到的是「库里没有」，那是静默漏召回。
      2. 词表粒度与 token 粒度对齐：字符级停词只判单字 token 与「整词全由虚字构成」
         （见 _is_stop）。会话/中国/基金 含虚字但是真词，不许丢。
    """
    terms = _raw_terms(query)
    if not terms:
        return [], []
    stop = load_stopwords()
    kept = [t for t in terms if not _is_stop(t, stop)]
    pruned = [t for t in terms if _is_stop(t, stop)]
    # 全被剪光 → 退回未剪词表；**但 pruned 照样带出去**，否则「剪得最狠的那次」
    # 恰好是唯一不告警的一次（调用方用 len(pruned) >= len(kept) 判异常）。
    return (kept or terms), pruned


def _raw_terms(query: str) -> list[str]:
    """未剪枝的词表（去重保序）。

    去重是必要的：同一个 bigram 会出现多次（"基金会史 基金会历史" → 基金 出现两次），
    重复词会让 NL 打分的词频虚高。
    """
    if not (query or "").strip():
        return []
    terms: list[str] = []
    for is_cjk, seg in iter_chunks(query):
        if is_cjk:
            if len(seg) == 1:
                terms.append(seg)
            else:
                terms.extend(seg[i:i + 2] for i in range(len(seg) - 1))
        else:
            terms.extend(_WORD_RE.findall(seg))
    return list(dict.fromkeys(terms))


def build_cjk_query_raw(query: str) -> str:
    """**不剪枝**的查询串：全文路 0 命中时的第二层重试（剪枝可能是漏召回的原因）。"""
    return " ".join(_raw_terms(query))
