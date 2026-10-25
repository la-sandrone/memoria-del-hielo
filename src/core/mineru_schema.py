"""
文件名: mineru_schema.py
摘要: MinerU v3 / v4 产物元数据的**唯一适配点** —— 全项目只有这里认两代的结构差异。
      导入器与后处理器只消费本模块归一化后的形状，禁止在别处写 `if version == 4`。
依赖: json, zipfile, pathlib, dataclasses
      ./unicode_utils（仅类型标注无关；本模块不依赖它）

═══ 四种坐标口径 → 折成一套 0–1 归一化 ═══
实测（真书真数据，2026-09-28）：

  产出                                         bbox 值域        单位
  v3 <stem>_middle.json                        0 … 813          页内 pt 绝对（配 page_size）
  v3 <stem>_content_list{,_v2}.json            0 … 1000         0–1000 整数
  v3 <stem>_model.json                         0 … 1            0–1 归一化
  v4 middle_json.json / structured_content.json
     / model_output.json                       0 … 1            0–1 归一化

本模块**一律折成 0–1 归一化**（x/宽、y/高）。收益是判据不必再知道自己读的是哪一代：
「脚注在页面底部」在归一化空间里就是同一句 `y >= 0.70`，不需要页高参与。
（v3 的 pt 口径除以 page_size；0–1000 口径除以 1000；v4 原样。）

═══ 三种产物形态（按**能力**命名，不按打包方式）═══
  dir   v3: <out>/<stem>/<method>/<stem>.md
           + <stem>_middle.json + <stem>_content_list{,_v2}.json + images/
           + 同级 <stem>_model.json（本模块不消费）
  full  v4: 完整侧车集 —— `--format zip` 的产出**或它的解包目录**（两者能力相同，
           故不叫 zip：叫 zip 会在解包场景撒谎）：
           markdown.md + middle_json.json + structured_content.json
           + model_output.json + images/page_*.jpg
  flat  v4: <out>/<stem>.md —— 图片 base64 内联、零 json、无任何元数据

═══ 归一的落点 ═══
  · blocks 归一成 NormalizedBlock（page_idx / type / text / bbox01）
  · 块类型跨代别名（v4 paragraph_title ↔ v3 title 等）
  · 行内文本抽平：v3 走 lines[].spans[]，v4 走 content[].{type,content}
  · structured_content 的 content 本来就是字符串（无需抽平）
  · 脚注候选集：v3 取 discarded_blocks（v3 有专门的丢弃列表）；
    v4 取全部 blocks（v4 没有丢弃列表，脚注就是普通块的一种 type）
  · 图片类型映射：v3 取 _content_list_v2.json 的 content.image_source.path；
    v4 full 取 structured_content.json 的 image_source（扁平字符串）
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# ── 常量 ────────────────────────────────────────────────

FORM_DIR = "dir"
FORM_FLAT = "flat"
FORM_FULL = "full"

# parse_mode 的三种态（D1 裁定）：
#   'txt'     原生文本层 —— 文本层的字符缺陷会原样穿透，图片值得 OCR 兜一层
#   'ocr'     视觉识别   —— 正文文字已由模型产出，图片无文字价值 → 跳过 OCR
#   'unknown' 字段拿不到（v4 扁平 md）→ **保守走 OCR**，不静默丢信息
PARSE_MODE_UNKNOWN = "unknown"

# v3 中间表示的页内 pt 尺寸上限哨兵（page_size 缺失时无法归一化）
MISSING = -1.0

# 跨代块类型别名：v4 名 → v3 名（本模块内部统一用**语义中性的名字**，见 _canon_type）
_TYPE_ALIASES: dict[str, str] = {
    "paragraph_title": "title",     # v4 的段落标题 = v3 的 title
    "doc_title": "title",
    "image_body": "image",
    "table_body": "table",
    "chart_body": "chart",
    "code_body": "code",
    "equation": "equation_interline",
    "interline_equation": "equation_interline",
}

# 判 OC​R 与否只看这些「视觉父类型」（与 v3 _content_list_v2 的取值域对齐）
VISUAL_TYPES = frozenset({"image", "table", "chart", "equation_interline"})

# 脚注相关类型（v2 判据用；两代都出现过）
FOOTNOTE_TYPES = frozenset({"page_footnote", "footnote"})
# 明确的版面元素：不是脚注（v1 判据把它们当脚注收了，D2 修正）
NON_FOOTNOTE_TYPES = frozenset({"header", "footer", "page_number"})

# 「已知」规范名白名单（唯一一份）。census_report 用它判未识别类型；
# 因为过的是 _canon_type(raw)，所以**不需要**再抄一份 raw 名白名单。
KNOWN_CANONICAL: frozenset[str] = frozenset({
    # 视觉父类型与其体/题/注
    "image", "table", "chart", "equation_interline",
    "image_caption", "image_footnote", "table_caption", "table_footnote",
    "chart_caption", "chart_footnote", "code_caption", "code_footnote",
    "code",
    # 文字块
    "text", "title", "list", "index", "caption", "phonetic", "ref_text",
    "aside_text",
    # 版面元素
    *NON_FOOTNOTE_TYPES,
    # 脚注
    *FOOTNOTE_TYPES,
})


def _canon_type(raw: Any) -> str:
    """块类型归一到语义中性名。未知类型原样返回（不静默丢弃，便于覆盖率统计）。"""
    t = str(raw or "").strip()
    return _TYPE_ALIASES.get(t, t)


# ── 归一化的数据形状 ────────────────────────────────────


@dataclass(frozen=True)
class Provenance:
    """产出方元数据 —— 落 documents_book 的四列。

    非 MinerU 产物：四个字段全空字符串。
    """

    version: str = ""       # '3.4.0' / '4.0.7'
    tier: str = ""          # [v4] flash|basic|standard|advanced
    parse_mode: str = ""    # [v4] txt|ocr；扁平 md 拿不到 → 'unknown'
    form: str = ""          # dir | flat | zip


@dataclass(frozen=True)
class NormalizedBlock:
    """跨代统一的块。bbox 一律 0–1 归一化；不可得时为 ()。"""

    page_idx: int
    type: str               # 已过 _canon_type
    raw_type: str           # 原样类型（审计/覆盖率用）
    text: str
    bbox01: tuple[float, float, float, float] | None
    discarded: bool = False  # v3 语义：来自 discarded_blocks；v4 恒 False

    @property
    def y_top01(self) -> float | None:
        """归一化 y 上沿（判页面底部用）。bbox 不可得 → None。"""
        return None if self.bbox01 is None else self.bbox01[1]


@dataclass
class MineruProduct:
    """一份 MinerU 产物的归一化视图。

    构造后所有消费方只看这些字段，不需要知道产物是 v3 还是 v4 还是扁平 md。
    """

    provenance: Provenance
    form: str
    # 脚注候选块（已按形态选好候选集，见模块 docstring）
    footnote_candidates: list[NormalizedBlock] = field(default_factory=list)
    # {图片引用路径: 块类型}；扁平 md / 缺 json 时为空
    image_type_map: dict[str, str] = field(default_factory=dict)
    # 每页原始页高（pt）；v4 归一化空间下不需要，仅留作审计
    page_height_pt: dict[int, float] = field(default_factory=dict)
    # 覆盖率审计：解析到的原始块类型计数（含未识别者）
    type_census: dict[str, int] = field(default_factory=dict)

    # ── 供导入器使用 ──

    @property
    def skip_ocr_for_images(self) -> bool:
        """图片要不要跳过 OCR（parse_mode 分流，D1 定：unknown 保守走 OCR）。

        ocr → 正文文字来自视觉识别，图片无文字价值 → 跳过
        txt / unknown / ''（v3 无此字段）→ 不跳过
        """
        return self.provenance.parse_mode == "ocr"

    @property
    def is_mineru(self) -> bool:
        return self.form != ""


# ── 入口：从路径装载 ────────────────────────────────────


def load_product(
    md_path: Path, *, assume_flat_mineru: bool = False
) -> MineruProduct:
    """按形态识别并装载一份 MinerU 产物的元数据（除扁平探测外不读 md 正文）。

    识别顺序（判据都要正面命中，不靠排除法）：
      1. 同目录 <stem>_middle.json          → v3 dir
      2. 同目录 <stem>.zip                  → v4 full
      3. 同目录 <stem>.json 且 schema=='docvortex.middle' → v4 full（解包目录形态）
      4. 无侧车 且 正文含 data:image/*;base64,  → v4 flat
      5. 都不命中                            → 非 MinerU 产物（form=''）

    `assume_flat_mineru=True` 时跳过第 4 条的正文探测，直接判 flat ——
    给"纯文字、无内联图"的 v4 扁平产出用（见下方判据说明）。
    """
    stem = md_path.stem
    d = md_path.parent

    v3_middle = d / f"{stem}_middle.json"
    if v3_middle.is_file():
        return _load_v3(md_path, v3_middle, d)

    z = d / f"{stem}.zip"
    if z.is_file():
        return _load_v4_zip(md_path, z)

    # 解包形态的中间表示名是**固定的** middle_json.json（真 zip 成员名），
    # 不是 <stem>.json —— 后者只作回退（手工改名/多书同目录）
    for cand in (d / "middle_json.json", d / f"{stem}.json"):
        if cand.is_file() and _is_v4_middle(cand):
            return _load_v4_dir(md_path, cand, d)

    if assume_flat_mineru or _looks_like_v4_flat(md_path):
        return MineruProduct(
            provenance=Provenance(parse_mode=PARSE_MODE_UNKNOWN, form=FORM_FLAT),
            form=FORM_FLAT,
        )

    # 都不命中 → 当普通 md（不是 MinerU 产物）
    return MineruProduct(provenance=Provenance(), form="")


# 扁平 md 的判据探测窗：只看前 N 字节。v4 扁平产出的内联图必然出现在正文里，
# 但一本纯文字书可能几张图都没有 → 靠调用方用 assume_flat_mineru 兜。
_PROBE_BYTES = 262_144


def _looks_like_v4_flat(md_path: Path) -> bool:
    """v4 扁平 md 的**正面判据**：正文含内联 data URI 图。

    取舍（写清楚，免得以后被当成"启发式"）：
      · 假阳性：用户自己写的笔记里粘过 base64 图 → 被判 MinerU flat。
        后果**仅限 provenance 元数据**（mineru_form='flat',
        mineru_parse_mode='unknown'）：footnote 候选为空、image_type_map 为空，
        行为与"非 MinerU"完全一致，检索结果不变。
      · 假阴性：v4 扁平产出但整本无内联图（纯文字书）→ 判为非 MinerU，
        同样只丢 provenance。要覆盖它，调用方传 assume_flat_mineru=True。
    两边的后果都被限制在元数据上，所以这个判据可以放心用，
    而不是"猜语义"。
    """
    try:
        with open(md_path, "rb") as f:
            head = f.read(_PROBE_BYTES)
    except OSError:
        return False
    return b"](data:image/" in head and b";base64," in head


def _is_v4_middle(p: Path) -> bool:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(d, dict) and d.get("schema") == "docvortex.middle"


# ── v3 ──────────────────────────────────────────────────


def _load_v3(md_path: Path, middle_path: Path, d: Path) -> MineruProduct:
    try:
        mid = json.loads(middle_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MineruProduct(provenance=Provenance(form=FORM_DIR), form=FORM_DIR)

    prov = Provenance(
        version=str(mid.get("_version_name") or ""),
        tier="",                     # v3 无档位概念（它用 _backend，如 hybrid_auto）
        parse_mode="",               # v3 无此字段
        form=FORM_DIR,
    )
    page_height: dict[int, float] = {}
    candidates: list[NormalizedBlock] = []
    census: dict[str, int] = {}

    for page in mid.get("pdf_info") or []:
        if not isinstance(page, dict):
            continue
        pidx = int(page.get("page_idx", -1))
        size = page.get("page_size") or []
        w, h = (float(size[0]), float(size[1])) if len(size) >= 2 else (0.0, 0.0)
        if h > 0:
            page_height[pidx] = h
        for blk in page.get("discarded_blocks") or []:
            nb = _v3_block(blk, pidx, w, h, discarded=True)
            if nb is not None:
                candidates.append(nb)
                census[nb.raw_type] = census.get(nb.raw_type, 0) + 1

    return MineruProduct(
        provenance=prov,
        form=FORM_DIR,
        footnote_candidates=candidates,
        image_type_map=_v3_image_type_map(md_path, d),
        page_height_pt=page_height,
        type_census=census,
    )


def _v3_block(
    blk: Any, pidx: int, w: float, h: float, *, discarded: bool
) -> NormalizedBlock | None:
    if not isinstance(blk, dict):
        return None
    raw = str(blk.get("type") or "")
    return NormalizedBlock(
        page_idx=pidx,
        type=_canon_type(raw),
        raw_type=raw,
        text=_v3_block_text(blk),
        bbox01=_norm_bbox(blk.get("bbox"), w, h, scale=1.0),
        discarded=discarded,
    )


def _v3_block_text(blk: dict[str, Any]) -> str:
    """v3 行内文本：lines[].spans[]，text 与 inline_equation 两类 span。"""
    out: list[str] = []
    for line in blk.get("lines") or []:
        if not isinstance(line, dict):
            continue
        for span in line.get("spans") or []:
            if not isinstance(span, dict):
                continue
            st, sc = span.get("type"), span.get("content", "")
            if st == "text":
                out.append(str(sc))
            elif st == "inline_equation":
                out.append(f"${sc}$")
    return " ".join(x for x in out if x).strip()


def _v3_image_type_map(md_path: Path, d: Path) -> dict[str, str]:
    """v3：_content_list_v2.json → {content.image_source.path: block type}。

    路径取原样（相对/绝对都可能），与 md 里的引用串直接比对——
    调用方用 `_RE_MINERU_IMG` / 四层策略解析后的形态，本模块不做匹配。
    """
    stem = md_path.stem
    for suffix in ("_content_list_v2.json", "_content_list.json"):
        p = d / f"{stem}{suffix}"
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        m: dict[str, str] = {}
        for group in data or []:
            for blk in group or []:
                if not isinstance(blk, dict):
                    continue
                t = _canon_type(blk.get("type"))
                if t not in VISUAL_TYPES:
                    continue
                content = blk.get("content")
                if not isinstance(content, dict):
                    continue
                src = content.get("image_source")
                if isinstance(src, dict) and src.get("path"):
                    m[str(src["path"])] = t
                elif isinstance(src, list):
                    for one in src:
                        if isinstance(one, dict) and one.get("path"):
                            m[str(one["path"])] = t
        if m:
            return m
    return {}


# ── v4：zip 与解包目录 ──────────────────────────────────


def _load_v4_zip(md_path: Path, z: Path) -> MineruProduct:
    try:
        with zipfile.ZipFile(z) as zf:
            names = set(zf.namelist())
            mid = json.loads(zf.read("middle_json.json")) if "middle_json.json" in names else None
            sc = (json.loads(zf.read("structured_content.json"))
                  if "structured_content.json" in names else None)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return MineruProduct(provenance=Provenance(form=FORM_FULL), form=FORM_FULL)
    return _build_v4(mid, sc, FORM_FULL, md_path, z.parent)


def _load_v4_dir(md_path: Path, middle: Path, d: Path) -> MineruProduct:
    """v4 解包目录形态。

    ⚠️ 真 zip 的成员名是**固定名**，不带 stem：
        markdown.md / middle_json.json / structured_content.json
    所以解包后找的是 `middle_json.json`，**不是** `<stem>.json`。
    （首版写成 `<stem>.json`，而夹具又被我改名成 Book.json → 测试自证通过。
      抓出来的手段是把真 zip 的 namelist 打出来对文件名。）
    仍接受 `<stem>.json` 作回退，供用户手工改名/多书同目录的情形。
    """
    try:
        mid = json.loads(middle.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        mid = None
    sc_p = d / "structured_content.json"
    sc = None
    if sc_p.is_file():
        try:
            sc = json.loads(sc_p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            sc = None
    # form 判据 = **能力**（真的读到了 middle_json 没有），不是猜打包方式
    form = FORM_FULL if mid is not None else FORM_FLAT
    return _build_v4(mid, sc, form, md_path, d)


def _build_v4(
    mid: dict[str, Any] | None,
    sc: dict[str, Any] | None,
    form: str,
    md_path: Path,
    d: Path,
) -> MineruProduct:
    src = mid or sc or {}
    md_meta = src.get("metadata") or {}
    producer = md_meta.get("producer") or {}
    ext = (src.get("extensions") or {}).get("mineru") or {}
    prov = Provenance(
        version=str(producer.get("version") or ""),
        tier=str(ext.get("tier") or ""),
        parse_mode=str(ext.get("parse_mode") or ""),
        form=form,
    )

    # 页高：extensions.docvortex_layout.pages[].height_pt（v4 归一化空间下仅审计用）
    page_height: dict[int, float] = {}
    lay = (src.get("extensions") or {}).get("docvortex_layout") or {}
    for pg in lay.get("pages") or []:
        if isinstance(pg, dict):
            h = pg.get("height_pt")
            if isinstance(h, (int, float)) and h > 0:
                page_height[int(pg.get("page_idx", -1))] = float(h)

    # 脚注候选集：v4 没有 discarded 列表 → 全部 blocks 都是候选
    candidates: list[NormalizedBlock] = []
    census: dict[str, int] = {}
    for page in (src.get("pages") or []):
        if not isinstance(page, dict):
            continue
        pidx = int(page.get("page_idx", -1))
        for blk in page.get("blocks") or []:
            nb = _v4_block(blk, pidx)
            if nb is None:
                continue
            candidates.append(nb)
            census[nb.raw_type] = census.get(nb.raw_type, 0) + 1

    return MineruProduct(
        provenance=prov,
        form=form,
        footnote_candidates=candidates,
        image_type_map=_v4_image_type_map(sc, d),
        page_height_pt=page_height,
        type_census=census,
    )


def _v4_block(blk: Any, pidx: int) -> NormalizedBlock | None:
    if not isinstance(blk, dict):
        return None
    raw = str(blk.get("type") or "")
    return NormalizedBlock(
        page_idx=pidx,
        type=_canon_type(raw),
        raw_type=raw,
        text=_v4_block_text(blk.get("content")),
        bbox01=_norm_bbox(blk.get("bbox"), 0.0, 0.0, scale=1.0),  # v4 已是 0–1
        discarded=False,
    )


def _v4_block_text(content: Any) -> str:
    """v4 行内文本。三种输入：

      · str              structured_content 已抽平成字符串
      · span 列表        middle_json；**span 的 content 可再嵌套一层列表**（实测）
      · 嵌套 dict        如 {"type":"hyperlink","content":[{"type":"text",…}]}

    实测样例（Unicode 14.0 标准书，page 1）:
        {"type": "index", "content": [
            {"type": "text", "content": [
                {"type": "text", "content": "Preface . . . xxi"}]}, …]}

    ⚠️ 这里踩过一次：首版只抽一层，遇到嵌套就把内层列表 `str()` 成了
    `[{'type': 'text', 'content': …}]` 塞进正文 —— **静默产出垃圾**，
    而字符集比对看起来"有分歧"却在查别的东西。
    故本函数对**不认识的节点形状直接抛**，绝不 str() 了事。
    """
    out: list[str] = []

    def walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, str):
            out.append(node)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if isinstance(node, dict):
            t = node.get("type")
            if t == "equation":
                out.append(f"${node.get('content') or ''}$")
                return
            if t in _VISUAL_BODIES:
                return          # 视觉体不进脚注文本（与 v3 只取 lines/spans 对称）
            if "content" in node:
                walk(node["content"])
                return
            return
        raise TypeError(
            f"未预期的 content 节点类型 {type(node).__name__}: {repr(node)[:120]}"
        )

    walk(content)
    return " ".join(x for x in out if x).strip()


_VISUAL_BODIES = frozenset({"image_body", "table_body", "chart_body", "code_body"})


def _v4_image_type_map(sc: dict[str, Any] | None, d: Path) -> dict[str, str]:
    """v4：structured_content.json 的 image_source（扁平字符串）→ 块类型。"""
    if not isinstance(sc, dict):
        return {}
    m: dict[str, str] = {}
    for page in sc.get("pages") or []:
        if not isinstance(page, dict):
            continue
        for blk in page.get("blocks") or []:
            if not isinstance(blk, dict):
                continue
            t = _canon_type(blk.get("type"))
            if t not in VISUAL_TYPES:
                continue
            src = blk.get("image_source")
            if isinstance(src, str) and src:
                m[src] = t
            elif isinstance(src, list):
                for one in src:
                    if isinstance(one, str) and one:
                        m[one] = t
    # zip 解包形态下 structured_content 的 image_source 是相对 zip 根的
    # （images/page_x_y_z.jpg），而 md 在解包目录里也相对同一根 → 无需改写
    return m


# ── bbox 归一化 ─────────────────────────────────────────


def _norm_bbox(
    bbox: Any, w: float, h: float, *, scale: float
) -> tuple[float, float, float, float] | None:
    """把各代 bbox 折成 0–1 归一化。尺度不可得 → None（不猜）。

    v3 _middle.json : 传 w/h = page_size（pt），scale=1
    v3 _content_list: 传 w=h=0，scale=0.001（0–1000 整数）
    v4              : 传 w=h=0，scale=1（已是 0–1）
    """
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return None
    try:
        x0, y0, x1, y1 = (float(bbox[i]) for i in range(4))
    except (TypeError, ValueError):
        return None
    if scale != 1.0:
        x0, y0, x1, y1 = (v * scale for v in (x0, y0, x1, y1))
    if w > 0 and h > 0:
        x0, x1 = x0 / w, x1 / w
        y0, y1 = y0 / h, y1 / h
    return (x0, y0, x1, y1)


# ── 覆盖率审计（「裁判」第四条：说清解析了多少、什么形状）────────


def census_report(candidates: Iterable[NormalizedBlock]) -> dict[str, Any]:
    """报告候选块的类型分布、bbox 缺失数、以及未识别类型。

    返回可直接打日志/断言的结构；`unknown_types` 非空时调用方应当告警，
    而不是静默继续（静默跳过就是覆盖率黑洞）。

    判「已知」的口径：把 raw 类型过一遍 `_canon_type` 再查规范名白名单。
    不能另写一份 raw 名白名单——那会与别名表各写各的，两表一漂移就误报。
    """
    from collections import Counter

    c: Counter[str] = Counter()
    no_bbox = 0
    n = 0
    for b in candidates:
        n += 1
        c[b.raw_type] += 1
        if b.bbox01 is None:
            no_bbox += 1
    return {
        "count": n,
        "types": dict(c),
        "no_bbox": no_bbox,
        "unknown_types": sorted(t for t in c if _canon_type(t) not in KNOWN_CANONICAL),
    }
