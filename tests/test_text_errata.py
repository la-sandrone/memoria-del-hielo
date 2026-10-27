"""测试: core/text_errata —— 书级误差统计的判据、合并与渲染。

三条纪律（都是踩过坑的）：
  1. **计数 0 ≠ 未计算**：`{}`（迁移 007 之前的存量书）与「算了是 0」必须分开说。
  2. **跳过 ≠ 0**：中文书不启用 cjk_halluc，跳过的键进 not_applicable，不能写 0 冒充。
  3. **未登记的信号不许静默透传**：manifest 塞进来的键若没自带释义，
     渲染时只能标「未登记」，不能把一个 LLM 看不懂的键混进 payload。
"""

from __future__ import annotations

import json

from src.core.text_errata import (
    CRITERIA_VERSION,
    SIGNAL_GLOSSARY,
    count_errata,
    errata_payload,
    fired_signals,
    merge_errata,
    render_errata_note,
)

CYR = "Византийский гуманизм как явление известен всем исследователям."
CLEAN = CYR + " Ещё одно предложение для объёма."


# ── 判据 ──────────────────────────────────────────────


def test_clean_russian_book_has_no_signals():
    e = count_errata(CLEAN)
    assert e["criteria_version"] == CRITERIA_VERSION
    assert e["chars"] == len(CLEAN)
    assert e["words"] > 0
    assert fired_signals(e) == []
    assert "not_applicable" not in e
    assert render_errata_note(e, "txt") == ""


def test_script_mix_counts_lookalike_substitution():
    """Φacmep（希腊 Φ + 拉丁 c,p,e）和 nуть（拉丁 n）都该被抓到。"""
    e = count_errata("Φacmep писал про nуть и Русck язык.")
    assert e["script_mix"] == 3


def test_accented_latin_only_for_cyrillic_words():
    """пахáть 里那个 á 是「重音表示形式选错」；纯拉丁的 café 不算误差。"""
    e = count_errata("пахáть слóва café français")
    assert e["accented_latin"] == 2, "只有西里尔词里的拉丁重音元音算"
    assert e["script_mix"] == 2
    e2 = count_errata("café français Université")
    assert e2["accented_latin"] == 0
    assert e2["script_mix"] == 0


def test_accented_latin_is_subset_of_script_mix():
    """不变式：重音键必然是串写键的子集（否则分母口径就自相矛盾了）。"""
    e = count_errata("пахáть and Φacmep and nуть")
    assert e["accented_latin"] <= e["script_mix"]


def test_hyphen_break_needs_newline_and_letters():
    assert count_errata("дву-\nсмысленный")["hyphen_break"] == 1
    assert count_errata("дву- смысленный")["hyphen_break"] == 0, "同行不算断词"
    assert count_errata("кто-то кое-что")["hyphen_break"] == 0, "真连字符不算"
    assert count_errata("дву-\n  смысленный")["hyphen_break"] == 1, "行首缩进要容忍"


def test_cjk_gate_skips_chinese_books_instead_of_zeroing():
    """中文书整本都是汉字 —— 必须进 not_applicable，**不是写 0 冒充**。"""
    zh = "月背征途，着陆月球背面。我们分析了矿物与岩石的成因，并给出结论。" * 20
    e = count_errata(zh)
    assert e["not_applicable"] == ["cjk_halluc"]
    assert e["cjk_halluc"] == 0
    assert "cjk_halluc" not in fired_signals(e), "跳过的键不许出现在触发列表里"
    assert "cjk_halluc" not in (errata_payload(e).get("glossary") or {}), "没触发就不贴释义"


def test_cjk_run_counted_in_non_chinese_book():
    """真书上汉字占比可忽略，闸门放行 —— 但要用够长的正文才具代表性。

    （闸门是按全文字符占比判的：短文本里两个汉字就能占 7%，会误触闸门。
      导入与回填一律以整本书为输入，所以这不是实际路径上的问题。）
    """
    e = count_errata(CYR * 20 + " Посхѣ江南ее слово.")
    assert e["cjk_halluc"] == 1
    assert "not_applicable" not in e, "占比可忽略的书不该被闸门跳过"


def test_isolated_cjk_char_is_not_a_hallucination():
    """实测：θε 被读成孤立的 戈 —— 单个汉字是字母误识，不是回吐语料。"""
    e = count_errata("это 戈στική θεωρία исихастов")
    assert e["cjk_halluc"] == 0


def test_kangxi_and_pua_pass_through_unicode_utils():
    zang = "\u2f49"  # ⽉ 康熙部首
    e = count_errata(f"{zang}背征途 \ue000")
    assert e["kangxi_radical"] == 1
    assert e["pua"] == 1


# ── 合并 ──────────────────────────────────────────────


def test_merge_overwrites_counts_and_appends_notes():
    a = merge_errata(None, count_errata(CLEAN), "importer")
    b = merge_errata(a, {"script_mix": 42}, "audit", note="离线扫描：42 处串写")
    assert b["script_mix"] == 42, "机械计数覆盖"
    assert b["criteria_version"] == CRITERIA_VERSION
    assert [n["source"] for n in b["notes"]] == ["audit"]
    assert a["script_mix"] == 0, "纯函数，不许改入参"


def test_merge_note_dedup_by_source_and_text():
    a = merge_errata(None, {}, "audit", note="同一句")
    b = merge_errata(a, {}, "audit", note="同一句")
    assert len(b["notes"]) == 1
    c = merge_errata(b, {}, "other", note="同一句")
    assert len(c["notes"]) == 2, "换来源就该记两条"


def test_annotated_book_without_criteria_version_is_still_reported():
    """回归：`errata_payload` 曾只认 criteria_version，会把「只有外部注记」的书
    当成未计算而静默丢掉注记。判据改为「非空即已记录」。"""
    out = merge_errata(
        {}, {"ink_bleed": 12}, "some-tool", glossary={"ink_bleed": "油墨洇透"},
    )
    payload = errata_payload(out)
    assert payload, "非空 errata 必须给出 payload"
    assert payload["glossary"]["ink_bleed"]["what"] == "油墨洇透"
    assert "ink_bleed 12 处" in render_errata_note(out)
    assert render_errata_note(out, "txt") != render_errata_note({}, "txt")


def test_merge_rejects_unregistered_key_without_glossary():
    """manifest 塞了个没人认识的键且不带释义 → 进 unregistered，不静默透传。"""
    out = merge_errata(None, {"weird_metric": 7}, "some-tool")
    assert out.get("weird_metric") is None
    assert out["unregistered"][0]["key"] == "weird_metric"
    payload = errata_payload(out)
    assert "weird_metric" not in (payload.get("glossary") or {})


def test_merge_accepts_external_key_when_glossary_given():
    out = merge_errata(
        count_errata(CLEAN), {"ink_bleed": 12}, "some-tool",
        glossary={"ink_bleed": "油墨洇透导致半行不可读"},
    )
    assert out["ink_bleed"] == 12
    assert "unregistered" not in out
    payload = errata_payload(out)
    assert payload["glossary"]["ink_bleed"]["what"] == "油墨洇透导致半行不可读"


# ── 渲染与自文档化 ────────────────────────────────────


def test_uncomputed_says_unrecorded_not_safe():
    """`{}` = 未计算。必须说「未记录」，不许说「无误差」。"""
    n = render_errata_note({}, "txt")
    assert "未记录" in n or "无误差统计记录" in n
    assert "而非" in n and "无误差" in n
    assert errata_payload({}) == {}, "未计算时不给 payload（免得被当成 0）"


def test_unknown_parse_mode_not_printed_as_english():
    e = count_errata("Φacmep писал")
    n = render_errata_note(e, "unknown")
    assert "读数方式未知" in n
    assert "unknown" not in n


def test_note_carries_counts_and_per_signal_glossary():
    e = count_errata("Φacmep писал про nуть.\nдву-\nсмысленный оборот.")
    n = render_errata_note(e, "ocr")
    assert "视觉读数" in n
    assert "形近字母串写" in n and "2" in n
    assert "行尾连字符未合并" in n
    assert "另找资料旁证" in n, "用户要求的旁证指令必须在"
    # 自文档化：每个触发的键都带释义
    payload = errata_payload(e, "ocr")
    assert set(payload["glossary"]) == set(fired_signals(e))


def test_payload_counts_keep_denominators():
    """计数离开分母不可比 —— chars / words 必须一起回给消费方。"""
    e = count_errata(CLEAN)
    counts = errata_payload(e)["counts"]
    assert counts["chars"] == len(CLEAN)
    assert counts["words"] > 0


def test_glossary_covers_every_counted_signal():
    """词表与判据同源：_COUNT_KEYS 里的信号键必须在 SIGNAL_GLOSSARY 里登记。"""
    from src.core.text_errata import _COUNT_KEYS, _DETAIL_KEYS

    signal_keys = set(_COUNT_KEYS) - {"chars", "words"} - set(_DETAIL_KEYS)
    assert signal_keys <= set(SIGNAL_GLOSSARY), (
        "有信号没登记释义 → 渲染时只能标「未登记」，等于白算：%s"
        % (signal_keys - set(SIGNAL_GLOSSARY))
    )


def test_detail_signal_renders_inline_under_its_parent():
    """accented_latin 是 script_mix 的子计数：内联显示，不单列一行。"""
    e = count_errata("пахáть слóва Φacmep nуть")
    assert fired_signals(e) == ["script_mix"], "子计数不单独占位"
    n = render_errata_note(e, "txt")
    assert "其中 2 词属拉丁重音元音代替" in n
    payload = errata_payload(e)
    assert "accented_latin" in payload["glossary"], "释义要随主信号带上"
    assert payload["glossary"]["accented_latin"]["detail_of"] == "script_mix"


def test_every_glossary_entry_has_required_fields():
    for key, g in SIGNAL_GLOSSARY.items():
        for field in ("label", "what", "harm", "unit"):
            assert g.get(field), f"{key} 缺 {field}"


def test_json_roundtrip_is_stable():
    """要落 JSON 列 —— 序列化-反序列化后必须逐键相同。"""
    e = count_errata("Φacmep писал про nуть.")
    assert json.loads(json.dumps(e, ensure_ascii=False)) == e


# ── 外部（离线审计）供给的信号 ────────────────────────
# 这两个键导入器**算不出来**：要看 middle_json 的块结构、要和源文件对账页数，
# 而导入器手里只有那个 md。它们经 mhi annotate 进来，词表在 text_errata 统一登记。


def test_count_errata_never_emits_external_signals():
    """判据不许越界：导入器看不到 middle_json，产出了就是假值。"""
    from src.core.text_errata import EXTERNAL_SIGNAL_KEYS

    e = count_errata(CYR * 30)
    for key in EXTERNAL_SIGNAL_KEYS:
        assert key not in e, f"count_errata 不该产出 {key}"


def test_audit_signals_merge_and_render():
    from src.core.text_errata import EXTERNAL_SIGNAL_KEYS

    e = count_errata(CLEAN)
    e = merge_errata(e, {"aside_halluc": 137, "pages_short": 2},
                     "audit_zip.py@v1", note="页数 337/339")
    assert e["aside_halluc"] == 137 and e["pages_short"] == 2
    assert "unregistered" not in e, "词表已登记，不该被判为未登记"
    n = render_errata_note(e, "ocr")
    assert "页边幻觉块 137 个" in n
    assert "产出页数短少 2 页" in n
    assert "注记[audit_zip.py@v1]" in n
    payload = errata_payload(e)
    assert payload["glossary"]["aside_halluc"]["harm"]


def test_audit_signals_marked_with_origin():
    """要能一眼看出哪些是外部供给、哪些是判据自算。"""
    for key in ("aside_halluc", "pages_short"):
        assert SIGNAL_GLOSSARY[key].get("origin") == "audit"
    for key in ("script_mix", "hyphen_break", "cjk_halluc"):
        assert "origin" not in SIGNAL_GLOSSARY[key], "判据自算的不该标 origin"


def test_zero_audit_signal_does_not_fire():
    """pages_short=0 是「页数一致」，不是「有问题」——不该占渲染位。"""
    e = merge_errata(count_errata(CLEAN), {"pages_short": 0}, "audit")
    assert "pages_short" not in fired_signals(e)
