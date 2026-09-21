#!/usr/bin/env python3
"""
文件名: server_paddle.py
摘要: PaddleOCR HTTP 服务（知识库 OCR 端点，默认端口 9008）。
      协议对齐 src/core/image_pipeline.py::_ocr_image:
        POST /ocr  multipart/form-data, files={"image": (filename, bytes)},
                  data={"langs": "ch,en,la,el,ru"}  ← 语言降级链（可选，缺省用启动 --lang）
        响应 JSON: {"text": str, "confidence": float, "lang": str,
                    "results": [{"text","confidence"}, ...]}
      健康检查惯例: GET /health → 200 {"status":"ok", ...}
用法:
  python server_paddle.py --port 9008 --device cpu --lang ch --cpu-threads 7
  --pipe 模式: 从 stdin 读图片二进制，输出 JSON（供管道调用）
模型: paddlex3.7 model_name 自动下载（~/.paddlex）；多语言懒加载（首次降级时 ~2s）
      det/cls 固定 mobile 小模型（多语种共用）；rec 按 lang：
        ch → PP-OCRv4_mobile_rec       中/英/数字
        en → en_PP-OCRv5_mobile_rec    英文定向优化
        la → latin_PP-OCRv5_mobile_rec 拉丁系（法/德/西/意/葡…48语言）
        el → el_PP-OCRv5_mobile_rec    希腊语
        ru → cyrillic_PP-OCRv5_mobile_rec  西里尔（俄语等）
CPU 线程: --cpu-threads N（默认 os.cpu_count()-1，至少1）
      paddle 官方 API: paddle.set_flags({"FLAGS_paddle_num_threads": N})
      + 环境变量 OMP_NUM_THREADS / FLAGS_paddle_num_threads（须在 import paddle 前设）
语言降级链: 按请求 langs 顺序识别，第一个 text 非空且平均置信度 ≥ 阈值（默认0.6，
      环境变量 PADDLE_LANG_CONFIDENCE 可调）的语言胜出；全链失败返回首选语言结果。
依赖: paddleocr 3.7 / paddlepaddle-gpu 3.3（venv-paddleocr），零 web 框架依赖
注意: enable_mkldnn=False —— paddle 3.3.x oneDNN PIR 回归 bug（官方 issue #77340，
      CPU 推理必炸 ConvertPirAttribute2RuntimeAttribute），关 oneDNN 是官方实测解法。
"""

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("server_paddle")

# ----------------------------------------------------------------------
# PaddleOCR 加载（按语言懒加载；模型按名自动下载到 ~/.paddlex）
# ----------------------------------------------------------------------

LANGS = {
    "ch": "PP-OCRv4_mobile_rec",              # 中/英/数字
    "en": "en_PP-OCRv5_mobile_rec",           # 英文定向优化
    "la": "latin_PP-OCRv5_mobile_rec",        # 拉丁系（法/德/西/意/葡…48语言）
    "el": "el_PP-OCRv5_mobile_rec",           # 希腊语
    "ru": "cyrillic_PP-OCRv5_mobile_rec",     # 西里尔（俄语等）
}
DET_MODEL_NAME = "PP-OCRv4_mobile_det"       # 通用检测（mobile，CPU 友好，多语种共用）
CLS_MODEL_NAME = "ch_ppocr_mobile_v2.0_cls"  # 方向分类器（mobile）


def _img_suffix(data: bytes) -> str:
    """按 magic bytes 判断图片格式（paddlex 按文件后缀路由输入类型）。"""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return ".png"  # 兜底


class OcrEngine:
    """PaddleOCR 多语言引擎。识别统一走 predict()（3.7 的 ocr() 已弃用且返回新格式）。"""

    def __init__(
        self,
        lang: str,
        device: str,
        cpu_threads: int | None = None,
        lang_confidence: float = 0.6,
    ) -> None:
        self.lang = lang
        self.device = device
        self.cpu_threads = cpu_threads or max(1, (os.cpu_count() or 2) - 1)
        # CPU 推理串行锁：多请求并发会争抢 CPU → 单张超时（timed out）
        self._infer_lock = threading.Lock()
        self.lang_confidence = lang_confidence
        self._ocrs: dict[str, object] = {}

        # CPU 线程数：环境变量必须在 paddle import 前设置（线程池在初始化时读取）
        os.environ["OMP_NUM_THREADS"] = str(self.cpu_threads)
        os.environ["FLAGS_paddle_num_threads"] = str(self.cpu_threads)
        import paddle  # noqa: PLC0415

        paddle.set_flags({"FLAGS_paddle_num_threads": self.cpu_threads})
        self._get_ocr(self.lang)  # 预加载首选语言
        logger.info(
            "PaddleOCR 就绪: lang=%s device=%s cpu_threads=%d 降级阈值=%.2f",
            self.lang, self.device, self.cpu_threads, self.lang_confidence,
        )

    def _get_ocr(self, lang: str):
        """按语言懒加载（首次降级到某语言时 ~2s 加载并缓存）。"""
        if lang not in self._ocrs:
            from paddleocr import PaddleOCR  # noqa: PLC0415

            self._ocrs[lang] = PaddleOCR(
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                enable_mkldnn=False,  # paddle3.3 oneDNN PIR 回归 bug，见 docstring
                lang=lang,
                text_detection_model_name=DET_MODEL_NAME,
                textline_orientation_model_name=CLS_MODEL_NAME,
                text_recognition_model_name=LANGS[lang],
                device=self.device,
            )
            logger.info("模型加载: lang=%s rec=%s", lang, LANGS[lang])
        return self._ocrs[lang]

    def _recognize_one(self, ocr, img_bytes: bytes) -> list[dict]:
        with tempfile.NamedTemporaryFile(suffix=_img_suffix(img_bytes), delete=False) as f:
            f.write(img_bytes)
            tmp = f.name
        try:
            return self._parse_predict(ocr.predict(tmp))
        finally:
            os.unlink(tmp)

    def recognize(
        self, img_bytes: bytes, langs: list[str] | None = None
    ) -> dict:
        """按语言降级链识别。

        链 = 请求 langs（缺省 = 启动 --lang）。第一个 text 非空且平均置信度
        ≥ 阈值 的语言胜出；全链失败返回首选语言的原始结果（可能为空文本，
        由客户端决定是否接受）。响应带 lang 字段（实际命中的语言）。
        """
        chain = [l for l in (langs or [self.lang]) if l in LANGS] or [self.lang]
        first: dict | None = None
        # 串行推理（CPU 模式）：一次一个请求，按到达顺序排队
        # 队列排满（120s 未轮到）→ busy 快速失败（客户端降级跳过，下轮续跑）
        if not self._infer_lock.acquire(timeout=120):
            return {
                "text": "", "confidence": 0.0, "lang": chain[0], "busy": True,
            }
        try:
            return self._recognize_chain(chain, img_bytes, first)
        finally:
            self._infer_lock.release()

    def _recognize_chain(self, chain, img_bytes, first):
        for lang in chain:
            results = self._recognize_one(self._get_ocr(lang), img_bytes)
            texts = [r["text"] for r in results]
            confs = [r["confidence"] for r in results]
            avg_conf = sum(confs) / len(confs) if confs else 0.0
            text = "\n".join(texts)
            payload = {
                "text": text,
                "confidence": round(avg_conf, 4),
                "results": results,
                "lang": lang,
            }
            if first is None:
                first = payload
            if text.strip() and avg_conf >= self.lang_confidence:
                return payload
        return first or {
            "text": "", "confidence": 0.0, "results": [], "lang": chain[0],
        }

    @staticmethod
    def _parse_predict(res) -> list[dict]:
        """predict() 返回 Result 列表，json 结构内递归找 rec_texts/rec_scores。"""
        out = []

        def walk(obj):
            if isinstance(obj, dict):
                texts = obj.get("rec_texts") or obj.get("texts")
                scores = obj.get("rec_scores") or obj.get("scores") or []
                if isinstance(texts, list):
                    for i, t in enumerate(texts):
                        conf = scores[i] if i < len(scores) else 0.0
                        out.append({"text": str(t), "confidence": float(conf)})
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)

        for r in res or []:
            try:
                walk(r.json)
            except Exception:
                walk(r)
        return out


# ----------------------------------------------------------------------
# HTTP 处理
# ----------------------------------------------------------------------

def _parse_multipart(body: bytes, content_type: str) -> dict[str, bytes]:
    m = re.search(r'boundary="?([^\";]+)"?', content_type or "")
    if not m:
        raise ValueError("缺少 multipart boundary")
    boundary = m.group(1).encode()
    fields: dict[str, bytes] = {}
    for part in body.split(b"--" + boundary):
        if not part or part in (b"\r\n", b"--\r\n", b"--"):
            continue
        header, _, content = part.partition(b"\r\n\r\n")
        name_m = re.search(rb'name="([^"]+)"', header)
        if name_m:
            fields[name_m.group(1).decode()] = content.rstrip(b"\r\n")
    return fields


class Handler(BaseHTTPRequestHandler):
    engine: OcrEngine = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):
        logger.info("%s %s", self.address_string(), fmt % args)

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # 健康检查惯例：GET /health → 200（监控/负载均衡工具认这个）
        if self.path.rstrip("/") == "/health":
            self._send_json({
                "status": "ok",
                "lang": getattr(self.engine, "lang", "?"),
                "device": getattr(self.engine, "device", "?"),
                "cpu_threads": getattr(self.engine, "cpu_threads", "?"),
            })
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path.rstrip("/") != "/ocr":
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            fields = _parse_multipart(body, self.headers.get("Content-Type", ""))
            img = fields.get("image")
            if not img:
                self._send_json({"error": "缺少 image 字段"}, 400)
                return
            # 语言降级链（multipart data 字段：langs="ch,en,la"）
            langs_raw = fields.get("langs", b"").decode("utf-8", errors="ignore")
            langs = [s.strip() for s in langs_raw.split(",") if s.strip()] or None
            result = self.engine.recognize(img, langs)
            if result.get("busy"):
                self._send_json({"error": "busy, retry later"}, 503)
                return
            self._send_json(result)
        except Exception as exc:  # 显式失败：客户端按 5xx 感知
            logger.exception("OCR 请求失败")
            self._send_json({"error": str(exc)}, 500)


def run_http(engine: OcrEngine, port: int) -> None:
    Handler.engine = engine
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    logger.info("server_paddle 就绪: http://127.0.0.1:%d/ocr", port)
    server.serve_forever()


def run_pipe(engine: OcrEngine, langs: list[str] | None = None) -> None:
    """管道模式：stdin 图片二进制 → stdout JSON。"""
    img = sys.stdin.buffer.read()
    sys.stdout.write(json.dumps(engine.recognize(img, langs), ensure_ascii=False))
    sys.stdout.flush()


def main() -> None:
    ap = argparse.ArgumentParser(description="PaddleOCR HTTP/管道服务")
    ap.add_argument("--port", type=int, default=9008,  # ⚠️ 9007 被 DSH web 占用
                    help="HTTP 监听端口（默认 9008，须与 config.toml [ocr].endpoint 一致）")
    ap.add_argument("--device", choices=("cpu", "gpu"), default="cpu",
                    help="推理设备（默认 cpu——前台显卡占用时友好）")
    ap.add_argument("--lang", choices=tuple(LANGS), default="ch",
                    help="识别语言模型（ch/en/la/el/ru，首次启动自动下载到 ~/.paddlex）")
    ap.add_argument("--cpu-threads", type=int, default=None,
                    help="CPU 推理线程数（默认 os.cpu_count()-1，至少1）")
    ap.add_argument("--lang-confidence", type=float, default=0.6,
                    help="语言降级链接受阈值（平均置信度≥此值才接受，默认0.6）")
    ap.add_argument("--pipe", action="store_true", help="管道模式（stdin→stdout）")
    args = ap.parse_args()

    engine = OcrEngine(
        args.lang, args.device,
        cpu_threads=args.cpu_threads,
        lang_confidence=args.lang_confidence,
    )
    if args.pipe:
        run_pipe(engine)
    else:
        run_http(engine, args.port)


if __name__ == "__main__":
    main()
