"""
文件名: tags.py
摘要: tag 语法权威层（纯函数，不碰数据库）。
      · 哨兵 TAG_ALL = "OMNIA"：语义为"搜全部数据"，只存在于查询接口，永不落库。
      · 读路径 normalize_query_tags()：哨兵 / 空串 → None（不生成 tag 谓词）；
        哨兵与真 tag 混用 → TagSyntaxError（ALL OR <tag> 恒等于 ALL，无意义）。
      · 写路径 validate_write_tags() / validate_single_tag()：逐元素拦截
        哨兵、SQL 保留字、结构性字符（单值来源里的逗号尤其危险——会被静默切成两个 tag）。
      · 唯一真相源：哨兵值、保留字表、非法字符表只在本文件定义，其余模块一律 import。
依赖: 无（纯函数，便于单测）
边界: 本模块是「输入卫生 + 早失败」，不是注入防线——注入防线是全项目沿用绑定参数（? 占位符）。
      保留字表方向保守即可：漏了不致命（参数化兜底），多了只是要求换个 tag 名；
      匹配为「元素级精确匹配」，故 Inorganic 含 IN、Matchov 含 MATCH 都不会被误伤。
"""

from __future__ import annotations

TAG_ALL = "OMNIA"

_SEPARATOR = ","

# 结构性字符：任何一个出现都会破坏 tags 字段（CSV-in-a-column）的结构或制造 SQL 歧义。
# 逗号不在此表——它是分隔符，多值输入按逗号切分；单值来源里的逗号由 validate_single_tag 拦截。
_FORBIDDEN_CHARS: dict[str, str] = {
    "'": "单引号",
    '"': "双引号",
    "`": "反引号",
    ";": "分号",
    "\\": "反斜杠",
    "\n": "换行",
    "\r": "回车",
    "\t": "制表",
}

# MariaDB 保留字（方向保守，不追求版本同步；元素级、大小写不敏感匹配）。
_SQL_KEYWORDS: frozenset[str] = frozenset(
    "add all alter analyze and as asc asensitive before between bigint binary blob both by "
    "call cascade case change char character check collate column condition constraint continue "
    "convert create cross current_date current_role current_time current_timestamp current_user "
    "cursor database databases day_hour day_microsecond day_minute day_second dec decimal declare "
    "default delayed delete desc describe deterministic distinct distinctrow div double drop dual "
    "each else elseif enclosed escaped except exists exit explain false fetch float float4 float8 "
    "for force foreign from fulltext general grant group having high_priority hour_microsecond "
    "hour_minute hour_second if ignore in index infile inner inout insensitive insert int int1 "
    "int2 int3 int4 int8 integer intersect interval into is iterate join key keys kill leading "
    "leave left like limit linear lines load localtime localtimestamp lock long longblob longtext "
    "loop low_priority match maxvalue mediumblob mediumint mediumtext middleint "
    "minute_microsecond minute_second mod modifies natural no_write_to_binlog not null numeric "
    "offset on optimize option optionally or order out outer outfile over partition precision "
    "primary procedure purge range read reads read_write real recursive references regexp release "
    "rename repeat replace require restrict return returning revoke right rlike row rows "
    "second_microsecond select sensitive separator set show signal smallint spatial specific sql "
    "sqlexception sqlstate sqlwarning sql_big_result sql_calc_found_rows sql_small_result ssl "
    "starting straight_join system table terminated then tinyblob tinyint tinytext to trailing "
    "trigger true undo union unique unlock unsigned update usage use using utc_date utc_time "
    "utc_timestamp values varbinary varchar varcharacter varying vector when where while window "
    "with write xor year_month zerofill "
    "accessible cube cume_dist data dense_rank empty first_value generated grouping groups "
    "history invisible json json_table lag last_value lateral lead locked nth_value ntile of "
    "others package page_checksum percent_rank persistent rank role rollup row_number skip "
    "stored virtual visible"
    .split()
)


class TagSyntaxError(ValueError):
    """tag 违反语法规范（哨兵混用 / 保留字 / 结构性字符 / 非法来源）。"""


def split_tags(tags: str | None) -> list[str]:
    """按逗号切分并规范化：去首尾空白、丢弃空元素。None/空串 → []。"""
    if not tags:
        return []
    if not isinstance(tags, str):
        raise TagSyntaxError(f"tags 必须是字符串，收到 {type(tags).__name__}")
    return [t.strip() for t in tags.split(_SEPARATOR) if t.strip()]


def is_tag_all(tag: str) -> bool:
    """是否为「搜全部数据」哨兵（对大小写与首尾空白不敏感）。"""
    return isinstance(tag, str) and tag.strip().upper() == TAG_ALL


def normalize_query_tags(tags: str | None) -> str | None:
    """读路径规范化。

    返回 None 表示「不生成 tag 谓词」（全集）；否则返回规范化后的 tag 串。
    · 空串 / 纯空白 → None（与哨兵同义）
    · 单独哨兵 TAG_ALL → None
    · 哨兵与真 tag 混用 → TagSyntaxError
    """
    elements = split_tags(tags)
    if not elements:
        return None
    if any(is_tag_all(e) for e in elements):
        if len(elements) > 1:
            raise TagSyntaxError(
                f"{TAG_ALL} 表示「搜全部数据」，与具体 tag 混用没有意义"
                f"（ALL OR <tag> 恒等于 ALL）：{tags!r}"
            )
        return None
    for element in elements:
        _check_element(element)
    return _SEPARATOR.join(elements)


def validate_write_tags(tags: str | None) -> str:
    """写路径校验（多值来源，如 CLI `--tags a,b`）。返回规范化后的逗号串（可能为 ""）。"""
    elements = split_tags(tags)
    for element in elements:
        if is_tag_all(element):
            raise TagSyntaxError(
                f"{TAG_ALL} 是保留哨兵（表示「搜全部数据」），永不落库——请改用别的 tag 名"
            )
        _check_element(element)
    return _SEPARATOR.join(elements)


def validate_single_tag(value: str | None, source: str) -> str:
    """写路径校验（单值来源，如目录名自动 tag）。返回规范化后的单元素（可能为 ""）。

    单值里出现逗号必须报错：它会被静默切成多个 tag，是"看着像一本书、实际打了两个 tag"
    这类幽灵问题的源头。
    """
    element = (value or "").strip()
    if not element:
        return ""
    if _SEPARATOR in element:
        raise TagSyntaxError(
            f"{source} 含逗号（{value!r}）——逗号是 tag 分隔符，会被静默切成多个 tag；"
            f"请改名该来源，或改用 --no-dir-tag"
        )
    if is_tag_all(element):
        raise TagSyntaxError(
            f"{source} 命中保留哨兵 {TAG_ALL}（表示「搜全部数据」），永不落库；请改名"
        )
    _check_element(element)
    return element


def audit_tag_element(element: str) -> str | None:
    """元素级体检：合法返回 None，违规返回人类可读原因。

    只读判据，供 --audit 与写路径校验共用同一套规则（唯一真相源）。
    """
    if is_tag_all(element):
        return "是保留哨兵 OMNIA（表示「搜全部数据」），永不落库——请改用别的 tag 名"
    for char, name in _FORBIDDEN_CHARS.items():
        if char in element:
            return (
                f"含非法字符「{name}」——引号/分号/反斜杠会造成 SQL 结构歧义，"
                f"换行/制表会破坏 tags 字段结构"
            )
    if element.lower() in _SQL_KEYWORDS:
        return "是 SQL 保留字——作为 tag 无信息量，且在任何字符串拼接退化处会制造结构歧义，请改名"
    return None


def _check_element(element: str) -> None:
    """单元素校验：结构性字符 + SQL 保留字（判据与 audit_tag_element 同源）。"""
    reason = audit_tag_element(element)
    if reason:
        raise TagSyntaxError(f"tag {element!r} {reason}")
