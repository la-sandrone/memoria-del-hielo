"""
文件名: book_importer.py
摘要: 图书/游戏剧情导入器 v2（chunk 层）。
      每本书一个事务：documents_book（书目向量 = 书名+目录）+
      documents_book_chunk（结构感知切块，≤2500 字符，8192 token 物理上限内）
      原子提交，中途失败整书回滚。
      source_path_hash 唯一约束 → 幂等导入/断点恢复（重跑同命令自动跳过已导书）；
      嵌入走 embed_batch（逐条缓存 + 分批 HTTP），重跑零网络请求。
      目录递归模式只取 *.md（避开 MinerU 的 _origin.pdf/_layout.pdf）；
      分类 tag = 扫描根目录名（搬家按分类目录逐个调用）。
      写入前 tag 逐元素校验（哨兵 OMNIA / SQL 保留字 / 结构性字符 → core.tags）。
依赖: core.config, core.db, core.embedding, core.image_pipeline,
      core.tokenizer, core.tags, core.chunker, core.mineru_postprocess
      PyMuPDF (可选), ebooklib (可选)
"""

from __future__ import annotations
import os
import hashlib
import json
import logging
import sys
import threading
import time
import uuid

import mariadb
from pathlib import Path
from typing import Any, Callable


def _to_windows_path(p: str) -> str:
    r"/mnt/<drive>/xxx → D:\xxx（LLM 消费视角的 Windows 路径）；其余原样。"
    if p.startswith("/mnt/") and len(p) > 6 and p[6] == "/":
        return p[5].upper() + ":\\" + p[7:].replace("/", "\\")
    return p

import numpy as np
from ..cleaners.text_cleaner import collapse_whitespace
from ..core.chunker import chunk_markdown, build_book_card
from ..core.config import get_config
from ..core.db import get_db
from ..core.embedding import get_embedding_service
from ..core.image_pipeline import get_image_pipeline
from ..core.mineru_postprocess import MineruPostprocessor, get_mineru_postprocessor
from ..core.tags import validate_single_tag, validate_write_tags
from ..core.tokenizer import tokenize

logger = logging.getLogger(__name__)

# ── Ctrl+C 两级语义（跨子目录共享） ─────────────────────────
# 第一次中断 = 跳过当前书，继续下一本；第二次 = 停止全部。
_INTERRUPTS: list[int] = [0]  # 中断计数（list 可变对象，函数内改元素无需 global 声明）


class ProgressTracker:
    """并行导入的线程安全进度状态（每本书的当前阶段）。

    worker 写（update：阶段 + done/total），主循环渲染
    （rich 表格或纯文本汇总行）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, tuple[str, int, int]] = {}

    def update(
        self, book_name: str, stage: str, done: int = 0, total: int = 0
    ) -> None:
        with self._lock:
            self._state[book_name] = (stage, done, total)

    def snapshot_all(self) -> dict[str, tuple[str, int, int]]:
        with self._lock:
            return dict(self._state)

    def snapshot(self, names: list[str]) -> str:
        with self._lock:
            return ", ".join(
                f"{n}·{self._state.get(n, ('…', 0, 0))[0]}" for n in names
            )


def reset_interrupts() -> None:
    """重置中断计数（每次导入入口调用一次）。"""
    _INTERRUPTS[0] = 0


class BookImporter:
    """图书/游戏剧情导入器（chunk 层）。"""

    def __init__(self) -> None:
        cfg = get_config()
        self.default_bucket_book: str = cfg.get("import.default_bucket_book", "图书")
        self.default_bucket_game: str = cfg.get("import.default_bucket_game", "游戏剧情")
        chunk_cfg = cfg.get("chunking", {})
        self.target_chars: int = int(chunk_cfg.get("target_chars", 2000))
        self.max_chars: int = int(chunk_cfg.get("max_chars", 2500))
        self.min_chars: int = int(chunk_cfg.get("min_chars", 400))
        self.overlap_chars: int = int(chunk_cfg.get("overlap_chars", 300))
        self.embed_batch_size: int = int(chunk_cfg.get("embed_batch_size", 32))
        self._embedding = get_embedding_service()
        self._image_pipeline = get_image_pipeline()
        self._mineru = get_mineru_postprocessor()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def import_file(
        self,
        file_path: Path,
        bucket: str | None = None,
        manual_tags: str = "",
        category_tag: str = "",
        force: bool = False,
        verbose: bool = True,
        progress_tracker: ProgressTracker | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """导入单个图书文件（书 + 全部 chunk 单事务原子提交）。

        返回: {"guid", "bucket", "key_metadata", "chunks", "footnotes",
               "mineru", "file", "status"}；
        已导入且未 force → status="skipped"（幂等/断点恢复）。
        """
        source_path = str(file_path.resolve())
        # LLM 消费视角：存 Windows 风格路径（E:\...），检索结果直接可读
        source_path = _to_windows_path(source_path)
        source_hash = hashlib.md5(source_path.encode("utf-8")).hexdigest()
        db = get_db()

        existing = db.execute_sql(
            "SELECT b.guid, b.key_metadata, b.status, b.expected_chunks, "
            "(SELECT COUNT(*) FROM documents_book_chunk c "
            " WHERE c.book_guid = b.guid) AS actual_chunks "
            "FROM documents_book b WHERE b.source_path_hash = ?",
            (source_hash,),
        )
        if existing:
            row = existing[0]
            complete = (
                row["status"] == "done"
                and row["actual_chunks"] == row["expected_chunks"]
            )
            if not force and complete:
                return {
                    "guid": row["guid"],
                    "key_metadata": row["key_metadata"],
                    "file": source_path,
                    "status": "skipped",
                }
            # force / 未完成（processing，一段后崩溃）/ 破损（done 但
            # chunks 数不符，如 commit 响应丢失）→ 删旧重导
            # 注意：page_footnotes 自 2026-09-21 起有 fk_footnotes_book 级联，但仍先按旧
            # guid 显式清一遍——老库/未打 FK 的环境同样正确，且不依赖级联行为
            old_guids = [r["guid"] for r in existing]
            if old_guids:
                db.execute_sql(
                    "DELETE FROM page_footnotes WHERE book_guid IN (%s)"
                    % ",".join("?" for _ in old_guids),
                    tuple(old_guids),
                )
            db.execute_sql(
                "DELETE FROM documents_book WHERE source_path_hash = ?",
                (source_hash,),
            )

        bucket = bucket or self.default_bucket_book
        key_meta = self._extract_book_name(file_path)
        source_dir = file_path.parent
        is_mineru = MineruPostprocessor.detect(file_path)

        # 阶段进度（stderr：MCP stdio 下 stdout 是协议通道）
        # 固定宽度填充：\r 刷新时防止短行残留（书级行已独立成行）
        # 并行导入（progress_tracker 传入）：阶段写入 tracker（线程安全），
        # 汇总行在书完成事件时 snapshot；verbose=False 且无 tracker 时静默
        def _stage(stage: str, done: int = 0, total: int = 0) -> None:
            if progress_tracker is not None:
                progress_tracker.update(file_path.name, stage, done, total)
                return
            if not verbose:
                return
            msg = stage if total <= 0 else f"{stage} {done}/{total}"
            print(f"\r  · {msg:<44}", file=sys.stderr, end="", flush=True)

        # 1. 提取文本
        text = self._extract_text(file_path)
        _stage("读取文本")

        # 2. MinerU 预处理：图片路径重映射（四层降级链）
        if is_mineru:
            logger.info("检测到 MinerU 输出，预处理图片引用...")
            text = self._mineru.prepare_images(text, source_dir)

        # 3. 图片管线（OCR + MD5 + 硬链接）
        # MinerU layout 判据：json 里图片的 block 类型映射（照片/公式类跳过 OCR）
        mineru_type_map = self._load_mineru_image_types(source_dir, file_path.stem)
        text = self._image_pipeline.process_image_refs(
            text,
            source_dir,
            progress=lambda done, total: _stage("图片", done, total),
            image_type_map=mineru_type_map,
            stop_check=stop_check,
        )

        text = collapse_whitespace(text)  # 3.5 文本清洗：空白折叠（变长，PUA 不动）；随后切块（护栏在 chunker 内）
        chunks = chunk_markdown(
            text,
            target_chars=self.target_chars,
            max_chars=self.max_chars,
            min_chars=self.min_chars,
            overlap_chars=self.overlap_chars,
        )
        if not chunks:
            raise ValueError(f"切块结果为空: {file_path}")
        _stage("切块", len(chunks), len(chunks))

        # 5. 嵌入：书目向量（书名+目录，"找书"用）+ chunk 向量（批量、带缓存）
        book_card = build_book_card(key_meta, text)
        book_vec = self._embedding.embed_one(book_card)
        chunk_vecs = self._embedding.embed_batch(
            [c.content for c in chunks],
            max_batch=self.embed_batch_size,
            progress=lambda done, total: _stage("嵌入", done, total),
            stop_check=stop_check,
        )

        def _emb_bytes(vec: list[float]) -> bytes:
            return np.asarray(vec, dtype=np.float32).tobytes()

        # 5.5 一段记账（状态机：二段入库完成才转 done）：
        # 书记录提前落库（status='processing'，expected_chunks 为校验锚点）——
        # 崩溃在二段 → 重导识别 processing 强制重做；
        # commit 响应丢失但实际提交 → done 且 chunks 数不符 → 强制重导
        guid = str(uuid.uuid4())
        # tag 语法校验（core.tags 是唯一真相源）：
        #   manual_tags 多值来源（--tags a,b）→ 整体校验后逐元素规范化；
        #   category_tag 单值来源（目录名）→ 含逗号会被静默切成两个 tag，故单独报错
        tags = ",".join(
            t for t in (
                validate_write_tags(manual_tags),
                validate_single_tag(category_tag, "目录名自动 tag"),
            ) if t
        )
        # content 超长分块：mariadb 客户端驱动 max_allowed_packet=16MB
        # （驱动未暴露连接参数，无法调高）——GB18030→UTF-8 膨胀 1.5 倍的
        # 超长文本（如 13MB 校对版 txt → 19.7MB）单参数会被驱动截断，
        # 服务器收到不完整 UTF-8 → Incorrect string value。
        # 方案：INSERT 带首块，余块 UPDATE CONCAT 追加；
        # 中途崩溃 → content 不完整 + status='processing' → 重导强制重做。
        content_parts = _chunk_utf8(text, _MAX_CONTENT_CHUNK)
        db.execute_sql(
            """INSERT INTO documents_book
               (guid, source_path_hash, source_path, bucket, tags,
                key_metadata, content, embedding,
                status, expected_chunks)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'processing', ?)""",
            (
                guid,
                source_hash,
                source_path,
                bucket,
                tags,
                key_meta,
                content_parts[0],
                _emb_bytes(book_vec),
                len(chunks),
            ),
        )
        for part in content_parts[1:]:
            db.execute_sql(
                "UPDATE documents_book SET content = CONCAT(content, ?) "
                "WHERE guid = ?",
                (part, guid),
            )
        chunk_rows = [
            (
                guid,
                c.chunk_seq,
                c.heading_path,
                c.char_start,
                c.char_len,
                c.visible_start,
                c.visible_len,
                c.byte_start,
                c.byte_len,
                c.grapheme_len,
                int(c.has_pua),
                c.content,
                tokenize(c.content),
                _emb_bytes(chunk_vecs[i]),
            )
            for i, c in enumerate(chunks)
        ]

        # 6. 入库（单事务：书 + 全部 chunk；chunk 分批插入——
        #    整包可能超 max_allowed_packet 被服务端断连（104）；连接异常重试一次）
        for _attempt in range(2):
            try:
                with db.transaction() as conn:
                    with conn.cursor(dictionary=True) as cur:
                        # 二段：chunks 插入 + 状态转 done（同一事务，原子）——
                        # 崩溃在事务前 → status='processing'，重导识别强制重做
                        for i in range(0, len(chunk_rows), 200):
                            cur.executemany(
                                """INSERT INTO documents_book_chunk
                                   (book_guid, chunk_seq, heading_path, char_start, char_len,
                                    visible_start, visible_len, byte_start, byte_len,
                                    grapheme_len, has_pua,
                                    content, content_tokenized, embedding)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                chunk_rows[i : i + 200],
                            )
                        cur.execute(
                            "UPDATE documents_book SET status = 'done' "
                            "WHERE guid = ?",
                            (guid,),
                        )
                break
            except mariadb.OperationalError:
                if _attempt == 1:
                    raise
                logger.warning(
                    "入库连接异常（可能 104），重试第 %d 次", _attempt + 1
                )

        # 7. MinerU 脚注（事务外增强数据：失败记日志不回滚——
        #    脚注是锦上添花，书+chunk 才是检索必需）
        footnote_count = 0
        if is_mineru:
            try:
                logger.info("提取 MinerU 脚注...")
                footnote_count = self._mineru.extract_footnotes(file_path, guid)
                logger.info("脚注提取完成: %s 条", footnote_count)
            except Exception:
                logger.exception("脚注提取失败（书已入库，不影响检索）: %s", file_path)

        # 结束阶段进度行（换行收尾）
        print(file=sys.stderr, flush=True)
        return {
            "guid": guid,
            "bucket": bucket,
            "key_metadata": key_meta,
            "chunks": len(chunks),
            "footnotes": footnote_count,
            "mineru": is_mineru,
            "file": source_path,
            "status": "ok",
        }

    def import_directory(
        self,
        dir_path: Path,
        bucket: str | None = None,
        manual_tags: str = "",
        recursive: bool = True,
        force: bool = False,
        parallel: int = 1,
    ) -> list[dict[str, Any]]:
        """递归导入目录下全部 .md（MinerU 书库搬家入口）。

        · 只 glob *.md —— 避开每本书 _origin.pdf/_layout.pdf（各 40MB+）
        · 分类 tag = 扫描根目录名（如"地质学"）→ 搬家按分类目录逐个调用
        · 幂等：已导书自动跳过 → 断点恢复 = 重跑同一条命令
        · 并行：parallel > 1 时书级 ThreadPoolExecutor（IO 密集：嵌入/OCR
          走 HTTP，server_bge/server_paddle 侧排队限流）
        · 急停：Ctrl+C 第一次中断当前批（已启动的书跑完入库），第二次全停
        """
        results: list[dict[str, Any]] = []
        category_tag = dir_path.name
        md_files = _safe_glob_src(dir_path, recursive)
        if not md_files:
            return results

        # 并行路径：书级粒度，线程池（每本书独立事务，单书失败不影响其他）
        if parallel > 1 and len(md_files) > 1:
            from concurrent.futures import ThreadPoolExecutor

            tracker = ProgressTracker()  # 并行进度状态（每本书的当前阶段）

            # rich 进度表格（仅 tty；非终端退化为纯文本完成行——
            # MCP stdio / 重定向场景 stderr 不是终端）
            progress = None
            task_total = None
            book_tasks: dict[str, Any] = {}
            if sys.stderr.isatty():
                try:
                    from rich.console import Console
                    from rich.progress import (
                        BarColumn,
                        Progress,
                        SpinnerColumn,
                        TextColumn,
                        TimeRemainingColumn,
                    )
                    progress = Progress(
                        SpinnerColumn(),
                        TextColumn(
                            "[progress.description]{task.description}"
                        ),
                        BarColumn(bar_width=24),
                        TextColumn(
                            "[progress.percentage]{task.percentage:>3.0f}%"
                        ),
                        TimeRemainingColumn(),
                        console=Console(stderr=True),
                    )
                    progress.start()
                    task_total = progress.add_task(
                        "[bold]总进度[/bold]", total=len(md_files)
                    )
                except ImportError:
                    logger.warning(
                        "rich 未安装，进度降级为纯文本（uv pip install rich）"
                    )
                    progress = None

            def _one(file_path: Path) -> dict[str, Any]:
                return self.import_file(
                    file_path, bucket, manual_tags, category_tag, force,
                    verbose=False, progress_tracker=tracker,
                    stop_check=lambda: _INTERRUPTS[0] >= 1,
                )

            try:
                with ThreadPoolExecutor(max_workers=parallel) as pool:
                    futures = {
                        pool.submit(_one, f): f for f in md_files
                    }
                    pending = set(futures)
                    done_count = 0
                    book_total = len(md_files)
                    while pending:
                        # time.sleep 是可靠的 KeyboardInterrupt 接收点
                        # （as_completed/fut.result() 阻塞中 Ctrl+C 传播不可靠——
                        # 业界已知坑）；同时承担 2s 周期进度刷新（时间驱动，
                        # 长任务书完成前也能看到每本书当前阶段）。
                        try:
                            time.sleep(2.0)
                        except KeyboardInterrupt:
                            _INTERRUPTS[0] += 1
                            if _INTERRUPTS[0] >= 2:
                                logger.warning(
                                    "用户再次中断：停止全部导入（已导书保留）"
                                )
                                raise
                            logger.warning(
                                "用户中断：并行导入暂停（再按一次 Ctrl+C 停止全部）"
                            )
                            # 取消未开始任务；已启动的 worker 在图片逐张/嵌入逐批
                            # 边界检查 stop_check 后快速退出（当前书事务回滚）
                            for fut in futures:
                                fut.cancel()
                            continue

                        # 周期刷新：每本书的当前阶段（时间驱动）
                        if progress is not None:
                            for name, (stage, done, ttl) in (
                                tracker.snapshot_all().items()
                            ):
                                tid = book_tasks.get(name)
                                if tid is None:
                                    tid = progress.add_task(
                                        name, total=ttl or None
                                    )
                                    book_tasks[name] = tid
                                progress.update(
                                    tid,
                                    description=f"{name} · {stage}",
                                    completed=done,
                                    total=ttl or None,
                                )
                            progress.update(
                                task_total,
                                completed=done_count,
                                total=len(md_files),
                            )
                        else:
                            running = [futures[x].name for x in pending]
                            if running:
                                line = tracker.snapshot(running)
                                print(
                                    f"\r⏳ 并行中: {line}…",
                                    file=sys.stderr, end="", flush=True,
                                )

                        # 收集已完成（worker 的 KeyboardInterrupt 在此标记
                        # interrupted，不冒泡——它属于第一次中断的快速中止）
                        for fut in [f for f in pending if f.done()]:
                            pending.discard(fut)
                            if fut.cancelled():
                                continue  # 未启动被取消的任务，无结果
                            file_path = futures[fut]
                            try:
                                result = fut.result()
                                results.append(result)
                                status = result.get("status", "?")
                                chunks = result.get("chunks", "-")
                            except KeyboardInterrupt:
                                results.append(
                                    {"file": str(file_path),
                                     "status": "interrupted"}
                                )
                                status, chunks = "interrupted", "-"
                            except Exception as exc:
                                logger.exception("导入失败: %s", file_path)
                                results.append(
                                    {"file": str(file_path), "status": "error",
                                     "error": str(exc)}
                                )
                                status, chunks = "error", "-"
                            done_count += 1
                            print(
                                f"[{done_count}/{book_total}] {file_path.name} "
                                f"→ {status}（{chunks} chunks）",
                                file=sys.stderr, flush=True,
                            )
                            if progress is not None:
                                tid = book_tasks.get(file_path.name)
                                if tid is not None:
                                    # 书完成：进度条打满转绿，描述带状态
                                    progress.update(
                                        tid,
                                        description=(
                                            f"{file_path.name} → {status}"
                                        ),
                                        completed=1,
                                        total=1,
                                    )
                                progress.update(
                                    task_total,
                                    completed=done_count,
                                    total=len(md_files),
                                )
            except KeyboardInterrupt:
                # 第二次中断：冒泡到 CLI（os._exit 立即终止，不等待
                # ThreadPoolExecutor.shutdown(wait=True) 的 worker 清理）
                raise
            finally:
                if progress is not None:
                    progress.stop()
            print(file=sys.stderr, flush=True)  # 结束并行 \r 汇总行
            return results

        # 串行路径（parallel == 1：书级进度 + 阶段进度）
        for idx, file_path in enumerate(md_files, 1):
            # 书级进度（stderr：MCP stdio 下 stdout 是协议通道）——独立一行
            print(
                f"[{idx}/{len(md_files)}] {file_path.name}",
                file=sys.stderr,
                flush=True,
            )
            try:
                result = self.import_file(
                    file_path, bucket, manual_tags, category_tag, force
                )
                results.append(result)
                logger.info(
                    "[%d/%d] %s: %s",
                    idx,
                    len(md_files),
                    result.get("status"),
                    result.get("key_metadata", file_path.stem),
                )
            except KeyboardInterrupt:
                _INTERRUPTS[0] += 1
                if _INTERRUPTS[0] >= 2:
                    logger.warning("用户再次中断：停止全部导入（已导书保留）")
                    raise
                logger.warning(
                    "用户中断: %s（该书未入库；再按一次 Ctrl+C 停止全部）",
                    file_path,
                )
                results.append({"file": str(file_path), "status": "interrupted"})
                continue
            except Exception as exc:
                logger.exception("导入失败: %s", file_path)
                results.append(
                    {"file": str(file_path), "status": "error", "error": str(exc)}
                )
        return results

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_book_name(file_path: Path) -> str:
        return file_path.stem

    @staticmethod
    def _load_mineru_image_types(
        source_dir: Path | None, md_stem: str
    ) -> dict[str, str]:
        """读 MinerU _content_list_v2.json，构建 {图片路径: block类型} 映射。

        只收集与 OCR 决策相关的类型（image/table/chart/equation_interline）。
        无 json（非 MinerU 产物）→ 空映射（全部图片正常 OCR）。
        """
        if not source_dir:
            return {}
        jp = None
        for suffix in ("_content_list_v2.json", "_content_list.json"):
            cand = source_dir / f"{md_stem}{suffix}"
            if cand.is_file():
                jp = cand
                break
        if jp is None:
            return {}
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        mapping: dict[str, str] = {}
        for page in data or []:
            for block in page or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype not in ("image", "table", "chart", "equation_interline"):
                    continue
                content = block.get("content")
                if not isinstance(content, dict):
                    continue
                src = content.get("image_source")
                if isinstance(src, dict) and src.get("path"):
                    mapping[src["path"]] = btype
        if mapping:
            logger.info("MinerU layout 映射: %d 张图片分类（跳过 OCR 类型: %s）",
                        len(mapping), sorted(set(mapping.values())))
        return mapping

    @staticmethod
    def _extract_text(file_path: Path) -> str:
        ext = file_path.suffix.lower()
        if ext == ".md":
            return file_path.read_text(encoding="utf-8", errors="replace")
        if ext == ".txt":
            return _read_text_detect_encoding(file_path)
        if ext == ".epub":
            return _extract_epub(file_path)
        if ext == ".pdf":
            return _extract_pdf(file_path)
        raise ValueError(f"不支持的格式: {ext}")


# ── 格式特定提取 ──────────────────────────────────────────

def _read_text_detect_encoding(path: Path) -> str:
    """txt 编码检测：BOM → utf-8 严格 → chardet 高置信非中文 → gb18030。

    顺序理由：现代文本几乎都是 utf-8（严格解码即可判定）；
    中文老书（GBK/GB2312）统一 gb18030 超集解码（几乎必成功）；
    chardet 对中文 GB 文本常误判为西里尔系，故仅在其高置信且
    非中文编码时采信；其余落 gb18030。
    """
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        import chardet

        det = chardet.detect(raw)
        enc = det.get("encoding") or ""
        conf = det.get("confidence", 0.0)
        norm = enc.lower().replace("_", "-")
        if (
            enc
            and conf >= 0.9
            and norm not in ("gb2312", "gbk", "gb18030", "big5", "utf-8")
        ):
            return raw.decode(enc, errors="replace")
    except Exception:
        pass
    try:
        return raw.decode("gb18030")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


# 单参数最大字节数：mariadb 客户端驱动默认 max_allowed_packet=16MB，
# 留 1MB 给 SQL 语句与其他参数（embedding 等）的包开销
_MAX_CONTENT_CHUNK = 15 * 1024 * 1024


def _chunk_utf8(text: str, limit_bytes: int) -> list[str]:
    """按 UTF-8 字节数分块（不切坏多字节字符；text 非空返回至少一块）。

    用于超长 content 的入库分块（CONCAT 追加），避免单参数超过驱动
    max_allowed_packet 被截断。二分定位每块边界：str 切片按字符，
    encode 测字节数，保证块边界落在字符边界上。
    """
    if not text:
        return [""]
    if len(text.encode("utf-8")) <= limit_bytes:
        return [text]
    parts: list[str] = []
    rest = text
    while rest:
        lo, hi = 0, len(rest)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(rest[:mid].encode("utf-8")) <= limit_bytes:
                lo = mid
            else:
                hi = mid - 1
        if lo == 0:
            lo = 1  # 防御：单字符超限（limit_bytes 远大于单字符时不会发生）
        parts.append(rest[:lo])
        rest = rest[lo:]
    return parts


def _extract_epub(path: Path) -> str:
    try:
        import ebooklib
        from ebooklib import epub
    except ImportError:
        raise ImportError("EPUB 提取需要 ebooklib: pip install 'memoria-del-hielo[epub]'")

    import re
    book = epub.read_epub(str(path))
    texts: list[str] = []
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            content = item.get_body_content()
            if content:
                text = re.sub(r"<[^>]+>", " ", content.decode("utf-8", errors="replace"))
                texts.append(text.strip())
    return "\n\n".join(texts)


def _extract_pdf(path: Path) -> str:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise ImportError("PDF 提取需要 PyMuPDF: pip install 'memoria-del-hielo[pdf]'")

    doc = fitz.open(str(path))
    texts: list[str] = [page.get_text() for page in doc]
    doc.close()
    return "\n\n".join(texts)


# ── CLI 便捷入口 ──────────────────────────────────────────

def import_book_file(
    path_str: str,
    bucket: str | None = None,
    tags: str = "",
    category_tag: str = "",
    force: bool = False,
) -> dict[str, Any]:
    return BookImporter().import_file(Path(path_str), bucket, tags, category_tag, force)


def import_book_directory(
    path_str: str,
    bucket: str | None = None,
    tags: str = "",
    recursive: bool = True,
    force: bool = False,
    parallel: int = 1,
) -> list[dict[str, Any]]:
    if parallel > 2:
        logger.warning(
            "parallel=%d > 2：documents_book_chunk 两个 FULLTEXT 索引并发插入互斥，"
            "锁竞争极大（Lock wait timeout 风险），建议降到 2 或 1",
            parallel,
        )
    return BookImporter().import_directory(
        Path(path_str), bucket, tags, recursive, force, parallel
    )


def _safe_glob_src(root: Path, recursive: bool) -> list[Path]:
    """递归收集 *.md + *.txt（避开 _origin.pdf/_layout.pdf 等原始文件）。

    Path.glob("**/*.md") 内部 walk 遇到 OSError（drvfs 长路径/磁盘瞬断/
    损坏目录项）直接抛异常崩掉整批导入；os.walk(onerror=...) 跳过坏目录
    继续。非递归时单层枚举即可（不进入子目录，无此风险）。
    """
    if not recursive:
        return sorted(
            f
            for f in root.iterdir()
            if f.is_file()
            and f.suffix.lower() in (".md", ".txt")
            and not f.name.startswith(".")
        )
    hits: list[Path] = []
    bad: list[str] = []

    def _onerror(e: OSError) -> None:
        bad.append(str(e.filename or e))

    for dirpath, _dirnames, filenames in os.walk(root, onerror=_onerror):
        for name in filenames:
            if name.endswith((".md", ".txt")) and not name.startswith("."):
                hits.append(Path(dirpath) / name)
    for d in bad:
        print(f"跳过不可读目录: {d}", file=sys.stderr)
    return sorted(hits)
