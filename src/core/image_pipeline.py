"""
文件名: image_pipeline.py
摘要: 图片发现 → MD5 去重 → Everything 兜底 → OCR → 硬链接。
      从 Markdown 提取图片引用，计算 MD5，查 images 表复用或 OCR，
      硬链接到 images/{md5}（无扩展名），最后将 OCR 文本插回原文。
依赖: httpx
      Pillow (PIL), hashlib, re, shutil
      core.config, core.db
      core.http_client (local_client —— trust_env=False，免疫 NO_PROXY 脏值)
"""

from __future__ import annotations

import hashlib
import logging
import io
import os
import re
import shutil
import threading
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

from .config import get_config
from .db import get_db
from .http_client import local_client

# Markdown 图片引用正则
# 匹配: ![alt](path) 或 ![alt](path "title")
_RE_MD_IMAGE = re.compile(r"!\[.*?\]\(([^\s\)]+)(?:\s+\"[^\"]*\")?\)")
# HTML <img src="path">
_RE_HTML_IMG = re.compile(r'<img[^>]+src\s*=\s*["\']([^"\']+)["\'][^>]*>', re.IGNORECASE)

_OCR_OK_EXTS = {".jpg", ".jpeg", ".png"}  # 可靠白名单；其他格式（GIF/BMP/WEBP/TIF）OCR前一律转PNG
class ImagePipelineError(RuntimeError):
    """图片管线处理失败。"""


class ImagePipeline:
    """图片管线：去重 + OCR + 硬链接 + 引用计数。"""

    def __init__(self) -> None:
        cfg = get_config()
        self.storage_dir: Path = self._resolve_storage_dir(cfg)
        self.ocr_endpoint: str | None = cfg.get("ocr.endpoint")
        self.everything_url: str = cfg.get("everything.http_url", "http://localhost:9000/")
        self.ocr_enabled: bool = cfg.get("images.ocr_enabled", True)
        # OCR 语言降级链（逗号分割；server_paddle 按链识别，首个达标语言胜出）
        self.lang_chain: str = cfg.get("ocr.lang_chain", "ch,en,la,el,ru")
        # MinerU layout 判据：这些类型的图跳过 OCR（但仍 MD5 去重/登记）
        skip_raw = cfg.get("ocr.skip_image_types", "image,equation_interline")
        self.skip_types: set[str] = {
            s.strip() for s in skip_raw.split(",") if s.strip()
        }
        self._http = local_client(15.0)
        # 熔断/探测缓存：服务不可达时整体快速降级，不逐张反复失败
        self._everything_dead: bool = False
        self._ocr_probed: bool = False
        self._ocr_ok: bool = False
        self._ocr_lock = threading.Lock()  # 首次探测加锁（并行导入多 worker 并发）
        self._warned_paths: set[str] = set()  # 图片跳过告警去重（防止几百条刷屏）

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def process_image_refs(
        self,
        md_text: str,
        source_dir: Path | None = None,
        progress: Callable[[int, int], None] | None = None,
        image_type_map: dict[str, str] | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> str:
        """从 Markdown 文本中处理所有图片引用。

        遍历图片引用 → 查找文件 → MD5 → 去重/reuse → OCR → 硬链接。
        在每张图片引用下方插入 OCR 文本（如能 OCR 到内容）。

        参数:
            md_text: 原始 Markdown 文本。
            source_dir: Markdown 文件所在目录（用于解析相对路径）。
            progress: 进度回调 (done, total)。
            image_type_map: MinerU layout 类型映射 {图片路径: block类型}。
                路径命中且类型 ∈ skip_types（config [ocr].skip_image_types）
                时跳过 OCR（照片/插图/公式——layout 分类已证明无文字价值），
                但 MD5 去重/硬链接/登记照做。非 MinerU 书传 None（全部 OCR）。
            stop_check: 可选中断检查回调——逐张图前调用，返回 True 则
                raise KeyboardInterrupt（并行导入第一次 Ctrl+C 时快速中止
                当前书，不等剩余图片 OCR 完成）。

        返回:
            OCR 文本已插入的更新版 Markdown。
        """
        refs = self._extract_refs(md_text)
        if not refs:
            return md_text

        # OCR 服务不可达（或显式关闭）→ 整体跳过图片管线（显式降级：
        # 图片引用保留在正文，OCR 是可选增强，不阻塞导入主流程）
        if not self._ocr_available():
            logger.warning(
                "OCR 服务不可达（%s），跳过图片 OCR 阶段（图片引用保留）",
                self.ocr_endpoint,
            )
            return md_text

        # 去重：同一个路径只处理一次
        seen: dict[str, str] = {}
        processed = 0
        missing = 0
        for raw_path in refs:
            if stop_check is not None and stop_check():
                # 并行导入第一次 Ctrl+C：当前书快速中止（图片阶段逐张检查）
                raise KeyboardInterrupt
            if raw_path in seen:
                continue
            processed += 1
            # MinerU layout 判据：命中且类型 ∈ skip_types → 跳过 OCR（登记照做）
            img_type = image_type_map.get(raw_path) if image_type_map else None
            skip_ocr = bool(img_type and img_type in self.skip_types)
            try:
                ocr_text = self._process_single_ref(
                    raw_path, source_dir, skip_ocr=skip_ocr
                )
                seen[raw_path] = ocr_text
            except Exception as exc:
                # 单张图片处理失败不阻断整本书导入：
                # 图片是增强项，失败则保留原引用继续（显式降级，logger 可查）
                missing += 1
                seen[raw_path] = ""
            if progress:
                progress(processed, len(refs))

        if missing:
            # 未找到的图片引用是常态（MinerU 引用但文件缺失）——
            # 不逐张刷日志，只汇总一条（避免几百条 warning 淹没进度）
            logger.warning("图片处理跳过 %d 张（未找到/不可用，引用保留）", missing)

        # 将 OCR 文本插回 Markdown
        lines = md_text.split("\n")
        new_lines: list[str] = []
        for line in lines:
            new_lines.append(line)
            for raw_path in refs:
                if raw_path in line and seen.get(raw_path):
                    # 在图片行之后插入 OCR 文本
                    new_lines.append(f"\n> 📝 OCR: {seen[raw_path]}\n")
                    break

        return "\n".join(new_lines)

    # ------------------------------------------------------------------
    # 内部处理
    # ------------------------------------------------------------------

    def _process_single_ref(
        self, raw_path: str, source_dir: Path | None, skip_ocr: bool = False
    ) -> str:
        """处理单个图片引用：查找 → MD5 → 去重 → OCR → 硬链接。

        skip_ocr=True 时跳过 OCR（MinerU layout 判据：照片/插图/公式），
        但 MD5 去重/硬链接/登记照做（ocr_text 留空）。

        返回 OCR 文本（空字符串表示无 OCR 内容）。
        """
        # 1. 查找文件
        img_path = self._find_image(raw_path, source_dir)
        if img_path is None:
            raise ImagePipelineError(f"图片未找到: {raw_path}")

        # 2. MD5
        md5_hash = self._md5_file(img_path)

        # 3. 查 images 表去重
        db = get_db()
        rows = db.execute_sql(
            "SELECT ocr_text, file_path FROM images WHERE md5_hash = ?",
            (md5_hash,),
        )

        if rows:
            # 命中：ref_count++，复用 OCR 文本
            db.execute_sql(
                "UPDATE images SET ref_count = ref_count + 1 WHERE md5_hash = ?",
                (md5_hash,),
            )
            return rows[0].get("ocr_text") or ""

        # 4. 未命中：硬链接
        dest_path = self.storage_dir / md5_hash
        self._hardlink_or_copy(img_path, dest_path)

        # 5. OCR（skip_ocr：MinerU 判据跳过——照片/插图/公式无文字价值）
        ocr_text = ""
        if not skip_ocr and self.ocr_enabled and self.ocr_endpoint:
            try:
                ocr_text = self._ocr_image(img_path) or ""
            except Exception as exc:
                logger.warning("OCR 失败 [%s]: %s", raw_path, exc)
                ocr_text = ""

        # 6. INSERT images 表
        db.execute_sql(
            "INSERT INTO images (md5_hash, file_path, ocr_text) VALUES (?, ?, ?)",
            (md5_hash, str(dest_path), ocr_text),
        )

        return ocr_text

    # ------------------------------------------------------------------
    # 图片查找
    # ------------------------------------------------------------------F

    def _find_image(
        self, raw_path: str, source_dir: Path | None
    ) -> Path | None:
        """按优先级查找图片文件。

        0. Windows 绝对路径（E:\\...）→ 转 WSL 路径（/mnt/<drive>/...）
           （md 可能由非 MinerU 工具生成，图片引用为 Windows 路径）
        1. 直接路径（相对 source_dir 或绝对路径）
        2. Everything HTTP API 兜底
        """
        # 0. Windows 绝对路径 → WSL 路径（导入跑在 WSL）
        if re.match(r"^[A-Za-z]:[\\/]", raw_path):
            wsl_path = (
                "/mnt/"
                + raw_path[0].lower()
                + "/"
                + raw_path[2:].replace("\\", "/")
            )
            p = Path(wsl_path)
            if p.is_file():
                return p.resolve()

        # 尝试直接访问
        p = Path(raw_path)
        if p.is_absolute():
            if p.is_file():
                return p
        elif source_dir is not None:
            candidate = source_dir / p
            if candidate.is_file():
                return candidate.resolve()

        if (hit := _find_in_media_dirs(raw_path, source_dir)) is not None: return hit  # pandoc/z-lib: MD同目录 media/images 兜底（Everything 之前）
        filename = Path(raw_path).name
        results = self._search_everything(filename)
        if results:
            # 双保险：个别路径返回 dict 结构时兼容提取
            if isinstance(results, dict):
                results = results.get("hits") or []
            if results:
                # 取第一个结果
                candidate = Path(results[0].get("path", ""))
                if candidate.is_file():
                    return candidate

        return None

    def _search_everything(self, filename: str) -> list[dict[str, Any]]:
        """通过 Everything HTTP API 搜索文件。失败一次即熔断（本批禁用）。"""
        if self._everything_dead:
            return []
        url = (
            f"{self.everything_url}"
            f"?search={urllib.parse.quote(filename)}"
            f"&json=1&path_column=1&count=20"
        )
        try:
            resp = self._http.get(url, timeout=10.0)
            resp.raise_for_status()
            # Everything API 返回 {"hits": [...]}；提取列表并防御异常结构
            data = resp.json()
            if isinstance(data, dict):
                hits = data.get("hits") or []
            elif isinstance(data, list):
                hits = data
            else:
                hits = []
            return hits
        except (httpx.HTTPError, ValueError) as exc:
            self._everything_dead = True
            logger.warning("Everything 不可达，本批禁用兜底搜索: %s", exc)
            return []

    def _ocr_available(self) -> bool:
        """OCR 服务可用性探测（每进程一次，缓存结果）。

        GET 探测：连接成功（含 404/405 等）视为服务存活；
        连接拒绝/超时 → 整体降级跳过图片管线。
        """
        if not (self.ocr_enabled and self.ocr_endpoint):
            return False
        with self._ocr_lock:
            if self._ocr_probed:
                return self._ocr_ok
            self._ocr_probed = True
            try:
                resp = self._http.get(
                    self.ocr_endpoint.rsplit("/", 1)[0] + "/health", timeout=2.0
                )
                self._ocr_ok = resp.status_code < 500
            except httpx.HTTPError:
                self._ocr_ok = False
            return self._ocr_ok

    # ------------------------------------------------------------------
    # 文件操作
    # ------------------------------------------------------------------

    @staticmethod
    def _md5_file(path: Path) -> str:
        """计算文件 MD5 哈希。"""
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _hardlink_or_copy(src: Path, dst: Path) -> None:
        """硬链接优先，跨设备则 fallback 到复制。目标已存在则跳过（幂等）。"""
        if dst.exists():
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(str(src), str(dst))
        except OSError as exc:
            if exc.errno == 18:  # EXDEV: 跨设备链接
                shutil.copy2(str(src), str(dst))
            elif exc.errno == 17:  # EEXIST: 目标已存在（幂等）
                pass
            else:
                raise

    def _ocr_image(self, path: Path) -> str | None:
        """调用 OCR 端点识别图片文字。"""
        if not self.ocr_endpoint:
            return None

        img_data, send_name = _prepare_ocr_image(path)  # 非支持格式（GIF等）先转PNG

        try:
            resp = self._http.post(
                self.ocr_endpoint,
                files={"image": (send_name, img_data)},
                data={"langs": self.lang_chain},
                # 120s：server_paddle 串行队列，排队等待也算在超时里
                timeout=120.0,
            )
            if resp.status_code == 503:
                # server 忙（串行队列排满）：显式降级跳过该图，不刷屏
                logger.warning(
                    "OCR 服务忙（503），跳过该图 [%s]", path.name
                )
                return None
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ImagePipelineError(f"OCR 请求失败: {exc}") from exc

        # 预期返回格式: {"text": "...", "confidence": 0.95}
        # 或 {"results": [{"text": "...", "confidence": ...}, ...]}
        if "text" in body:
            return body["text"]
        if "results" in body and isinstance(body["results"], list):
            texts = [r.get("text", "") for r in body["results"] if r.get("text")]
            return "\n".join(texts) if texts else None
        return None

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_refs(md_text: str) -> list[str]:
        """提取 Markdown 中的所有图片引用路径（去重保留序）。"""
        seen: set[str] = set()
        refs: list[str] = []
        for match in _RE_MD_IMAGE.finditer(md_text):
            path = match.group(1)
            if path not in seen:
                seen.add(path)
                refs.append(path)
        for match in _RE_HTML_IMG.finditer(md_text):
            path = match.group(1)
            if path not in seen:
                seen.add(path)
                refs.append(path)
        return refs

    @staticmethod
    def _resolve_storage_dir(cfg: Any) -> Path:
        """解析图片存储目录（相对或绝对）。"""
        dir_name: str = cfg.get("images.storage_dir", "images")
        p = Path(dir_name)
        if not p.is_absolute():
            p = cfg.config_path.parent / dir_name
        p.mkdir(parents=True, exist_ok=True)
        return p.resolve()


# ── 全局单例 ──────────────────────────────────────────────

_pipeline: ImagePipeline | None = None


def get_image_pipeline() -> ImagePipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = ImagePipeline()
    return _pipeline


def _find_in_media_dirs(raw_path: str, source_dir: Path | None) -> Path | None:
    """pandoc/z-lib 兜底：引用路径失效时按文件名在 MD 同目录查找。

    z-lib 的 pandoc 转换产物图片引用常指向打包机的绝对 Windows 路径
    （目录名可能已变/不存在），但图片文件实际就在 MD 同目录的
    media/、images/、media/images/ 下。反斜杠在 POSIX 下不是路径
    分隔符，Windows 路径需手动切 basename。
    """
    if source_dir is None:
        return None
    name = raw_path.replace("\\", "/").rsplit("/", 1)[-1]
    if not name:
        return None
    for sub in ("media", "images", "media/images", ""):
        candidate = source_dir / sub / name if sub else source_dir / name
        if candidate.is_file():
            return candidate.resolve()
    return None


# ------------------------------------------------------------------
# OCR 图片准备：非 PaddleOCR 支持格式（GIF 等）→ PIL 转 PNG
# ------------------------------------------------------------------

def _prepare_ocr_image(path: Path) -> tuple[bytes, str]:
    """把图片读成可送 OCR 的字节。

    - 扩展名在白名单（PaddleOCR/opencv 可解）→ 原样读取
    - 其余（GIF 等）→ PIL 转 PNG（内存转换，不落盘、不污染硬链接存储）
    - 转换失败 → 警告 + 原样发送（让 server 侧报错，显式不静默吞）
    返回 (字节, 发送文件名)。
    """
    ext = path.suffix.lower()
    if ext in _OCR_OK_EXTS:
        return path.read_bytes(), path.name
    try:
        from PIL import Image

        with Image.open(path) as im:
            if im.mode not in ("RGB", "L", "RGBA"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "PNG")
        logger.info("图片转 PNG 后送 OCR [%s]（%s → png）", path.name, ext or "未知格式")
        return buf.getvalue(), f"{path.stem}.png"
    except Exception as exc:
        logger.warning("图片转 PNG 失败，原样发送 OCR [%s]: %s", path.name, exc)
        return path.read_bytes(), path.name
