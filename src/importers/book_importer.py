"""
文件名: book_importer.py
摘要: 图书/游戏剧情导入器 v2（chunk 层）。
      每本书一个事务：documents_book（书目向量 = 书名+目录）+
      documents_book_chunk（结构感知切块，≤2500 字符，8192 token 物理上限内）
      原子提交，中途失败整书回滚。
      source_path_hash 唯一约束 → 幂等导入/断点恢复（重跑同命令自动跳过已导书）；
      嵌入走 embed_batch（逐条缓存 + 分批 HTTP），重跑零网络请求。
      目录递归模式只取 *.md / *.txt / **MinerU 的 *.zip**（避开 _origin.pdf/_layout.pdf）；
      zip 输入先物化到 cache 再走同一条 md 路径（见 core.zip_input）——
      身份（幂等键 + 书名）挂在 zip 上，工作路径才是解包正文。
      MinerU 的**裸结构 JSON**（`--format middle_json` 的产出）拒绝导入并给出改法
      （官方：standalone structural JSON is not a complete result package）。
      分类 tag = 扫描根目录名（搬家按分类目录逐个调用）。
      写入前 tag 逐元素校验（哨兵 OMNIA / SQL 保留字 / 结构性字符 → core.tags）。
依赖: core.config, core.db, core.embedding, core.image_pipeline,
      core.tokenizer, core.tags, core.chunker, core.mineru_postprocess,
      core.zip_input
      PyMuPDF (可选), ebooklib (可选)
"""

from __future__ import annotations

import unicodedata
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
from ..core.text_errata import count_errata
from ..core.mineru_schema import FORM_DIR, FORM_FLAT, FORM_FULL
from ..core.tags import validate_single_tag, validate_write_tags
from ..core.tokenizer import tokenize_cjk_ub
from ..core.zip_input import (
    REQUIRED_MEMBER,
    ZipInputError,
    describe as describe_zip,
    is_mineru_zip,
    materialize as materialize_zip,
)

logger = logging.getLogger(__name__)

# ── Ctrl+C 两级语义（跨子目录共享） ─────────────────────────
# 第一次中断 = 跳过当前书，继续下一本；第二次 = 停止全部。
_INTERRUPTS: list[int] = [0]  # 中断计数（list 可变对象，函数内改元素无需 global 声明）

# ── 可导入的源文件扩展名（**唯一真相源**） ──────────────────
# 此前 (.md, .txt) 在 4 处各写一份（本文件的枚举、CLI 的计数与两处根目录枚举），
# 加 .zip 时必然漏一处；而"计数的尺"与"执行的尺"一旦走偏，就会出现
# 「总说 13、实际跑了 12」。故收拢到这里，CLI 一律 import 它。
# .zip 另有两层规则，都在 _safe_glob_src 里：
#   ① **内容判据** —— 归档里必须有 markdown.md（否则不算 MinerU 产出）；
#   ② **同主干撞车** —— <stem>.md 与 <stem>.zip 并存时 zip 优先（同一次解析的
#      两种序列化，都导会入库两次）。
SRC_EXTS = (".md", ".txt", ".zip")

# ── 内联 base64 图要不要 OCR（2026-09-29 用户裁定） ──────────
# **一律不 OCR**。这一条取代了 D1 的「parse_mode 未知 ⟹ 保守走 OCR」在**内联图**
# 这一档上的口径 —— 因为 flat 是唯一 parse_mode 未知的形态，而它恰好没有
# structured_content，拿不到图片类型映射，照片/插图/公式全会被当文字读，
# OCR 文本进正文就是噪声。
# ⚠ 没有信息被丢：图片字节仍落盘（images/{md5}）、引用仍留在正文，
#    丢的只是"自动把 OCR 文本灌进正文"这一步。想走视觉时图片还在盘上。
# ⚠ 新的 zip 形态（--format zip）不受影响：它的图片是独立文件 + 有类型映射，
#    照常走 process_image_refs 的既有判据（parse_mode='ocr' 或类型命中才跳过）。
_INLINE_IMAGE_OCR = False

# ── MinerU 裸结构 JSON = 拒绝导入 ────────────────────────
# 官方 reference/output_files 原话：PDF ParseResult.to_dict()/to_json()
# "still omit block image_base64, so standalone structural JSON is not a
#  complete result package"。`--format middle_json` 的产物就是它：
# 只有结构，没有 markdown 正文、也没有图片文件 ⟹ 做不成切片语料。
MINERU_JSON_SCHEMAS = ("docvortex.middle", "docvortex.model")
_MINERU_JSON_HINT = (
    "MinerU 的裸结构 JSON 不是完整结果包（官方 reference/output_files：\n"
    "  \"standalone structural JSON is not a complete result package\"），\n"
    "它只有结构，没有 markdown 正文、也没有图片文件，做不成切片语料。\n"
    "改法：跑 `mineru-kit parse <src> -o <out> --format zip`\n"
    "（mineru_batch.py v6.4 起 v4 的缺省已是 zip），把产出的 <stem>.zip 交给我。"
)


class MiddleJsonInputError(ValueError):
    """把 MinerU 的裸结构 JSON 当书导入 —— 明确拒绝，并指路 --format zip。"""


def describe_mineru_json(p: Path, *, max_bytes: int = 64 * 1024 * 1024) -> str | None:
    """这个 .json 是不是 MinerU 的裸结构产物？是则返回 schema 名，否则 None。

    ⚠ 只能读内容认 `schema` 键，**不能靠文件名**：
      · `--format middle_json` 写的是 `<stem>.json`，名字随书变，无固定模式；
      · 而 v3 的 `<stem>_middle.json` 是**合法侧车**（_load_v3 要读它），
        按名字拦会误伤所有 v3 产出的目录扫描。
    文件过大（>max_bytes）时返回 None：那是病态输入，不值得为它读进内存。
    """
    try:
        if p.stat().st_size > max_bytes:
            return None
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    schema = data.get("schema")
    return str(schema) if schema in MINERU_JSON_SCHEMAS else None


def refuse_explicit_mineru_json(p: Path) -> None:
    """显式点名一个 .json 时：是 MinerU 裸结构就拒绝并指路。

    只对 .json 生效；其它扩展名的显式路径照旧走原来的流程（不支持就报不支持）。
    """
    if p.suffix.lower() != ".json" or not p.is_file():
        return
    schema = describe_mineru_json(p)
    if schema:
        raise MiddleJsonInputError(
            f"{p}\n是 MinerU 的裸结构 JSON（schema={schema}）—— 不能当书导入。\n\n"
            + _MINERU_JSON_HINT
        )


def _refuse_if_only_mineru_json(root: Path, recursive: bool) -> None:
    """目录里一本可导入的都没有 —— 若躺着 MinerU 裸结构 JSON，说清为什么。

    旧行为是静默返回 0 本（"什么都没发生"），用户会以为路径写错了。
    只在**零可导入文件**时才扫 json ⟹ 对正常书库零成本。
    """
    hits: list[Path] = []
    try:
        it = root.rglob("*.json") if recursive else root.glob("*.json")
        for p in it:
            if p.is_file() and describe_mineru_json(p) is not None:
                hits.append(p)
                if len(hits) >= 5:
                    break
    except OSError:
        return
    if hits:
        raise MiddleJsonInputError(
            f"「{root}」下没有可导入的 md/txt/MinerU zip，"
            f"但发现 MinerU 的裸结构 JSON：\n"
            + "\n".join(f"  · {p}" for p in hits)
            + "\n\n" + _MINERU_JSON_HINT
        )


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
        identity_path: Path | None = None,
        work_note: str | None = None,
    ) -> dict[str, Any]:
        """导入单个图书文件（书 + 全部 chunk 单事务原子提交）。

        identity_path: 这本书在**用户磁盘上的身份**。缺省 = file_path。
            只有 zip 输入会给出不同的值：那时 file_path 是 cache 里的解包正文
            （<cache>/mineru_unzip/<md5>/markdown.md），身份是那个 .zip。
            两个字段必须分开，否则：
              · 幂等键 md5(source_path) 会存 cache 路径 —— 清一次 cache 就变成
                另一本书了；
              · 书名会取解包正文的 stem ⟹ 每本书都叫 "markdown"。
        work_note: 身份头里附一句工作路径说明（zip 物化结论）。

        返回: {"guid", "bucket", "key_metadata", "chunks", "footnotes",
               "mineru", "file", "status"}；
        已导入且未 force → status="skipped"（幂等/断点恢复）。
        """
        # 身份路径：幂等键与书名都挂在**它**上面，不挂在 cache 路径上
        ident = identity_path if identity_path is not None else file_path
        source_path = str(ident.resolve())
        # LLM 消费视角：存 Windows 风格路径（<local-path>
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
        key_meta = self._extract_book_name(ident)
        source_dir = file_path.parent   # 工作目录：zip 时是解包根（images/ 就在它下面）
        # 产物形态一次装载、多处使用（v3 dir / v4 full / v4 flat；
        # 判据全在 core.mineru_schema）。provenance 落库见第 5.5 段。
        product = MineruPostprocessor.load(file_path)
        is_mineru = product.is_mineru
        _banner(file_path, product, ident, work_note)
        # 书级文本误差统计（errata_stats）：算点见 3.5 之后 —— 判据的输入必须是
        # **将要落库的那个串**（collapse_whitespace 之后），否则导入时算的与
        # 事后从 content 回填的对不上，会长出两套口径。判据/词表见
        # core.text_errata（全项目唯一权威实现）。

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
            # 带书名：--parallel N 下多本的进度挤在同一输出区，不带归属会被读成
            # 「同一本跑了很多遍」。实测用户因此误判两次。
            # ⚠️ 这行是 \r 原地刷新（end=""）⟹ **会擦掉上一条**，历史留不住；
            #    正因如此，归属信息必须在行内，不能只靠上下文。
            who = ident.name
            if len(who) > 20:
                who = who[:9] + "…" + who[-9:]
            print(f"\r  · [{who}] {msg:<40}", file=sys.stderr, end="", flush=True)

        # 1. 提取文本
        text = self._extract_text(file_path)
        _stage("读取文本")

        # 2. MinerU 预处理：图片路径重映射（四层降级链）
        if is_mineru:
            logger.info("检测到 MinerU 输出（%s），预处理图片引用...",
                        product.form)
            text = self._mineru.prepare_images(text, source_dir)
            # v4 扁平 md：图片以 base64 内联（实测一本书 124 张/21MB、另一本
            # 185 张/45MB）。必须解码落盘 + 改写引用，否则 base64 整段进 chunk
            # 与 documents_book.content。物化后引用变成存储路径，
            # 随后的 process_image_refs 会命中已登记记录、复用 ocr_text。
            n_inline = text.count("data:image/")
            if n_inline:
                _why = ("只落盘换引用，**不 OCR**（内联图政策）"
                        if not _INLINE_IMAGE_OCR else "逐张 OCR")
                # 故意用 print 而不是原地刷新：这一行要**留在屏上**做说明，
                # 否则用户只看到进度在爬、不知道在爬什么、还要爬多久
                print(f"\n  · [{ident.name}] 内联 base64 图 {n_inline} 张 "
                      f"（parse_mode={product.provenance.parse_mode or 'v3无此字段'}）"
                      f"⟹ {_why}", file=sys.stderr, flush=True)
            n_ok, n_bad = 0, 0
            text, n_ok, n_bad = self._image_pipeline.materialize_inline_images(
                text,
                # 内联图跳过 OCR：既有 policy（见 _INLINE_IMAGE_OCR），
                # 以及 parse_mode='ocr' 这个同一结论的另一条理由（留着以防
                # 政策被改回 True）。物化写进 images 表的 ocr_text 是空串，
                # 随后 process_image_refs 命中该行 ⟹ 不再 OCR、也不插 OCR 文本。
                skip_ocr=(not _INLINE_IMAGE_OCR) or product.skip_ocr_for_images,
                progress=lambda done, total: _stage("内联图(落盘)", done, total),
                stop_check=stop_check,
            )
            if n_ok or n_bad:
                logger.info("内联 base64 图物化: 成功 %d / 失败 %d", n_ok, n_bad)

        # 3. 图片管线（OCR + MD5 + 硬链接）
        # MinerU layout 判据：图片 block 类型映射（照片/公式类跳过 OCR）。
        # 优先用适配器给的映射（v3 content_list_v2 / v4 structured_content 同一口径）；
        # 适配器给不出时回退旧的 v3 专用读取（保持对既有产出的兼容）。
        mineru_type_map = product.image_type_map or self._load_mineru_image_types(
            source_dir, ident.stem
        )
        text = self._image_pipeline.process_image_refs(
            text,
            source_dir,
            progress=lambda done, total: _stage("图片", done, total),
            image_type_map=mineru_type_map,
            stop_check=stop_check,
            skip_ocr_all=product.skip_ocr_for_images,
        )

        text = collapse_whitespace(text)  # 3.5 文本清洗：空白折叠（变长，PUA 不动）；随后切块（护栏在 chunker 内）
        # 3.6 书级误差统计：**必须在这之后算**（text 此刻就是将来落库的 content）。
        # 机械那部分（形近串写 / 行尾断词 / 成串汉字 / 康熙部首 / PUA）由判据算；
        # 「每本书不一样」的外部注记走 mhi annotate 事后富化，合并进同一列。
        errata = count_errata(text)
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
                status, expected_chunks,
                mineru_version, mineru_tier, mineru_parse_mode, mineru_form,
                errata_stats)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'processing', ?, ?, ?, ?, ?, ?)""",
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
                product.provenance.version,
                product.provenance.tier,
                product.provenance.parse_mode,
                product.provenance.form,
                json.dumps(errata, ensure_ascii=False, sort_keys=True),
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
                tokenize_cjk_ub(c.content),
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
        index_offset: int = 0,
        grand_total: int = 0,
    ) -> list[dict[str, Any]]:
        """递归导入目录下全部可导入文件（.md/.txt/MinerU zip；书库搬家入口）。

        index_offset / grand_total: 书级计数器 `[X/N]` 的**全局**坐标。
        用户裁定：N 要是「本次命令行一共处理的 md/txt 数」，不是每次
        import_directory 调用各自的数量 —— 子目录分派模式下每组只有 1 本，
        那个分母恒为 1、零信息量（实测让用户误判过一次）。
        grand_total=0 表示调用方没给（单目录直接调用）⟹ 退回本次数量。

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
            # 零可导入文件：若只是因为躺着 MinerU 裸结构 JSON，要说清为什么，
            # 不要静默返回 0 本（旧行为让用户以为路径写错了）
            _refuse_if_only_mineru_json(dir_path, recursive)
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

            def _one(cand: Path) -> dict[str, Any]:
                return self.import_candidate(
                    cand, bucket, manual_tags, category_tag, force,
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
        for idx, cand in enumerate(md_files, 1):
            # 书级进度（stderr：MCP stdio 下 stdout 是协议通道）——独立一行
            print(
                f"[{index_offset + idx}/{grand_total or len(md_files)}] "
                f"{cand.name}",
                file=sys.stderr,
                flush=True,
            )
            try:
                result = self.import_candidate(
                    cand, bucket, manual_tags, category_tag, force
                )
                results.append(result)
                logger.info(
                    "[%d/%d] %s: %s",
                    idx,
                    len(md_files),
                    result.get("status"),
                    result.get("key_metadata", cand.stem),
                )
            except KeyboardInterrupt:
                _INTERRUPTS[0] += 1
                if _INTERRUPTS[0] >= 2:
                    logger.warning("用户再次中断：停止全部导入（已导书保留）")
                    raise
                logger.warning(
                    "用户中断: %s（该书未入库；再按一次 Ctrl+C 停止全部）",
                    cand,
                )
                results.append({"file": str(cand), "status": "interrupted"})
                continue
            except MiddleJsonInputError:
                raise                      # 裸结构 JSON 是"这类输入不支持"，不是"这本坏了"
            except Exception as exc:
                logger.exception("导入失败: %s", cand)
                results.append(
                    {"file": str(cand), "status": "error", "error": str(exc)}
                )
        return results

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def import_candidate(
        self,
        cand: Path,
        bucket: str | None,
        manual_tags: str,
        category_tag: str,
        force: bool,
        *,
        verbose: bool = True,
        progress_tracker: ProgressTracker | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """把一个**候选路径**导入成一本书：md/txt 直通；zip 先物化再按 md 导入。

        zip 的处置只有一句话：**身份挂在 zip 上，工作路径才是解包正文**。
        解包目录是内容寻址的（<cache>/mineru_unzip/<zip 内容 md5>/），
        所以同一份产物换位置/改名都命中同一次解包；重新解析过的新产物只有
        一字之差也算不出同一个 md5，不会被旧内容串味。
        """
        if cand.suffix.lower() != ".zip":
            return self.import_file(
                cand, bucket, manual_tags, category_tag, force,
                verbose=verbose, progress_tracker=progress_tracker,
                stop_check=stop_check,
            )
        m = materialize_zip(cand, stop_check=stop_check)
        return self.import_file(
            m.md_path, bucket, manual_tags, category_tag, force,
            verbose=verbose, progress_tracker=progress_tracker,
            stop_check=stop_check,
            identity_path=cand,
            work_note=describe_zip(m),
        )

    @staticmethod
    def _extract_book_name(file_path: Path) -> str:
        """书名 = 文件主干，**做 NFC 归一**（D2 裁定）。

        幂等键是 md5(source_path)，而 macOS（HFS+）会强制 NFD：从 mac 拷来的
        同名书会算出不同 md5 ⟹ 同一本书导入两次。NFC 是除 Apple 以外的事实
        工业标准，故在此处归一。现状 686 本书 0 例，属预防。
        """
        return unicodedata.normalize("NFC", file_path.stem)

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
    p = Path(path_str)
    # 显式点名一个裸结构 JSON：当场拒绝并指路，别让它走到后面才以
    # "不支持的格式: .json" 这种毫无信息量的方式失败
    refuse_explicit_mineru_json(p)
    if p.suffix.lower() == ".zip":
        return BookImporter().import_candidate(
            p, bucket, tags, category_tag, force
        )
    return BookImporter().import_file(p, bucket, tags, category_tag, force)


def import_book_directory(
    path_str: str,
    bucket: str | None = None,
    tags: str = "",
    recursive: bool = True,
    force: bool = False,
    parallel: int = 1,
    index_offset: int = 0,
    grand_total: int = 0,
) -> list[dict[str, Any]]:
    if parallel > 2:
        logger.warning(
            "parallel=%d > 2：documents_book_chunk 两个 FULLTEXT 索引并发插入互斥，"
            "锁竞争极大（Lock wait timeout 风险），建议降到 2 或 1",
            parallel,
        )
    return BookImporter().import_directory(
        Path(path_str), bucket, tags, recursive, force,
        index_offset=index_offset, grand_total=grand_total,
    )


def _version_label(prov, form: str, fp: Path) -> str:
    """MinerU 版本展示串：**精确版本优先；缺元数据时用形态反推**，不含糊其辞。

    依据（三种信号都是充分的，用户裁定 + 机制上成立）：
      · **内联 base64 图（flat）⟹ 一定是 v4** —— v3 从不内联，它把图片放成
        `images/` 独立文件、md 里只写 `![](images/xxx)`；
      · **zip 打包 / 固定成员名（full）⟹ v4** —— 那是 v4 专有的序列化形态；
      · **同级目录名 `hybrid_auto` / `auto`（dir）⟹ v3** —— v3 的 method 目录。

    ⚠️ 反推只用于**展示**：`documents_book.mineru_version` 仍存原始值
    （flat 无元数据就存 `''`），**不把推断写进库** —— 列是事实，展示是解释。
    """
    if prov.version:
        return f"MinerU {prov.version}"
    if form in (FORM_FLAT, FORM_FULL):
        why = "扁平 md 内联 base64 图" if form == FORM_FLAT else "zip 打包"
        return f"MinerU v4（据形态反推：{why}为 v4 专有；精确补丁版本无元数据）"
    if form == FORM_DIR:
        d = fp.parent.name
        return (f"MinerU v3（同级 `{d}` 目录 + 侧车存在；版本字段缺失）"
                if d in ("hybrid_auto", "auto")
                else "MinerU v3（侧车存在；版本字段缺失）")
    return "MinerU（形态未识别）"


def _banner(
    fp: Path,
    prod,
    identity: Path | None = None,
    work_note: str | None = None,
) -> None:
    """每本一行「身份头」：**完整路径 + MinerU 溯源**（+ zip 的工作路径）。

    用户裁定撤掉并行、老实一本一本导，就是为了这个：串行之后每个阶段行都属于
    最近这一条头，归属不再有歧义（此前多本进度挤在同一输出区、又被原地刷新
    擦掉历史，同一个缺陷被误判两次）。

    identity/work_note：zip 输入时**身份**是那个 .zip，而工作路径是 cache 里的
    解包正文 —— 两个都打。只打 cache 路径等于没告诉用户这是哪本书。

    ⚠️ 必须是**模块级**函数：调用点（装载 product 之后）在 import_file 的很靠前
    位置，早于方法内各嵌套 helper 的定义 ⟹ 写成嵌套 def 会 UnboundLocalError。
    """
    ident = identity if identity is not None else fp
    prov = prod.provenance
    if prod.is_mineru:
        bits = [_version_label(prov, prod.form, ident), prod.form]
        if prov.tier:
            bits.append(f"tier={prov.tier}")
        bits.append(f"parse_mode={prov.parse_mode or '(v3无此字段)'}")
        src = " / ".join(bits)
    else:
        src = "非 MinerU 产出（按普通 Markdown 处理）"
    print(f"\n  ── 路径: {ident}", file=sys.stderr, flush=True)
    print(f"     来源: {src}", file=sys.stderr, flush=True)
    if ident != fp:
        print(f"     工作: {fp}", file=sys.stderr, flush=True)
    if work_note:
        print(f"     {work_note}", file=sys.stderr, flush=True)


def _safe_glob_src(root: Path, recursive: bool) -> list[Path]:
    """递归收集可导入的源文件：*.md + *.txt + **MinerU 的 *.zip**。

    Path.glob("**/*.md") 内部 walk 遇到 OSError（drvfs 长路径/磁盘瞬断/
    损坏目录项）直接抛异常崩掉整批导入；os.walk(onerror=...) 跳过坏目录
    继续。非递归时单层枚举即可（不进入子目录，无此风险）。

    ⚠️ zip 只在**内容确认**是 MinerU 产出（归档里有 markdown.md）时才收。
    判据放在枚举里而不是导入时，是为了让"计数的尺"与"执行的尺"保持同一把：
    目录里躺着无关压缩包时，把它算进总数 N 却永远导不进来，就会出现
    「总说 13、实际跑了 12」。被拒的 zip 逐条报到 stderr（与本函数已有的
    「跳过不可读目录」同一风格，不是静默丢弃）。
    """
    if not recursive:
        cands = sorted(
            f
            for f in root.iterdir()
            if f.is_file()
            and f.suffix.lower() in SRC_EXTS
            and not f.name.startswith(".")
        )
    else:
        hits: list[Path] = []
        bad: list[str] = []

        def _onerror(e: OSError) -> None:
            bad.append(str(e.filename or e))

        for dirpath, _dirnames, filenames in os.walk(root, onerror=_onerror):
            for name in filenames:
                if name.startswith("."):
                    continue
                if Path(name).suffix.lower() in SRC_EXTS:
                    hits.append(Path(dirpath) / name)
        for d in bad:
            print(f"跳过不可读目录: {d}", file=sys.stderr)
        cands = sorted(hits)

    accepted: list[Path] = []
    for p in cands:
        if p.suffix.lower() != ".zip":
            accepted.append(p)
        elif is_mineru_zip(p):
            accepted.append(p)
        else:
            print(
                f"跳过非 MinerU zip（归档里没有 {REQUIRED_MEMBER}）: {p}",
                file=sys.stderr,
            )

    # ── 同主干撞车：<stem>.md 与 <stem>.zip 并存 ⟹ **zip 优先**（用户裁定） ──
    # 理由：两者是**同一次解析的两种序列化**，不是两本书。zip 是超集（带侧车
    # 元数据与独立图片文件），flat 的唯一优势只是文件小。若两个都导，会因为
    # 幂等键 md5(source_path) 不同而入库两次、内容几乎重复（真样本实测
    # 67 块 + 66 块，正文逐字相同），且 flat 那份还会被 zip 的侧车"认领"成
    # form='full'，溯源标签也跟着不准。
    # 只对 .md 生效：.txt 不是 MinerU 的产出形态，不存在"同一次解析"这回事。
    zip_stems = {(p.parent, p.stem) for p in accepted if p.suffix.lower() == ".zip"}
    out: list[Path] = []
    for p in accepted:
        if p.suffix.lower() == ".md" and (p.parent, p.stem) in zip_stems:
            print(
                f"跳过 {p.name}（同目录的 {p.stem}.zip 是超集：带侧车元数据与独立"
                f"图片；两者是同一次解析的两种序列化，都导会重复）",
                file=sys.stderr,
            )
            continue
        out.append(p)
    return out
