"""
文件名: mineru_postprocess.py
摘要: MinerU 输出后处理——脚注提取（判据 v2）+ 图片路径重映射。
      结构差异（v3/v4 三形态、四种 bbox 口径、跨代块类型名）**全部下沉到
      core.mineru_schema**；本模块只负责「判据」与「落库」。
      脚注判据 v2 = 类型优先（收 page_footnote/footnote、拒 footer/header/page_number、
      其余回退底部区域），修掉 v1 的两头错（页脚冒充 24% / 误杀真脚注 13.6%）。
      图片映射四层策略：VLM 描述精确匹配 → images/ hash 直查
      → Everything 搜索裸 hash → 保留原样。
依赖: json, re, pathlib, urllib.parse, requests
      core.config, core.db, core.mineru_schema

═══════════════════════════════════════════════════════════════════
⚠️ 能力声明：我们只尽力，请留意上下文 —— PDF 大坑过多
═══════════════════════════════════════════════════════════════════
PDF 是一堆历史包袱的集合，而且**每一层都能撒谎**：
  · 字体表可以撒谎：`CMap` 是**程序**不是表（规范把映射留给了生成它的程序），
    所以"标准的 ToUnicode 解读"这东西根本不存在；
  · ToUnicode 可写可不写；**写了也能是错的**；同一份文件不同阅读器结果可以不同；
  · 生产者会用 ASCII 码位当字形槽来省空间（实测一本书 50 个这样的字体、
    覆盖 27% 的页面），于是文本层给出的是**槽位号**，看着像正常拉丁字母；
  · 渲染与提取是两条路：**渲染对而提取错**是常态，不是异常。
**上游自己都不能解决 100%，我们也不假装能。**

所以本模块的立场是「检测 → 计数上报 → 绕过去」，不是「修」：
  1. 用**可验证的判据**把能判的判掉（判据要能配正对照、能证伪）；
  2. 判不了 / 超出能力的，一律**显式计数上报**，绝不静默吞掉
     （同 mineru_batch.py 的 DROP 政策：不修不猜，但必须上报，
       否则结算不认账、跑几千本时你不知道少了几本）。
**使用本模块的任何结论时请留意上下文**：数值只覆盖判据覆盖到的那部分；
未覆盖的部分在统计里单列，**不要当成"没问题"**。
"""

from __future__ import annotations

import json
import re
import urllib.parse
import warnings
from pathlib import Path
from typing import Any

import requests

from .config import get_config
from .db import get_db
from .mineru_schema import (
    FOOTNOTE_TYPES,
    NON_FOOTNOTE_TYPES,
    MineruProduct,
    load_product,
)

# ── 常量 ────────────────────────────────────────────────

# MinerU MD 图片引用正则 — 匹配 ![](/resources/images/HASH)
_RE_MINERU_IMG = re.compile(
    r'!\[.*?\]\((/resources/images/[a-f0-9]+)\)'
)

# MinerU <details> 块正则 — 匹配图片后的 VLM 描述
_RE_DETAILS = re.compile(
    r'\n<details>\s*\n<summary>\w+</summary>\s*\n\n.*?\n</details>',
    re.DOTALL,
)

# 脚注过滤：纯页码或数字编号
_RE_PAGE_NUMBER = re.compile(r'^[\s·]*\d+[\s·]*$')
_RE_REF_NUMBER = re.compile(r'^\(\d+(?:,\s*\d+)*\)$')  # (178) (127, 203, 321)

# 脚注区域阈值。⚠️ 已折成 0–1 归一化坐标（mineru_schema 负责折算四套 bbox 口径），
# 所以这个数**不再与页高相乘**。
FOOTNOTE_Y_THRESHOLD = 0.70

# 判据版本（落 page_footnotes.judge_version）。存量 64,256 条一律 'v1'，无需回填。
JUDGE_VERSION_V1 = "v1"
JUDGE_VERSION_V2 = "v2"
JUDGE_VERSION = JUDGE_VERSION_V2


def is_footnote_v2(block_type: str, y_top01: float | None) -> bool:
    """脚注判据 v2 —— **类型优先**。

    v1 的两头错（全库 64,256 条实测）：
      · 不筛类型 ⟹ `footer`（页脚冒充）**15,367 条 = 24%**；
      · 又叠加 `y >= 0.70` ⟹ **误杀真 `page_footnote` 80/588 = 13.6%**
        （实测 y/h min 0.415、p10 0.629）。
    ⟹ 类型判据已经够用，y 门槛在类型判据之上是**纯损失**。

    v2 的规则：
      · 类型 ∈ FOOTNOTE_TYPES（page_footnote / footnote）→ **收，不看 y**
      · 类型 ∈ NON_FOOTNOTE_TYPES（footer / header / page_number）→ 拒
      · 其余类型 → 回退底部区域判据（保住 v1 在无类型信息时的覆盖）
    """
    if block_type in FOOTNOTE_TYPES:
        return True
    if block_type in NON_FOOTNOTE_TYPES:
        return False
    return y_top01 is not None and y_top01 >= FOOTNOTE_Y_THRESHOLD


def is_noise_text(text: str) -> bool:
    """纯页码 / 文献编号 / 过短 —— 只用于**回退分支**。

    类型明确的脚注不过这道闸：MinerU 说它是 `page_footnote` 就信它的类型判断，
    不再拿字数去二次猜测（v1 用 `len(t) < 3` 属于同类的二次猜测）。
    """
    t = text.strip()
    if len(t) < 3:
        return True
    return bool(_RE_PAGE_NUMBER.match(t) or _RE_REF_NUMBER.match(t))

# 图片 hash 直查的扩展名尝试顺序
_IMG_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif')


# ── 主类 ────────────────────────────────────────────────

class MineruPostprocessor:
    """MinerU 输出后处理器。"""

    def __init__(self) -> None:
        self._db = get_db()
        self._config = get_config()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    @staticmethod
    def load(md_path: Path) -> MineruProduct:
        """装载产物元数据（provenance / 脚注候选 / 图片类型映射）。

        判据全部在 core.mineru_schema（v3 目录 / v4 full / v4 flat 三形态）。
        调用方一次装载、多处使用，避免重复读 json；`detect` 与它同源。
        """
        return load_product(md_path)

    @staticmethod
    def detect(md_path: Path) -> bool:
        """检测是否为 MinerU 输出（三形态统一判据，见 mineru_schema.load_product）。"""
        return load_product(md_path).is_mineru

    def extract_footnotes(
        self,
        md_path: Path,
        book_guid: str,
    ) -> int:
        """提取脚注并入库 page_footnotes（判据 v2，写入 judge_version）。

        参数:
            md_path:   Markdown 文件路径。
            book_guid: documents_book 中已入库的 GUID。

        返回:
            入库的脚注条数（**尝试数**，与 v1 同口径，便于对账）。
        """
        product = load_product(md_path)
        if not product.is_mineru:
            return 0

        inserted = 0
        for blk in product.footnote_candidates:
            text = (blk.text or "").strip()
            if not text:
                continue
            if blk.type in FOOTNOTE_TYPES:
                pass                                  # 类型明确：信它
            elif blk.type in NON_FOOTNOTE_TYPES:
                continue                              # 页眉/页脚/页码：明确拒
            elif not is_footnote_v2(blk.type, blk.y_top01) or is_noise_text(text):
                continue                              # 其余：回退底部区域判据

            self._db.execute_sql(
                """INSERT IGNORE INTO page_footnotes
                   (book_guid, page_idx, footnote_text, footnote_type, bbox,
                    judge_version)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    book_guid,
                    blk.page_idx,
                    text,
                    blk.type,
                    None if blk.bbox01 is None else json.dumps(list(blk.bbox01)),
                    JUDGE_VERSION,
                ),
            )
            inserted += 1

        return inserted

    def prepare_images(
        self,
        md_text: str,
        source_dir: Path,
    ) -> str:
        """为 MinerU 图片引用做路径重映射。四层策略：

        ① VLM 描述精确匹配（content_list_v2.json → actual_path）
        ② hash 直查 images/ 目录（HASH.ext）
        ③ Everything 搜索裸 hash（兜底文件名匹配）
        ④ 保留原引用 + 移除 <details> 块

        参数:
            md_text:    原始 Markdown 文本。
            source_dir: MD 文件所在目录。

        返回:
            图片路径已重映射的 Markdown 文本。
        """
        image_map = self._build_image_map(source_dir)  # 可能为空 {}

        result_lines: list[str] = []
        md_lines = md_text.split("\n")
        i = 0
        while i < len(md_lines):
            line = md_lines[i]
            img_match = _RE_MINERU_IMG.search(line)
            if img_match:
                old_path = img_match.group(1)  # /resources/images/HASH
                hash_str = Path(old_path).name  # HASH
                resolved: str | None = None

                # ── 策略 ①：VLM 描述精确匹配 ──
                details_text = self._collect_details(md_lines, i + 1)
                if details_text and details_text in image_map:
                    resolved = image_map[details_text]

                # ── 策略 ②：hash 直查（images/ + Everything） ──
                if resolved is None:
                    resolved = self._resolve_by_hash(hash_str, source_dir)

                if resolved is not None:
                    new_line = line.replace(old_path, resolved)
                    result_lines.append(new_line)
                else:
                    # 策略 ④：保留原样
                    result_lines.append(line)

                # 跳过 <details> 块
                j = i + 1
                while j < len(md_lines):
                    if md_lines[j].strip() == "</details>":
                        i = j
                        break
                    j += 1
                else:
                    i += 1
                    continue
            else:
                result_lines.append(line)
            i += 1

        return "\n".join(result_lines)

    # ------------------------------------------------------------------
    # 内部方法：图片映射
    # ------------------------------------------------------------------

    def _resolve_by_hash(self, hash_str: str, source_dir: Path) -> str | None:
        """策略 ②：hash 直查文件名。两级降级：

        ②-a: images/HASH.{jpg,png,webp,bmp,gif}（本地目录）
        ②-b: Everything HTTP 搜索裸 hash（全局兜底）

        返回: 相对路径（如 'images/abc.jpg'）或绝对路径（Everything 命中），
              或 None。
        """
        # ── ②-a：本地 images/ 目录直查 ──
        images_dir = source_dir / "images"
        if images_dir.is_dir():
            for ext in _IMG_EXTENSIONS:
                candidate = images_dir / f"{hash_str}{ext}"
                if candidate.is_file():
                    return f"images/{hash_str}{ext}"

        # ── ②-b：Everything 搜索裸 hash ──
        try:
            everything_url = self._config.get("everything.http_url", "")
            if everything_url:
                url = (
                    f"{everything_url.rstrip('/')}/"
                    f"?search={urllib.parse.quote(hash_str)}"
                    f"&json=1&path_column=1&count=1"
                )
                resp = requests.get(url, timeout=5)
                resp.raise_for_status()
                results = resp.json()
                if results and isinstance(results, list) and len(results) > 0:
                    abs_path = results[0].get("path", "")
                    if abs_path and Path(abs_path).is_file():
                        # 返回绝对路径，image_pipeline 可处理
                        return abs_path
        except Exception:
            pass  # Everything 不可用时静默降级

        return None

    def _build_image_map(self, source_dir: Path) -> dict[str, str]:
        """从 content_list JSON 构建 {vlm_description: relative_path} 映射。

        优先 v2，回退 v1。
        """
        stem = self._find_md_stem(source_dir)
        if not stem:
            return {}

        for suffix in ["_content_list_v2.json", "_content_list.json"]:
            cl_path = source_dir / f"{stem}{suffix}"
            if cl_path.is_file():
                return self._parse_content_list(cl_path)

        return {}

    @staticmethod
    def _find_md_stem(source_dir: Path) -> str | None:
        """在目录中找到 MD 文件的 stem（不含扩展名）。"""
        for f in source_dir.glob("*.md"):
            return f.stem
        return None

    @staticmethod
    def _parse_content_list(cl_path: Path) -> dict[str, str]:
        """解析 content_list JSON，提取 image 映射。"""
        with open(cl_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        image_map: dict[str, str] = {}
        # content_list 结构: [[page_blocks], [page_blocks], ...]
        pages = data if isinstance(data, list) else data.get("pages", [])
        for page_blocks in pages:
            if not isinstance(page_blocks, list):
                continue
            for block in page_blocks:
                if block.get("type") != "image":
                    continue
                content = block.get("content", {})
                vlm_desc = content.get("content", "").strip()
                img_source = content.get("image_source", {})
                actual_path = img_source.get("path", "")

                if vlm_desc and actual_path:
                    image_map[vlm_desc] = actual_path

        return image_map

    @staticmethod
    def _collect_details(md_lines: list[str], start: int) -> str:
        """从 start 行开始收集 <details> 块中的纯文本内容。

        返回修剪后的描述文本，若格式不符则返回空字符串。
        """
        if start >= len(md_lines):
            return ""

        if not md_lines[start].strip().startswith("<details>"):
            return ""

        # 跳过 <details> 和 <summary> 行
        i = start + 1
        while i < len(md_lines):
            line = md_lines[i].strip()
            if line.startswith("<summary>") and line.endswith("</summary>"):
                i += 1
                break
            i += 1
        else:
            return ""

        # 收集纯文本直到 </details>
        texts: list[str] = []
        while i < len(md_lines):
            line = md_lines[i].strip()
            if line == "</details>":
                break
            if line and not line.startswith("<"):
                texts.append(line)
            i += 1

        return "\n".join(texts).strip()


# ── 全局单例 ──────────────────────────────────────────────

_postprocessor: MineruPostprocessor | None = None


def get_mineru_postprocessor() -> MineruPostprocessor:
    global _postprocessor
    if _postprocessor is None:
        _postprocessor = MineruPostprocessor()
    return _postprocessor
