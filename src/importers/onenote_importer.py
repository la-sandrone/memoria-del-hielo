"""
文件名: onenote_importer.py
摘要: OneNote 笔记导入器。
      遍历 OneNoteMdExporter 导出目录，读取 YAML front matter，
      提取 key_metadata（标题优先 / 前50字 fallback），
      自动 tag（子目录名），扫描图片引用，jieba 分词后入库 documents_onenote。
      tag 写入前逐元素校验（哨兵 OMNIA / SQL 保留字 / 结构性字符 → core.tags）。
依赖: core.config, core.db, core.image_pipeline, core.tokenizer, core.tags
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from ..core.config import get_config
from ..core.db import get_db
from ..core.image_pipeline import get_image_pipeline
from ..core.tags import split_tags, validate_single_tag, validate_write_tags
from ..core.tokenizer import tokenize

# YAML front matter: ---\nkey: value\n...\n---\n
_RE_YAML_FM = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n",
    re.DOTALL,
)


def parse_yaml_front_matter(text: str) -> dict[str, str]:
    """简易 YAML front matter 解析（仅 key: value 格式，无嵌套）。"""
    match = _RE_YAML_FM.match(text)
    if not match:
        return {}
    result: dict[str, str] = {}
    for line in match.group(1).split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip().strip("\"'")
    return result


def strip_yaml_front_matter(text: str) -> str:
    """移除 YAML front matter，返回正文。"""
    return _RE_YAML_FM.sub("", text, count=1)


def extract_key_metadata(md_text: str) -> str:
    """提取 key_metadata：标题优先，否则前50个 Unicode 可见字符。"""
    fm = parse_yaml_front_matter(md_text)
    title = fm.get("title", "")
    if title and not title.endswith("..."):
        return title
    body = strip_yaml_front_matter(md_text)
    visible = "".join(ch for ch in body if ch.isprintable() and ch not in "\n\r\t")
    return visible[:50]


class OneNoteImporter:
    """OneNote 笔记导入器。"""

    def __init__(self) -> None:
        cfg = get_config()
        self.default_bucket: str = cfg.get(
            "import.default_bucket_onenote", "OneNote笔记"
        )
        self.auto_tags_enabled: bool = True
        self._image_pipeline = get_image_pipeline()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def import_file(
        self,
        md_path: Path,
        bucket: str | None = None,
        manual_tags: str = "",
        auto_tags: bool = True,
    ) -> dict[str, Any]:
        """导入单个 OneNote .md 文件。

        返回: {"guid": str, "bucket": str, "key_metadata": str, "status": "ok|skipped"}
        """
        self.auto_tags_enabled = auto_tags
        bucket = bucket or self.default_bucket

        md_text = md_path.read_text(encoding="utf-8")

        key_meta = extract_key_metadata(md_text)
        tags = self._build_tags(md_path, manual_tags)

        # 图片管线
        source_dir = md_path.parent
        md_text = self._image_pipeline.process_image_refs(md_text, source_dir)

        # jieba 分词 → content_tokenized
        text_tokenized = tokenize(md_text)

        guid = str(uuid.uuid4())
        db = get_db()
        db.execute_sql(
            """INSERT INTO documents_onenote
               (guid, bucket, tags, key_metadata, content, content_tokenized)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (guid, bucket, tags, key_meta, md_text, text_tokenized),
        )

        return {
            "guid": guid,
            "bucket": bucket,
            "key_metadata": key_meta,
            "status": "ok",
        }

    def import_directory(
        self,
        dir_path: Path,
        bucket: str | None = None,
        manual_tags: str = "",
        auto_tags: bool = True,
        recursive: bool = True,
    ) -> list[dict[str, Any]]:
        """批量导入目录下所有 .md 文件。

        返回: list[import_file 结果]
        """
        self.auto_tags_enabled = auto_tags
        results: list[dict[str, Any]] = []
        pattern = "**/*.md" if recursive else "*.md"
        for md_file in sorted(dir_path.glob(pattern)):
            if md_file.name.startswith("_"):
                continue
            try:
                result = self.import_file(md_file, bucket, manual_tags, auto_tags)
                results.append(result)
            except Exception as exc:
                results.append({
                    "file": str(md_file),
                    "status": "error",
                    "error": str(exc),
                })
        return results

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _build_tags(self, md_path: Path, manual_tags: str) -> str:
        """合并自动 tag（子目录名）和手动 tag；两者写入前都过 core.tags 校验。"""
        tags: list[str] = []
        if self.auto_tags_enabled:
            parent = md_path.parent.name
            if parent and parent != ".":
                # 单值来源：目录名含逗号会被静默切成两个 tag，故单独报错
                tags.append(validate_single_tag(parent, "OneNote 子目录名自动 tag"))
        for t in split_tags(validate_write_tags(manual_tags)):
            if t not in tags:
                tags.append(t)
        return ",".join(tags)


# ── CLI 便捷入口 ──────────────────────────────────────────

def import_onenote_file(
    path_str: str,
    bucket: str | None = None,
    tags: str = "",
    auto_tags: bool = True,
) -> dict[str, Any]:
    importer = OneNoteImporter()
    return importer.import_file(Path(path_str), bucket, tags, auto_tags)


def import_onenote_directory(
    path_str: str,
    bucket: str | None = None,
    tags: str = "",
    auto_tags: bool = True,
    recursive: bool = True,
) -> list[dict[str, Any]]:
    importer = OneNoteImporter()
    return importer.import_directory(Path(path_str), bucket, tags, auto_tags, recursive)
