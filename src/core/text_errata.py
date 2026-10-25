"""
文件名: text_errata.py
摘要: 书级「文本误差统计」的唯一权威实现 —— 判据 / 词表 / 合并 / 渲染都在本文件。
      落 documents_book.errata_stats（JSON 列，存纯数据，不存散文）。

      **为什么是一列 JSON 而不是一堆列**：误差的种类是开放的（形近串写、断词、
      回吐语料、将来还不知有什么），一列一种就无穷无尽。JSON 列装得下未知，
      代价是丢了「按列聚合」——所以计数键名必须稳定，且**词表与判据同源**
      （SIGNAL_GLOSSARY 就在判据旁边），否则措辞会与代码漂移。

      **自文档化的契约**：DB 只存 `{key: 计数}`；每个键的释义由本文件给出，
      在渲染时（render_errata_note / errata_payload）按**实际触发的键**贴出。
      外部（manifest）可以新增键，但必须自带释义写入 `glossary`，否则渲染时
      只会标成「未登记信号」——**不许静默透传一个 LLM 看不懂的键**。

      **判据的输入口径**：一律是**落库后的 documents_book.content**
      （= 清洗后的正文）。这条很重要——只有输入是同一个字符串，
      「导入时算的」与「事后从 content 回填的」才逐位相同，不会长出两套口径。
      实测依据：fold_kangxi_radicals 不在导入路径上（content 保留康熙部首）；
      collapse_whitespace 保留 \\n（行尾断词信号存活）。

      ⚠️ 计数 0 与「未计算」是两件事：
         {}                      未计算（迁移 007 之前的存量书）
         {"criteria_version":...} 已计算，0 就是真的 0
         not_applicable 里的键   判据不适用（如中文书上的 cjk_halluc），**不是 0**
依赖: ./unicode_utils（count_unicode_risk —— 康熙部首 / PUA，复用不重写）
"""

from __future__ import annotations

import re
from typing import Any

from .unicode_utils import count_unicode_risk

# 判据版本。判据一改就升版本（D9 范式：不一致就不一致，但要说清是哪个口径）。
#   v1（2026-10-06 起）：script_mix / hyphen_break / cjk_halluc + 上卷的 kangxi_radical / pua
CRITERIA_VERSION = "v1"

# ── 形近字母映射（拉丁 / 希腊 → 西里尔）──────────────────────────
# 只收形状确实相近的，**不做语音猜测**。用途是把词折到西里尔，好认出
# 「Φacmep 就是 Фасмер」。方向单向：拉丁/希腊折向西里尔。
CONFUSE: dict[str, str] = {
    "a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х", "y": "у",
    "k": "к", "t": "т", "n": "п", "b": "ь", "s": "ѕ", "i": "і", "j": "ј",
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У", "N": "П", "S": "Ѕ",
    "I": "І", "J": "Ј",
    "\u03b1": "а", "\u03b5": "е", "\u03bf": "о", "\u03c1": "р", "\u03ba": "к",
    "\u03c4": "т", "\u03bd": "п", "\u03c5": "у", "\u03c7": "х", "\u03c3": "с",
    "\u03b9": "і", "\u03bc": "м", "\u0391": "А", "\u0392": "В", "\u0395": "Е",
    "\u039f": "О", "\u03a1": "Р", "\u03a4": "Т", "\u03a7": "Х", "\u03a6": "Ф",
    "\u03a0": "П", "\u039a": "К", "\u039c": "М", "\u039d": "П", "\u0399": "І",
    "\u00e1": "а", "\u00e9": "е", "\u00f3": "о", "\u00fa": "у", "\u00fd": "ы",
    "\u00ed": "і", "\u014d": "о", "\u0101": "а", "\u0113": "е",
}

# 含拉丁预组合重音元音的词（如 пахáть 里的 á）单独计数：它们不是「抄错字母」，
# 而是**重音的表示形式**选错了（该用 西里尔元音+U+0301），但同样打断字面检索。
ACCENTED_LATIN = frozenset("\u00e1\u00e9\u00f3\u00fa\u00fd\u00ed\u014d\u0101\u0113")

_SCRIPT_RANGES: tuple[tuple[str, tuple[tuple[int, int], ...]], ...] = (
    ("cyr", ((0x0400, 0x052F), (0x2DE0, 0x2DFF), (0xA640, 0xA69F))),
    ("lat", ((0x0041, 0x005A), (0x0061, 0x007A), (0x00C0, 0x024F), (0x1E00, 0x1EFF))),
    ("grk", ((0x0370, 0x03FF), (0x1F00, 0x1FFF))),
)

_CYR_RE = re.compile("[\u0400-\u052f\u2de0-\u2dff\ua640-\ua69f]")
_CJK_RE = re.compile("[\u4e00-\u9fff\u3400-\u4dbf]")
# 连成串的汉字才算回吐；单个孤立汉字多为希腊字母误识（实测 θε 被读成 戈）
_CJK_RUN_RE = re.compile("[\u4e00-\u9fff\u3400-\u4dbf]{2,}")
# 行尾连字符未合并：дву-\nсмыслен
_HYPHEN_BREAK_RE = re.compile(r"[^\W\d_]-[ \t]*\n[ \t]*[^\W\d_]", re.UNICODE)
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# 体裁闸门：正文里汉字占比超此值就**不启用** cjk_halluc（中文书整本都是汉字，
# 不设闸门会全线误报）。跳过 ≠ 0，跳过的键进 not_applicable。
CJK_DOMINANT_THRESHOLD = 0.05

# ── 判据词表：与判据同源，渲染时按实际触发的键取用 ──────────────
# 新增判据必须同时在此登记；manifest 引入的外部键自带释义（另存 glossary）。
SIGNAL_GLOSSARY: dict[str, dict[str, str]] = {
    "script_mix": {
        "label": "形近字母串写",
        "unit": "词",
        "what": "同一个词里混了两种文字系统（拉丁/希腊字母被当成西里尔写出来）",
        "eg": "nуть 实为 путь、Φacmep 实为 Фасмер、typeц 实为 турец",
        "harm": "字面检索漏召回：搜 путь 命中不了 nуть",
    },
    "accented_latin": {
        "label": "拉丁重音元音代替",
        "unit": "词",
        "what": "重音本该用 西里尔元音+U+0301，却写成了拉丁预组合重音元音",
        "eg": "пахáть（á 是 U+00E1 拉丁字母，应为 а+U+0301）",
        "harm": "同上：字面检索漏召回；且是「串写」在重音字典里高发的原因",
    },
    "hyphen_break": {
        "label": "行尾连字符未合并",
        "unit": "处",
        "what": "原书行末断词，产出没把两头接回去",
        "eg": "дву-/смыслен 实为 двусмысленный、учи-/телей 实为 учителей",
        "harm": "词被切成两半，词表与检索都认不出",
    },
    "cjk_halluc": {
        "label": "成串汉字（疑似回吐训练语料）",
        "unit": "处",
        "what": "非中文书里出现连成串的汉字——实测是模型在不可读区域不返回空值，"
                "而回吐训练语料里的中文样本；也有字符级误识（дне 被读成 江南）",
        "eg": "Посхѣ江南ее（实为 Послѣднее）、公式里 $\\mathbf{e}_{孔国国国国国}$",
        "harm": "正文被插入无关内容，且不可辨伪",
    },
    "kangxi_radical": {
        "label": "康熙部首码点",
        "unit": "个",
        "what": "字体映射缺陷的显式表现：看着是「月」，码点却是 U+2F49「⽉」",
        "eg": "⽉背征途",
        "harm": "检索用「月球」匹配不到「⽉球」",
    },
    "aside_halluc": {
        "label": "页边幻觉块",
        "unit": "个",
        "origin": "audit",
        "what": "产物里被判定为「回吐训练语料」的块数（中文年报范本 / 化学式 / "
                "英文 pangram）。模型拿到不可读区域时**不返回空值**，而是编一个出来",
        "eg": "页边竖带被读成「1. 2023年，公司与上海华谊（集团）…」",
        "harm": "这些块**不进 markdown.md**（MinerU 会整体丢弃 aside_text），所以只看正文"
                "看不见它；但它标出了「这一本 VLM 状态不稳」，是正文污染的前兆"
                "（实测与 markdown 汉字串的 Spearman = 0.595，且不冗余）",
    },
    "pages_short": {
        "label": "产出页数短少",
        "unit": "页",
        "origin": "audit",
        "what": "产出页数少于源文件页数的部分（按源 djvu/pdf 的页数对账）",
        "eg": "",
        "harm": "可能被截断；引用时先确认末尾章节在不在",
    },
    "pua": {
        "label": "私用区字符",
        "unit": "个",
        "what": "PUA 码点（U+E000–U+F8FF），正常文本里不该出现",
        "eg": "",
        "harm": "字面无意义；回查原文时按 has_pua 走 SUBSTRING 净化路径",
    },
}

# 机械计数键（可覆盖、可重算）；notes 是累加型，不在此表。
_COUNT_KEYS: tuple[str, ...] = (
    "chars", "words", "script_mix", "accented_latin", "hyphen_break",
    "cjk_halluc", "kangxi_radical", "pua",
    # 下面两个**不是** count_errata 算的，只有离线审计（tools/audit_zip.py）能算出——
    # 它要看 middle_json 的块结构、要和源文件对账页数，而导入器手里只有那个 md。
    # 走 manifest（mhi annotate）进来，词表在此统一登记，免得每行都要自带 glossary。
    "aside_halluc", "pages_short",
)

# 由外部（离线审计）供给的信号键。count_errata 绝不产出它们——
# 产出了就说明判据越界了（导入器看不到 middle_json，算不出真值）。
EXTERNAL_SIGNAL_KEYS: tuple[str, ...] = ("aside_halluc", "pages_short")

# 子计数：挂在某个主信号底下显示，不单独占一行（否则「串写 233」与
# 「重音 29」并列会被读成两件事，其实是同一件的一部分）。
_DETAIL_KEYS: dict[str, str] = {"accented_latin": "script_mix"}

# 需要显示「占词百分比」的信号（分母是 words）
_RATIO_SIGNALS: tuple[str, ...] = ("script_mix",)


def is_computed(errata: Any) -> bool:
    """`{}` 是唯一的「未计算」标记；任何非空 dict 都算有记录。

    刻意**不要求 criteria_version 存在**：manifest 可以只塞外部注记，
    那种书若被当成「未计算」，注记就会被静默丢掉 —— 实测过的坑。
    """
    return isinstance(errata, dict) and bool(errata)


def _script_of(ch: str) -> str | None:
    o = ord(ch)
    for name, ranges in _SCRIPT_RANGES:
        for lo, hi in ranges:
            if lo <= o <= hi:
                return name
    return None


def _normalize_word(word: str) -> str:
    return "".join(CONFUSE.get(c, c) for c in word)


def count_errata(text: str) -> dict[str, Any]:
    """按 v1 判据统计一本书的误差，返回可 JSON 序列化的 dict。

    输入必须是**将要落库的那个字符串**（即 collapse_whitespace 之后、
    进 documents_book.content 的正文），否则导入时算的与事后回填的对不上。
    """
    chars = len(text)
    script_mix = 0
    accented_latin = 0
    words = 0
    letters = 0
    for word in _WORD_RE.findall(text):
        words += 1
        scripts: set[str] = set()
        has_accented = False
        for ch in word:
            s = _script_of(ch)
            if s is not None:
                scripts.add(s)
                letters += 1
            if ch in ACCENTED_LATIN:
                has_accented = True
        if len(scripts) > 1:
            script_mix += 1
        # acented_latin 只算**西里尔词里混进拉丁重音元音**（пахáть）。
        # 纯拉丁词（café / études）本来就是拉丁文，不算误差。
        if has_accented and "cyr" in scripts:
            accented_latin += 1

    # 汉字单独数：_WORD_RE 的 [^\W\d_] 把 CJK 也算字母（都是 alnum），
    # 但 _script_of 不认 CJK，所以上面 letters 里没含汉字，这里补上供体裁闸门用。
    cjk_total = len(_CJK_RE.findall(text))
    letters += cjk_total
    cjk_ratio = (cjk_total / letters) if letters else 0.0

    hyphen_break = len(_HYPHEN_BREAK_RE.findall(text))
    kangxi, pua = count_unicode_risk(text)

    out: dict[str, Any] = {
        "criteria_version": CRITERIA_VERSION,
        "chars": chars,
        "words": words,
        "script_mix": script_mix,
        "accented_latin": accented_latin,
        "hyphen_break": hyphen_break,
        "kangxi_radical": kangxi,
        "pua": pua,
    }
    if cjk_ratio > CJK_DOMINANT_THRESHOLD:
        # 体裁闸门：中文书不启用该判据。跳过 ≠ 0，必须显式登记。
        out["cjk_halluc"] = 0
        out["not_applicable"] = ["cjk_halluc"]
    else:
        out["cjk_halluc"] = len(_CJK_RUN_RE.findall(text))
    return out


def merge_errata(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
    source: str,
    *,
    note: str | None = None,
    glossary: dict[str, str] | None = None,
) -> dict[str, Any]:
    """把 incoming 合并进 existing，返回新的 errata dict（纯函数，不改入参）。

    合并策略是**显式按键**的，不依赖 SQL 的 JSON_MERGE_* 语义：
      · 计数键（_COUNT_KEYS）→ 覆盖（机械化事实，后来者更准）
      · criteria_version     → 覆盖
      · not_applicable       → 覆盖
      · notes                → 追加（去重：同 (source,text) 不重复累加）
      · glossary             → 并入（外部键的释义）
      · 其余未知键           → 拒绝，写进 `unregistered`，**不静默透传**

    source: 谁给的这批数（写进 notes / 溯源）。空串表示导入器自己算的。
    """
    out: dict[str, Any] = dict(existing or {})
    inc = dict(incoming or {})

    for key in _COUNT_KEYS:
        if key in inc:
            out[key] = inc.pop(key)
    if "criteria_version" in inc:
        out["criteria_version"] = str(inc.pop("criteria_version"))
    if "not_applicable" in inc:
        na = inc.pop("not_applicable") or []
        out["not_applicable"] = sorted({str(x) for x in na})

    if glossary:
        merged_gloss = dict(out.get("glossary") or {})
        merged_gloss.update({str(k): str(v) for k, v in glossary.items()})
        out["glossary"] = merged_gloss

    unregistered: list[dict[str, Any]] = list(out.get("unregistered") or [])
    gloss_now = out.get("glossary") or {}
    for key in sorted(inc):
        # 键要么在词表里（有释义），要么自带释义，否则不认。
        if key in SIGNAL_GLOSSARY or key in gloss_now:
            out[key] = inc[key]
        else:
            unregistered.append({"key": key, "value": inc[key], "source": source})
    if unregistered:
        out["unregistered"] = unregistered

    if note:
        notes = list(out.get("notes") or [])
        if not any(n.get("source") == source and n.get("text") == note for n in notes):
            notes.append({"source": source, "text": note})
        out["notes"] = notes

    return out


def _ratio_pct(num: int, den: int) -> str:
    if not den:
        return ""
    pct = 100.0 * num / den
    return "%.2f%%" % pct if pct < 1 else "%.1f%%" % pct


def fired_signals(errata: dict[str, Any]) -> list[str]:
    """按词表顺序列出「真的有值」的信号键（供渲染与测试用）。"""
    na = set(errata.get("not_applicable") or [])
    out = []
    for key in SIGNAL_GLOSSARY:
        if key in na or key in _DETAIL_KEYS:
            continue
        val = errata.get(key)
        if isinstance(val, int) and val > 0:
            out.append(key)
    return out


def errata_payload(errata: dict[str, Any], parse_mode: str = "") -> dict[str, Any]:
    """给 MCP 返回用的自文档化 payload：计数 + 逐个触发键的释义。

    只对**实际触发的键**贴释义——省 token，且释义与判据同源，不可能过期。
    """
    if not is_computed(errata):
        return {}
    payload: dict[str, Any] = {
        "criteria_version": errata.get("criteria_version"),
        "counts": {k: errata[k] for k in _COUNT_KEYS if k in errata},
    }
    if errata.get("not_applicable"):
        payload["not_applicable"] = list(errata["not_applicable"])
    if errata.get("notes"):
        payload["notes"] = list(errata["notes"])

    gloss: dict[str, Any] = {}
    for key in fired_signals(errata):
        g = SIGNAL_GLOSSARY[key]
        gloss[key] = {"label": g["label"], "what": g["what"], "harm": g["harm"]}
        if g.get("eg"):
            gloss[key]["eg"] = g["eg"]
    # 触发了主信号就把它的子计数释义也带上（渲染里会出现「其中 N 词属…」）
    for dkey, parent in _DETAIL_KEYS.items():
        if parent in gloss and errata.get(dkey):
            dg = SIGNAL_GLOSSARY[dkey]
            gloss[dkey] = {"label": dg["label"], "what": dg["what"],
                           "harm": dg["harm"], "detail_of": parent}
            if dg.get("eg"):
                gloss[dkey]["eg"] = dg["eg"]
    # 外部键：只贴自带释义的；没释义的绝不静默透传
    for key, val in errata.items():
        if key in SIGNAL_GLOSSARY or key in _COUNT_KEYS:
            continue
        if key in ("criteria_version", "not_applicable", "notes", "glossary",
                   "unregistered"):
            continue
        own = (errata.get("glossary") or {}).get(key)
        if own:
            gloss[key] = {"label": key, "what": own}
        elif val:
            gloss[key] = {"label": key, "what": "（未登记的信号，本文件无释义，值仅供参考）"}
    if gloss:
        payload["glossary"] = gloss
    return payload


def render_errata_note(errata: dict[str, Any], parse_mode: str = "") -> str:
    """把 errata 渲染成给 LLM 的一句话（+ 触发键的释义行）。无信号则返回 ""。"""
    if not is_computed(errata):
        return (
            "⚠️ 本书无误差统计记录（迁移 007 之前导入，或非本库算的）。"
            "计数为 0 表示「未记录」而非「无误差」；引用生僻字/符号前先看上下文。"
        )

    how = {"txt": "文本层读数", "ocr": "视觉读数", "unknown": "读数方式未知"}.get(
        parse_mode, parse_mode or "读数方式未记录"
    )
    fired = fired_signals(errata)
    own_keys = [
        k for k, v in errata.items()
        if k not in SIGNAL_GLOSSARY and k not in _COUNT_KEYS
        and k not in ("criteria_version", "not_applicable", "notes",
                      "glossary", "unregistered")
        and isinstance(v, int) and v > 0
    ]
    if not fired and not own_keys and not errata.get("notes"):
        return ""

    words = errata.get("words") or 0
    bits: list[str] = []
    for key in fired:
        g = SIGNAL_GLOSSARY[key]
        val = errata[key]
        qual: list[str] = []
        if key in _RATIO_SIGNALS and words:
            pct = _ratio_pct(val, words)
            if pct:
                qual.append("占词 %s" % pct)
        for dkey, parent in _DETAIL_KEYS.items():
            dval = errata.get(dkey)
            if parent == key and isinstance(dval, int) and dval > 0:
                dg = SIGNAL_GLOSSARY[dkey]
                qual.append("其中 %d %s属%s" % (dval, dg.get("unit", "处"),
                                                dg["label"]))
        bits.append("%s %s %s%s" % (g["label"], val, g.get("unit", "处"),
                                    ("（%s）" % "，".join(qual)) if qual else ""))
    for key in sorted(own_keys):
        bits.append("%s %s 处" % (key, errata[key]))

    lines = ["⚠️ 本书文本可信度：%s；观测到 %s。" % (how, "、".join(bits))]
    for key in fired:
        g = SIGNAL_GLOSSARY[key]
        seg = "  · %s（%s）：%s" % (g["label"], key, g["what"])
        if g.get("eg"):
            seg += "；如 %s" % g["eg"]
        seg += " → %s" % g["harm"]
        lines.append(seg)
    for note in (errata.get("notes") or [])[:3]:
        lines.append("  · 注记[%s]：%s" % (note.get("source", "?"), note.get("text", "")))
    if errata.get("unregistered"):
        keys = "、".join(str(u.get("key")) for u in errata["unregistered"][:5])
        lines.append("  · ⚠️ 存在未登记信号（无释义）：%s" % keys)
    lines.append(
        "引用本书内容时**另找资料旁证**，不要单凭本书下结论；"
        "生僻字/符号先看上下文是否说得通。"
    )
    return "\n".join(lines)
