"""测试: unicode_utils.count_unicode_risk —— 书级 Unicode 风险计数。

只算「从 md 本身就能算出来」的信号（本库不碰源 PDF）。
真实依据：某已入库的书实测有 11,018 个康熙部首码点。
"""

from __future__ import annotations

from src.core.unicode_utils import count_unicode_risk


def test_clean_text_is_zero():
    assert count_unicode_risk("月背征途 着陆 月球背面") == (0, 0)
    assert count_unicode_risk("") == (0, 0)


def test_kangxi_radicals_counted():
    """U+2F49（康熙部首 月）看着是「月」，但码点不同 —— 正是那类污染。"""
    zang = "⽉"      # U+2F49 KANGXI RADICAL MOON
    han = "月"       # U+6708 CJK
    assert count_unicode_risk(f"{zang}背征途") == (1, 0)
    assert count_unicode_risk(f"{zang}{zang}{han}") == (2, 0)


def test_pua_counted():
    assert count_unicode_risk("正常\ue000\uf8ff") == (0, 2)


def test_mixed_and_single_pass():
    s = "⽉\ue000⽇\ue001"
    assert count_unicode_risk(s) == (2, 2)


def test_cjk_radicals_supplement_is_not_kangxi():
    """CJK 部首补充 U+2E80–U+2EFF 是**另一块**，不在康熙部首窄口径内
    （宽口径在 NFKC 下没有永久稳定性背书，见 unicode_utils 的说明）。"""
    assert count_unicode_risk("⻜⻋⻓") == (0, 0)
