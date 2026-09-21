"""
文件名: mineru_postprocess.py
摘要: MinerU 输出后处理——脚注提取 + 图片路径重映射。
      检测 MD 同目录的 _middle.json 判定 MinerU 输出。
      从 middle.json → discarded_blocks 提取脚注，
      图片映射四层策略：VLM 描述精确匹配 → images/ hash 直查
      → Everything 搜索裸 hash → 保留原样。
依赖: json, re, pathlib, urllib.parse, requests
      core.config, core.db
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

# 脚注高度阈值：y > 页面高度 × THRESHOLD 视为脚注区域
FOOTNOTE_Y_THRESHOLD = 0.70

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
    def detect(md_path: Path) -> bool:
        """检测是否为 MinerU 输出。

        判据：MD 同目录下存在 {stem}_middle.json。
        """
        middle = md_path.parent / f"{md_path.stem}_middle.json"
        return middle.is_file()

    def extract_footnotes(
        self,
        md_path: Path,
        book_guid: str,
    ) -> int:
        """从 middle.json 提取脚注并入库 page_footnotes。

        参数:
            md_path:   Markdown 文件路径（用于定位 middle.json）。
            book_guid: documents_book 中已入库的 GUID。

        返回:
            入库的脚注条数。
        """
        middle_path = md_path.parent / f"{md_path.stem}_middle.json"
        if not middle_path.is_file():
            raise FileNotFoundError(f"middle.json not found: {middle_path}")

        with open(middle_path, "r", encoding="utf-8") as f:
            middle = json.load(f)

        pdf_info = middle.get("pdf_info", [])
        inserted = 0

        for page in pdf_info:
            page_idx = page.get("page_idx", -1)
            if page_idx < 0:
                continue

            page_height = page.get("page_size", [0, 0])[1]
            if page_height <= 0:
                continue

            discarded = page.get("discarded_blocks", [])
            for block in discarded:
                text = self._extract_block_text(block)
                if not text or not text.strip():
                    continue

                # 过滤非脚注
                if self._is_noise(text, block):
                    continue

                # 位置过滤：需在页面底部 30% 区域
                bbox = block.get("bbox", [0, 0, 0, 0])
                y = bbox[1] if len(bbox) >= 2 else 0
                if y < page_height * FOOTNOTE_Y_THRESHOLD:
                    continue

                block_type = block.get("type", "text")
                bbox_json = json.dumps(bbox)

                self._db.execute_sql(
                    """INSERT IGNORE INTO page_footnotes
                       (book_guid, page_idx, footnote_text, footnote_type, bbox)
                       VALUES (?, ?, ?, ?, ?)""",
                    (book_guid, page_idx, text.strip(), block_type, bbox_json),
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
    # 内部方法：脚注
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_block_text(block: dict[str, Any]) -> str:
        """从 discarded_block 中提取文本。"""
        lines = block.get("lines", [])
        texts: list[str] = []
        for line in lines:
            for span in line.get("spans", []):
                if span.get("type") == "text":
                    texts.append(span.get("content", ""))
                elif span.get("type") == "inline_equation":
                    content = span.get("content", "")
                    texts.append(f"${content}$")
        return " ".join(texts)

    @staticmethod
    def _is_noise(text: str, block: dict[str, Any]) -> bool:
        """判断是否应排除的噪音块。"""
        t = text.strip()

        # 页码
        if block.get("type") == "page_number":
            return True

        # 纯数字 / 点包围数字
        if _RE_PAGE_NUMBER.match(t):
            return True

        # 文献编号引用 (178) (127, 203, 321)
        if _RE_REF_NUMBER.match(t):
            return True

        # 页眉/页脚空内容
        if len(t) < 3:
            return True

        return False

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
