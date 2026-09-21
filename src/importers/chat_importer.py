"""
文件名: chat_importer.py
摘要: AI 对话导入器。
      输入 Chatbox 导出 .md，清洗 → 生成摘要 → jieba 分词 → 嵌入 → 入库。
依赖: core.config, core.db, core.embedding, core.tokenizer, core.tags
      cleaners.chat_cleaner
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import numpy as np

from ..core.config import get_config
from ..core.db import get_db
from ..core.embedding import get_embedding_service
from ..core.tags import validate_write_tags
from ..core.tokenizer import tokenize
from ..cleaners.chat_cleaner import ChatCleaner


class ChatImporter:
    """AI 对话导入器。"""

    def __init__(self) -> None:
        cfg = get_config()
        self.default_bucket: str = cfg.get("import.default_bucket_chat", "AI对话")
        self._embedding = get_embedding_service()
        self._cleaner = ChatCleaner()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def import_file(
        self,
        file_path: Path,
        bucket: str | None = None,
        manual_tags: str = "",
        no_clean: bool = False,
        manual_summary: str | None = None,
    ) -> dict[str, Any]:
        bucket = bucket or self.default_bucket
        raw_text = file_path.read_text(encoding="utf-8", errors="replace")

        # 清洗
        cleaned_text = raw_text if no_clean else self._cleaner.clean(raw_text)

        # 摘要 → key_metadata
        key_meta = manual_summary or self._cleaner.generate_summary(raw_text)

        # jieba 分词 → content_tokenized
        text_tokenized = tokenize(cleaned_text)

        # 嵌入（documents_chat.embedding 为 NOT NULL）
        embedding_vec = self._embedding.embed_one(cleaned_text)
        emb_bytes = np.asarray(embedding_vec, dtype=np.float32).tobytes()

        # 入库
        guid = str(uuid.uuid4())
        tags = validate_write_tags(manual_tags)   # 哨兵/保留字/结构性字符就地拦截
        db = get_db()
        db.execute_sql(
            """INSERT INTO documents_chat
               (guid, bucket, tags, key_metadata, content, content_tokenized, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (guid, bucket, tags, key_meta, cleaned_text, text_tokenized, emb_bytes),
        )

        return {"guid": guid, "bucket": bucket, "key_metadata": key_meta, "status": "ok"}

    def import_directory(
        self,
        dir_path: Path,
        bucket: str | None = None,
        manual_tags: str = "",
        no_clean: bool = False,
        manual_summary: str | None = None,
        recursive: bool = True,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        pattern = "**/*.md" if recursive else "*.md"
        for md_file in sorted(dir_path.glob(pattern)):
            if md_file.name.startswith("."):
                continue
            try:
                result = self.import_file(md_file, bucket, manual_tags, no_clean, manual_summary)
                result["file"] = str(md_file)
                results.append(result)
            except Exception as exc:
                results.append({"file": str(md_file), "status": "error", "error": str(exc)})
        return results


# ── CLI 便捷入口 ──────────────────────────────────────────

def import_chat_file(
    path_str: str, bucket: str | None = None, tags: str = "",
    no_clean: bool = False, summary: str | None = None,
) -> dict[str, Any]:
    return ChatImporter().import_file(Path(path_str), bucket, tags, no_clean, summary)


def import_chat_directory(
    path_str: str, bucket: str | None = None, tags: str = "",
    no_clean: bool = False, summary: str | None = None, recursive: bool = True,
) -> list[dict[str, Any]]:
    return ChatImporter().import_directory(
        Path(path_str), bucket, tags, no_clean, summary, recursive,
    )
