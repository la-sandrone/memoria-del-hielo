#!/usr/bin/env python3

"""
文件名: server_bge.py
摘要: BGE 嵌入代理 — 常驻嵌入服务，双端口架构（PORT_LOCAL_FIRST 本地优先 / PORT_REMOTE_FIRST 远端优先）。
      五模型 CPU/GPU 懒加载，硅基流动远端接入，streaming ndjson。
依赖: fastapi, uvicorn, sentence-transformers, torch
"""

from __future__ import annotations
# ── rerank 修复锚点

import asyncio
import json
import logging
import multiprocessing
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, AsyncGenerator

import httpx
import torch
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field


# ── 远端 API 配额（硅基流动；按模型分开，换后端只改这里）──
REMOTE_QUOTAS: dict[str, dict[str, int]] = {
    "BAAI/bge-m3": {"rpm": 2000, "tpm": 500000},
    "BAAI/bge-reranker-v2-m3": {"rpm": 2000, "tpm": 500000},
}

# ══════════════════════════════════════════════════════════════════════════
# 超窗处理策略（2026-10-25 新增）
# ══════════════════════════════════════════════════════════════════════════
# 此前 server_bge **没有任何截断/分块逻辑**（实测 5 个副本：主文件、公开发布版、
# 3 个 .bak，全都只声明 max_length 而不检查）。超长文本被**远端模型静默截断**——
# 调用方以为向量覆盖了全文，其实尾部被丢了，而且不会有任何报错。
#
# 三档（EMBED_OVERFLOW_MODE）：
#   "reject"    超窗直接拒绝，返回 413（**默认**）。让调用方自己决定怎么切，
#               而不是服务端替它猜——静默截断是最坏的一种"帮你"。
#   "average"   按窗口的 EMBED_WINDOW_RATIO 切块 → 各块分别嵌入 →
#               **按每块字符数加权平均**并重新 L2 归一化。
#               依据：分块 + 池化是长文本嵌入的标准做法（Late Chunking,
#               arXiv:2409.04701 原文 "chunking applied after the transformer
#               model and just before mean pooling"）；按长度加权是 mean pooling
#               的加权变体——长块承载的信息多、权重应更高，避免短块把语义拉偏。
#               注意：average 会**增加 token 消耗**（原文被切成多块各自编码）。
#   "truncate"  旧的静默行为（只留前 max_length token），仅为兼容保留。
#
# 切点必须吸附到**字素簇边界**（UAX #29）：直接按字符切会把 emoji ZWJ 序列
# （👨‍👩‍👧‍👦）、组合重音符（a̐）、天城文连字（क्षि）切成两半，产生无意义片段。
EMBED_OVERFLOW_MODE = os.getenv("EMBED_OVERFLOW_MODE", "reject")
EMBED_WINDOW_RATIO = float(os.getenv("EMBED_WINDOW_RATIO", "0.85"))
EMBED_OVERFLOW_STATUS = 413  # HTTP 413 Content Too Large（RFC 9110 §15.5.14）

from sentence_transformers import CrossEncoder, SentenceTransformer

# ── 配置 ──────────────────────────────────────────

# 请在此填写你的硅基流动 API Key
SILICONFLOW_API_KEY = os.environ.get("SILICONFLOW_API_KEY", "写自己的")

# ── 端口配置 ──
PORT_LOCAL_FIRST = 9005    # 本地优先端口（先试本机模型）
PORT_REMOTE_FIRST = 9006   # 远端优先端口（直接走远端）

# ── HTTP 远端请求等待超时（秒）──
# httpx 语义：read timeout = 等待「一个数据块」（响应头或响应体 chunk）的最长时长，
# 不是整个请求的总时长。原硬编码 30.0。调试远端慢响应（ReadTimeout/500）时：
#   调大该值（如 300.0）观察真实耗时；设 None = 完全禁用超时（慎用）。
# 此值为共享 client 的默认超时，统一覆盖 /embeddings 与 /rerank 两处远端调用。
REMOTE_HTTP_TIMEOUT: float | None = 120.0

# ── 对外传输策略：强制 HTTP/1.1 ─────────────────────
# 事实（2026-09-27 实测）：httpx 0.28.1 的 http2 默认即为 False，且 h2 包未安装 →
# 当前链路本就走 HTTP/1.1。故下面显式写死**不是**在修既有故障，而是**防回归**：
# 一旦有人装上 h2、或后续 httpx 改了默认值，这里能挡住"悄悄上 H2"。
# 依据同本仓 2026-09-25「DSH 掉线定案」：H2 的多路复用把故障域从"一条流"扩大到"整个
# 会话"，且连接级有状态（HPACK 动态表 + 双层流控）无法热修复——在移动网络的 TLS 抖动
# 下，一次抖动会被放大成整条连接报废；H1 则把它吸收成一次普通重试。
# 代价：H1 每请求多几个字节头。收益：故障半径可控。
OUTBOUND_HTTP2 = False

# 连接池上限：写死为 httpx 的默认值，目的是**防止"善意调参"**。
# 特别说明 keepalive_expiry=5.0 保持默认：缩短空闲复用窗口**不能**提高成功率，只会
# 增加 TLS 握手次数。抖动应交给重试吸收，而不是靠激进剪连接。
OUTBOUND_LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=5.0,
)

# 是否信任环境里的代理变量（http_proxy / https_proxy / all_proxy）。
# 沿用 src/core/http_client.py 的 2026-08-29 定案：httpx 会把 NO_PROXY 的每一项当 URL
# 解析，带方括号的 IPv6 条目（"[::1]"）会被首个 ':' 切碎 → InvalidURL 直接抛，把调用方
# 一起带走。本项目对外只到国内直连的硅基流动，代理变量没有价值，只增加故障面。
OUTBOUND_TRUST_ENV = False

# h2 包是否可用（决定"意外上 H2"是否在物理上可能）。仅供 /health 与启动日志回显核对。
try:  # pragma: no cover - 环境探测
    import h2 as _h2  # noqa: F401

    _H2_AVAILABLE = True
except ImportError:  # pragma: no cover - 环境探测
    _H2_AVAILABLE = False

# ── 上游（硅基流动）健康探测 ────────────────────────
# 探针端点 /v1/models。实测：不带凭据返 401 {"code":30014,"message":"Token is invalid."}，
# 不见 token、不指定模型、**不消耗配额**，却完整验证 DNS→TCP→TLS→HTTP→应用层。
# 带有效凭据后返 200 → 单探针即可同时区分「上游不可达」「凭据被拒」「健康」。
REMOTE_PROBE_PATH = "/models"

# 探针重试：单次失败信息量约等于 0（网络纪律），故探针内部先递增退避重试，全败才算
# 一次失败。否则一次 TLS 抖动会立刻把"连续失败"计数刷满 → 假告警。
REMOTE_PROBE_ATTEMPTS = 5
REMOTE_PROBE_BACKOFF_BASE = 1.0   # 1s → 2s → 4s → 8s
REMOTE_PROBE_INTERVAL = 30.0      # 两轮探测间隔（秒）
REMOTE_PROBE_TIMEOUT = 10.0       # 单次探测超时；实测正常耗时 160–340ms

# 单笔请求等待限流放行的上限（秒）。对齐 server_paddle.py 的既有项目约定
# （Lock.acquire(timeout=120) → busy 503 → 客户端跳过、下轮续跑）。
# 为什么必须有上限：acquire 是**持 _serial 锁**等待的，无界等待 = 一笔卡死锁死整条
# 远端队列（2026-09-27 实测复现：acquire(600000) 永久阻塞，后续请求全部被锁死）。
REMOTE_ACQUIRE_TIMEOUT = 120.0

# 上游判 down 的阈值：连续失败达到该次数即 down（1 ~ 阈值-1 为 degraded）。
UPSTREAM_FAIL_THRESHOLD = 4

# 滚动窗口大小（供 /health 展示"最近 N 次"的失败率）
UPSTREAM_WINDOW_SIZE = 20

# 错误信息落进 /health 时的截断长度（防止上游回一坨 HTML/Base64 把响应撑爆）
UPSTREAM_MSG_MAX = 300

# API key 占位符：原值是非 ASCII 中文串，被当成 HTTP header 时会抛
# UnicodeEncodeError('ascii', ...) —— 请求**根本发不出去**，表面症状却像"上游连不上"。
# 故单独列出，供启动校验与 /health 明确标为**配置错误**而非上游故障。
_API_KEY_PLACEHOLDER = "写自己的"

# 令牌桶归属名（与 create_app 里 limiter 的配额来源共用一处，避免两处各写一份字面量）
BUCKET_EMBED = "BAAI/bge-m3"
BUCKET_RERANK = "BAAI/bge-reranker-v2-m3"

# 模型清单
MODELS: dict[str, dict] = {
    "BAAI/bge-small-zh-v1.5": {
        "params": "~33M",
        "max_length": 512,
        "default_device": "cpu",
        "dimension": 512,
        "remote_available": False,
    },
    "BAAI/bge-large-zh-v1.5": {
        "params": "~326M",
        "max_length": 1024, 
        "default_device": "cuda:0",
        "dimension": 1024,
        "remote_available": True,
        "remote_model": "BAAI/bge-large-zh-v1.5",
    },
    "BAAI/bge-large-en-v1.5": {
        "params": "~335M",
        "max_length": 1024,
        "default_device": "cuda:0",
        "dimension": 1024,
        "remote_available": True,
        "remote_model": "BAAI/bge-large-en-v1.5",
    },
    "BAAI/bge-m3": {
        "params": "~567M",
        "max_length": 8192,
        "default_device": "cuda:0",
        "dimension": 1024,
        "remote_available": True,
        "remote_model": "BAAI/bge-m3",
    },
    "BAAI/bge-reranker-v2-m3": {
        "params": "~568M",
        "max_length": 8192,
        "dimension": 1024,  # reranker 输出为分数，不输出 embedding
        "remote_available": True,
        "remote_model": "BAAI/bge-reranker-v2-m3",
        "default_device": "cuda:0",  # 缺失会导致本地重排 KeyError 500
        "is_reranker": True,
    },
}

# 懒加载配置
MIN_TTL_SECONDS = 600  # 10 分钟
CPU_THREADS = max(1, os.cpu_count() or 4) // 2

# 硅基流动端点
SILICONFLOW_BASE = "https://api.siliconflow.cn/v1"


def _build_outbound_client(**overrides) -> httpx.AsyncClient:
    """
    构造**唯一**的对外 httpx 客户端：强制 HTTP/1.1 + 固定连接池 + 不信任代理环境变量。

    所有对外连接都必须走这里，不要直接 httpx.AsyncClient(...)——否则新增的连接会绕过
    H1 策略，回到"默认值由最幸运那批人的环境标定"的老问题上去。

    overrides 可覆盖 base_url / timeout 等；策略三项（http2 / limits / trust_env）
    不允许覆盖：它们不是可调项，是纪律。
    """
    forbidden = {"http2", "limits", "trust_env"} & set(overrides)
    if forbidden:
        raise ValueError(
            "outbound 策略项不允许覆盖: "
            + str(sorted(forbidden))
            + "——如需放宽，请先改模块顶部常量并说明理由"
        )
    kwargs: dict = {
        "base_url": SILICONFLOW_BASE,
        "timeout": REMOTE_HTTP_TIMEOUT,
        "http2": OUTBOUND_HTTP2,
        "limits": OUTBOUND_LIMITS,
        "trust_env": OUTBOUND_TRUST_ENV,
    }
    kwargs.update(overrides)
    return httpx.AsyncClient(**kwargs)


# ── 本地缓存检测 ───────────────────────────────────


def _find_modelscope_cache(
    model_name: str, is_reranker: bool = False
) -> str | None:
    """
    检查 ModelScope 缓存中是否有该模型的预下载版本。

    ModelScope 缓存结构为 ~/.cache/modelscope/models/{org}/{model_name}/
    模型文件直接放在该目录下（pytorch_model.bin、modules.json 等），
    没有 hub/ 或 snapshots/ 中间层。

    `download_models.py` 通过 snapshot_download() 预下载到该缓存。
    返回模型目录路径（SentenceTransformer 可直接加载），未命中返回 None。
    """
    cache_root = os.path.expanduser("~/.cache/modelscope")
    # 哨兵按模型类型选：SentenceTransformer 认 modules.json；
    # CrossEncoder（reranker）没有 modules.json，它走
    # AutoModelForSequenceClassification，认 config.json。
    sentinel = "config.json" if is_reranker else "modules.json"

    # ModelScope 会把仓库名里的 "." 存成 "___"
    # （bge-small-zh-v1.5 → bge-small-zh-v1___5）。只按字面名查，
    # 带点的模型永远判未命中 → 绕过本地完整副本、每次转去
    # HuggingFace 重新下载（实测这三个都中招：bge-small-zh-v1.5 /
    # bge-large-zh-v1.5 / bge-large-en-v1.5）。两种形态都试。
    for candidate in (model_name, model_name.replace(".", "___")):
        model_dir = os.path.join(cache_root, "models", candidate)
        if os.path.isdir(model_dir) and os.path.isfile(
            os.path.join(model_dir, sentinel)
        ):
            return model_dir
    return None


# ── 日志 ──────────────────────────────────────────

logger = logging.getLogger("server_bge")


# ══════════════════════════════════════════════════════════════════════════
# 超窗检测与切块（配合顶部 EMBED_OVERFLOW_MODE）
# ══════════════════════════════════════════════════════════════════════════

_TOKENIZERS: dict[str, object] = {}


def _get_tokenizer(model_name: str):
    """拿 tokenizer（只读 tokenizer 文件，不加载模型权重）。失败返回 None。

    回退链：modelscope 本地缓存 → HF 模型名（可能触发下载）。
    都没有时返回 None，调用方退回「字符估算」——精度差但不会崩。
    """
    if model_name in _TOKENIZERS:
        return _TOKENIZERS[model_name]
    tok = None
    try:
        from transformers import AutoTokenizer
        src = _find_modelscope_cache(model_name) or model_name
        tok = AutoTokenizer.from_pretrained(src)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "tokenizer 不可用（%s）: %s —— 超窗判断回退到字符估算", model_name, exc
        )
    _TOKENIZERS[model_name] = tok
    return tok


def _count_tokens(text: str, tok) -> int:
    """token 数（不含特殊 token）。无 tokenizer 时保守估算。

    估算口径：中文按 1 字 1 token（实际 XLM-R 约 4 字/token，这里故意高估），
    其余按 3 字符 1 token。高估只会让「本该通过的被判超窗」，不会漏判。
    """
    if not text:
        return 0
    if tok is None:
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        return cjk + (len(text) - cjk) // 3
    enc = tok(text, add_special_tokens=False, truncation=False,
              return_length=True)
    n = enc["length"] if not isinstance(enc["length"], list) else enc["length"][0]
    return int(n)


def _grapheme_bounds(text: str) -> list[int]:
    """字素簇边界（升序，含 0 与 len(text)）。regex 的 \\X 遵循 UAX #29。

    直接按字符下标切会把 emoji ZWJ 序列、组合重音符、天城文连字切成两半，
    产生无意义片段——所以切点必须吸附到这些边界上。
    """
    try:
        import regex as _rx
        return [0] + [m.end() for m in _rx.finditer(r"\X", text)]
    except Exception:  # noqa: BLE001
        logger.warning("regex 模块不可用，切点退化为按字符切分")
        return list(range(len(text) + 1))


def _snap_forward(pos: int, bounds: list[int]) -> int:
    """把字符位置吸附到**不小于**它的最近字素簇边界。"""
    lo, hi = 0, len(bounds) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if bounds[mid] < pos:
            lo = mid + 1
        else:
            hi = mid
    return bounds[lo]


def _split_by_tokens(text: str, tok, window: int) -> list[str]:
    """按 window 个 token 切块，切点吸附字素簇。返回非空块列表。"""
    if tok is None or window <= 0:
        # 无 tokenizer：按「1 token ≈ 1 字符」的最保守口径切
        step = max(1, window)
        return [text[i:i + step] for i in range(0, len(text), step)]

    bounds = _grapheme_bounds(text)
    enc = tok(text, add_special_tokens=False, truncation=False,
              return_offsets_mapping=True)
    offs = enc["offset_mapping"]
    # XLM-R 的 offsets 可能含 (0,0) 特殊位；过滤掉零长度项
    offs = [(s, e) for (s, e) in offs if e > s]
    if not offs:
        return [text]

    chunks: list[str] = []
    i = 0
    n = len(offs)
    while i < n:
        j = min(i + window, n)
        start_ch = _snap_forward(offs[i][0], bounds) if chunks else 0
        end_ch = _snap_forward(offs[j - 1][1], bounds) if j < n else len(text)
        if end_ch <= start_ch:          # 保护：极端情况下至少推进一个 token
            end_ch = _snap_forward(offs[j - 1][1], bounds)
        chunk = text[start_ch:end_ch]
        if chunk:
            chunks.append(chunk)
        i = j
    return chunks or [text]


def _weighted_mean(vecs: list[list[float]], weights: list[int]) -> list[float]:
    """按权重求平均后重新 L2 归一化。

    为什么加权：长块承载的信息多，若等权平均，一个短碎片也能把语义拉偏。
    为什么要重新归一化：余弦距离要求单位向量；加权和不再是单位长度。
    """
    import numpy as np

    arr = np.asarray(vecs, dtype=np.float32)
    w = np.asarray(weights, dtype=np.float32)
    if w.sum() <= 0:
        w = np.ones_like(w)
    w = w / w.sum()
    v = (arr * w[:, None]).sum(axis=0)
    norm = float(np.linalg.norm(v))
    if norm > 0:
        v = v / norm
    return v.tolist()


def _model_max_len(model_name: str) -> int:
    cfg = MODELS.get(model_name) or {}
    return int(cfg.get("max_length") or 8192)


def _plan_overflow(texts: list[str], model_name: str):
    """按 EMBED_OVERFLOW_MODE 规划如何送嵌入。

    返回 (prepared, mapping)：
      prepared — 实际要送去嵌入的文本列表（average 模式下被展开）
      mapping  — 与**原始 texts** 一一对应；每项是
                 None（prepared 里同位置原样对应）
                 或 (start, count, weights)（聚合自 prepared[start:start+count]）
    超窗且 mode=reject 时抛 HTTPException(413)。
    """
    max_len = _model_max_len(model_name)
    # 留给特殊 token 的余量：比例由 EMBED_WINDOW_RATIO 控制（默认 85%）
    window = max(1, int(max_len * EMBED_WINDOW_RATIO))
    mode = (EMBED_OVERFLOW_MODE or "reject").lower()

    tok = _get_tokenizer(model_name)
    prepared: list[str] = []
    mapping: list = []
    cursor = 0

    for idx, text in enumerate(texts):
        n_tok = _count_tokens(text, tok)
        if n_tok <= max_len:
            prepared.append(text)
            mapping.append(None)
            cursor += 1
            continue

        if mode == "truncate":
            # 旧行为：留前 max_len 个 token（按字符近似）
            keep = max(1, int(len(text) * max_len / max(1, n_tok)))
            prepared.append(text[:keep])
            mapping.append(None)
            cursor += 1
            logger.warning(
                "输入 #%d 超窗（%d > %d tokens），按 truncate 策略截断到约 %d 字符",
                idx, n_tok, max_len, keep,
            )
            continue

        if mode == "average":
            chunks = _split_by_tokens(text, tok, window)
            start = cursor
            prepared.extend(chunks)
            cursor += len(chunks)
            mapping.append((start, len(chunks), [len(c) for c in chunks]))
            logger.info(
                "输入 #%d 超窗（%d > %d tokens）→ 切成 %d 块（窗口 %d），算后按长度加权平均",
                idx, n_tok, max_len, len(chunks), window,
            )
            continue

        # 默认 reject：明确报错，让调用方自己切
        raise HTTPException(
            status_code=EMBED_OVERFLOW_STATUS,
            detail=(
                f"输入 #{idx} 超出模型窗口：{n_tok} tokens > {max_len}"
                f"（model={model_name}）。请自行切块后分别请求，"
                f"或把服务端 EMBED_OVERFLOW_MODE 设为 average / truncate。"
            ),
        )

    return prepared, mapping, max_len


# ── 模型管理器 ─────────────────────────────────────

class ModelManager:
    """
    模型加载管理器 — 懒加载 + 最小存活时间。

    每个模型首次调用时加载到指定设备。
    10 分钟内无调用 → 卸载释放显存/内存。
    """

    def __init__(self, device: str = "cuda:0") -> None:
        self._device = device
        self._loaded: dict[str, tuple[SentenceTransformer, float]] = {}
        self._lock = asyncio.Lock()

    async def get_model(
        self, model_name: str
    ) -> SentenceTransformer | CrossEncoder:
        """
        获取模型（懒加载 + 刷新存活时间戳）。
        """
        async with self._lock:
            now = time.monotonic()

            # 已加载 → 刷新时间戳
            if model_name in self._loaded:
                model, _ = self._loaded[model_name]
                self._loaded[model_name] = (model, now)
                return model

            # 未加载 → 加载
            cfg = MODELS.get(model_name)
            if cfg is None:
                raise ValueError(f"未知模型: {model_name}")

            device = cfg["default_device"]
            logger.info("Loading model %s to %s ...", model_name, device)

            is_reranker = bool(cfg.get("is_reranker"))

            # 先检查 ModelScope 本地缓存
            local_path = _find_modelscope_cache(
                model_name, is_reranker=is_reranker
            )
            if local_path:
                logger.info("  Found in ModelScope cache: %s", local_path)
                model_source = local_path
            else:
                logger.info("  Not in local cache, downloading from HuggingFace ...")
                model_source = model_name

            # 异步加载（在 executor 中运行以避免阻塞事件循环）
            loop = asyncio.get_event_loop()
            loader = CrossEncoder if is_reranker else SentenceTransformer
            model = await loop.run_in_executor(
                None,
                lambda: loader(
                    model_source,
                    device=device,
                    trust_remote_code=True,
                ),
            )

            self._loaded[model_name] = (model, now)
            logger.info("Model %s loaded (device=%s)", model_name, device)
            return model

    async def unload_expired(self) -> int:
        """
        卸载过期的模型（超过 MIN_TTL_SECONDS 无调用）。

        返回: 卸载的模型数量。
        """
        async with self._lock:
            now = time.monotonic()
            to_unload: list[str] = []
            for name, (_, last_used) in self._loaded.items():
                if now - last_used > MIN_TTL_SECONDS:
                    to_unload.append(name)

            for name in to_unload:
                model, _ = self._loaded.pop(name)
                # 移动到 CPU 再释放（防止 GPU 显存泄漏）
                try:
                    model.cpu()
                except Exception:
                    pass
                del model
                logger.info("Unloaded model %s (expired TTL)", name)

            # 清理 CUDA 缓存
            if to_unload and torch.cuda.is_available():
                torch.cuda.empty_cache()

            return len(to_unload)

    def get_loaded_models(self) -> dict[str, float]:
        """返回已加载模型及其剩余 TTL（秒）。"""
        now = time.monotonic()
        result: dict[str, float] = {}
        for name, (_, last_used) in self._loaded.items():
            remaining = max(0.0, MIN_TTL_SECONDS - (now - last_used))
            result[name] = remaining
        return result


class RateLimiterBusy(Exception):
    """等待限流放行超时（有界等待用尽）→ 端点回 503 busy，由调用方降级重试。

    对齐 server_paddle.py 的既有约定（排队超 120s → busy 503，客户端跳过下轮续跑）。
    """

    def __init__(self, waited_s: float, limit_s: float, detail: str = "") -> None:
        super().__init__(
            "限流排队超时（已等 %.1fs / 上限 %.0fs）：%s" % (waited_s, limit_s, detail)
        )
        self.waited_s = waited_s
        self.limit = limit_s
        self.detail = detail


class RateLimiterOversize(ValueError):
    """单笔请求的 token 估算超过整个窗口配额——令牌桶**永远无法**满足它。

    必须**显式拒绝而不是等待**：桶容量上限就是 tpm，`_tok_tokens >= tokens` 永不成立，
    等待将永久阻塞且持锁（这正是 2026-09-27 查出的死锁）。
    继承 ValueError，使端点既有的 `except ValueError` 自动映射为 400。
    """

    def __init__(self, tokens: int, tpm: int) -> None:
        super().__init__(
            "单笔请求 %d tokens 超过 TPM 上限 %d：本地限流器无法规约，"
            "请拆成多笔（每笔 ≤ %d）后重试" % (tokens, tpm, tpm)
        )
        self.tokens = tokens
        self.tpm = tpm


class RateLimiter:
    """远端 RPM/TPM 令牌桶（平滑补充）+ 按到达顺序串行转发（FIFO）。

    容量 = 配额（满桶起步），补充速率 = 配额/60 每秒 —— 突发后平滑恢复，
    不会像滑动窗口那样锁死整个 60s 窗口。
    rpm/tpm = 0 表示不限流（串行锁仍生效：请求按到达顺序排队）。
    配置: 环境变量 REMOTE_RPM / REMOTE_TPM（0=不限，默认 0）。
    """

    def __init__(self, rpm: int = 0, tpm: int = 0, window: float = 60.0) -> None:
        self.rpm = rpm
        self.tpm = tpm
        self.window = window
        self._serial = asyncio.Lock()
        self._req_tokens = float(rpm) if rpm > 0 else 0.0
        self._tok_tokens = float(tpm) if tpm > 0 else 0.0
        self._last_refill = time.monotonic()
        # 累计计费（供 /health 汇报"本进程一共从桶里取了多少"）——只增不减。
        self._charged_requests = 0
        self._charged_tokens = 0
        # 真实用量滑窗：每笔放行记录 (单调时刻, tokens)。桶的"瞬时亏空"在平滑补充下
        # 毫秒级就补回（2000rpm → 33/s），**答不了"这一分钟用了多少"**；滑窗才是答案。
        self._history: deque[tuple[float, int]] = deque()
        # 有界等待 / 超大单笔的计数（供 /health 暴露，便于判读"是不是在反复撞限流"）
        self._busy_events = 0
        self._oversize_rejected = 0

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now
        if self.rpm > 0:
            self._req_tokens = min(
                float(self.rpm), self._req_tokens + elapsed * self.rpm / self.window
            )
        if self.tpm > 0:
            self._tok_tokens = min(
                float(self.tpm), self._tok_tokens + elapsed * self.tpm / self.window
            )

    def _prune_history(self, now: float) -> None:
        """丢弃滑窗之外的记录（只保留最近 window 秒）。"""
        cutoff = now - self.window
        hist = self._history
        while hist and hist[0][0] < cutoff:
            hist.popleft()

    async def acquire(self, tokens: int = 0, timeout: float | None = None) -> None:
        """按到达顺序等待放行。**两种失败都是有界的**（原实现是无界循环 → 持锁死锁）。

        tokens  = 本次请求预估消耗 token 数（TPM 用）。
        timeout = 等待上限（秒）；None 取 REMOTE_ACQUIRE_TIMEOUT。

          · RateLimiterOversize —— tokens > tpm：桶永远满足不了，**立即拒绝**（不排队）。
          · RateLimiterBusy     —— 等待超过 timeout：快速失败，由调用方降级重试。
        """
        # 超大单笔：桶容量上限就是 tpm，等待永远不会成功——必须立刻拒绝而不是排队。
        if self.tpm > 0 and tokens > self.tpm:
            self._oversize_rejected += 1
            raise RateLimiterOversize(tokens, self.tpm)

        limit_s = REMOTE_ACQUIRE_TIMEOUT if timeout is None else timeout
        t_start = time.monotonic()
        deadline = t_start + limit_s
        async with self._serial:
            while True:
                self._refill()
                req_ok = self.rpm <= 0 or self._req_tokens >= 1.0
                tok_ok = self.tpm <= 0 or tokens <= 0 or self._tok_tokens >= tokens
                if req_ok and tok_ok:
                    if self.rpm > 0:
                        self._req_tokens -= 1.0
                    if self.tpm > 0 and tokens > 0:
                        self._tok_tokens -= tokens
                    self._charged_requests += 1
                    self._charged_tokens += max(0, tokens)
                    now = time.monotonic()
                    self._history.append((now, max(0, tokens)))
                    self._prune_history(now)
                    return
                if time.monotonic() >= deadline:
                    self._busy_events += 1
                    raise RateLimiterBusy(
                        time.monotonic() - t_start,
                        limit_s,
                        "rpm_available=%.1f tpm_available=%.1f need_tokens=%d"
                        % (self._req_tokens, self._tok_tokens, tokens),
                    )
                await asyncio.sleep(0.05)

    def snapshot(self) -> dict:
        """
        令牌桶状态 + **真实用量滑窗**（供 /health 汇报）。只读、无副作用。

        为什么两套数字都要：
          · `available` = 桶的瞬时余量。平滑补充下每次放行约 30ms 就补回，所以
            「limit - available」**不是用量**（旧实现把它叫 used_in_window，名不副实：
            实测 6 笔请求后仍读 0.0）。
          · `requests_last_60s` / `tokens_last_60s` = 真实用量滑窗，这才是
            「这一分钟我用了多少 RPM / TPM」的答案。
        """
        self._refill()
        now = time.monotonic()
        self._prune_history(now)
        win_requests = len(self._history)
        win_tokens = sum(n for _, n in self._history)
        rpm_avail = self._req_tokens if self.rpm > 0 else None
        tpm_avail = self._tok_tokens if self.tpm > 0 else None
        return {
            "rpm": {
                "unlimited": self.rpm <= 0,
                "limit": self.rpm,
                "available": round(rpm_avail, 2) if rpm_avail is not None else None,
                "refill_per_sec": round(self.rpm / self.window, 3) if self.rpm > 0 else None,
                "window_sec": self.window,
                "requests_last_60s": win_requests,
                "cumulative_charged": self._charged_requests,
                "busy_events": self._busy_events,
                "oversize_rejected": self._oversize_rejected,
                "acquire_timeout_sec": REMOTE_ACQUIRE_TIMEOUT,
            },
            "tpm": {
                "unlimited": self.tpm <= 0,
                "limit": self.tpm,
                "available": round(tpm_avail, 2) if tpm_avail is not None else None,
                "refill_per_sec": round(self.tpm / self.window, 3) if self.tpm > 0 else None,
                "window_sec": self.window,
                "tokens_last_60s": win_tokens,
                "cumulative_charged": self._charged_tokens,
                # 口径声明：TPM 扣的是 len(text) 字符数估算，不是真实 token 数。
                # 实测校准（2026-09-27，用上游 usage 字段）：嵌入侧逐例相等（1.00x）；
                # 重排侧对较长文档高估（0.36–0.41x）→ 偏保守、提前限流，不会超配额。
                "accounting": "char_estimate",
            },
        }


def _classify_kind(exc: BaseException) -> str:
    """把异常分型成 /health 里的 kind，便于快速判读（配合 ms 字段定案）。"""
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connect_error"
    if isinstance(exc, httpx.HTTPStatusError):
        return "rate_limited" if exc.response.status_code == 429 else "http_error"
    if isinstance(exc, httpx.TransportError):
        return "transport_error"
    return type(exc).__name__


def _validate_api_key(key: str) -> tuple[bool, str]:
    """
    校验远端 API key 是否可用。返回 (是否可用, 状态描述)。

    ⚠️ 关键区分：key 的三种问题都是**本地配置错误**，绝不能报成"上游故障"——
    否则 /health 会替用户做出错误归因（实测踩过：中文占位符导致 header 组装就抛
    UnicodeEncodeError，请求压根没发出去，而表面症状像"上游连不上"）。
    """
    if not key or key == _API_KEY_PLACEHOLDER:
        return False, "未配置（仍为占位符 " + repr(_API_KEY_PLACEHOLDER) + "）"
    if not key.isascii():
        # httpx 组装 header 时会把非 ASCII 值按 ascii 编码 → UnicodeEncodeError
        return False, "含非 ASCII 字符（httpx 组装 header 会抛 UnicodeEncodeError，请求发不出去）"
    return True, "ok"


def _describe_upstream_error(exc: BaseException, resp: "httpx.Response | None") -> str:
    """
    把异常 + 响应压成一行**可定案**的说明。

    分型（配合 /health 的 kind 与 ms 字段读）：
      · 连接类（ConnectError / ConnectTimeout / ReadTimeout ...）→ 看 ms：
          ms < 50   连接层没走网络（本地链路/代理/凭据问题），不是上游
          ms > 100  真走了网络（抖动），链路侧或上游侧
      · HTTPStatusError → 带上游响应体摘要（截断），保留上游的 {code, message}
    """
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            body = exc.response.text[:UPSTREAM_MSG_MAX]
        except Exception:
            body = "<响应体读取失败>"
        return "HTTP %d from %s: %s" % (exc.response.status_code, exc.request.url.host, body)
    if isinstance(exc, httpx.ConnectTimeout):
        return "ConnectTimeout: 建连超时（本地链路/代理问题优先于上游问题）: %s" % exc
    if isinstance(exc, httpx.TimeoutException):
        return "%s: 等待数据块超时: %s" % (type(exc).__name__, exc)
    if isinstance(exc, httpx.TransportError):
        return "%s: 传输层错误: %s" % (type(exc).__name__, exc)
    return "%s: %s" % (type(exc).__name__, exc)


class UpstreamHealth:
    """
    上游健康追踪 = 被动记录真实调用 + 主动周期探测。

    为什么两者都要：
      · 被动记录 = 用户实际感受到的上游健康（真实调用路径，含 429 / 超时 / 5xx）
      · 主动探测 = 独立证据。**没有流量时被动记录永远停在 unknown**——你问上游好不好，
        它只能答"不知道"。探针补上这个盲区，且不消耗模型配额。

    状态机（/health 的 upstream.state）：
      unknown        无任何数据
      healthy        最近一次成功且无连续失败
      degraded       连续失败 1 ~ (阈值-1) 次
      down           连续失败 >= 阈值（默认 4）
      misconfigured  本地凭据无效/被拒——**不是上游故障**，独立成一档
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._key_ok, self._key_note = _validate_api_key(api_key)
        self._key_rejected = False
        self._lock = asyncio.Lock()
        self._consecutive_failures = 0
        self._serving = {"total": 0, "local": 0, "remote": 0, "remote_errors": 0}
        self._calls: dict[str, dict] = {}
        self._window: list[bool] = []
        self._last_error: dict | None = None
        self._last_success_iso: str | None = None
        self._probe_last_status: int | None = None
        self._probe_last_ms: float | None = None
        self._probe_last_ok_iso: str | None = None
        self._probe_last_error: dict | None = None
        self._probe_rounds = 0
        self._probe_fail_rounds = 0

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _empty_calls() -> dict:
        return {"total": 0, "success": 0, "rate_limited": 0, "total_ms": 0.0}

    def _trim_window(self) -> None:
        if len(self._window) > UPSTREAM_WINDOW_SIZE:
            del self._window[:-UPSTREAM_WINDOW_SIZE]

    # ── 被动：真实调用 ──
    async def record_request(self, use_local: bool) -> None:
        async with self._lock:
            self._serving["total"] += 1
            self._serving["local" if use_local else "remote"] += 1

    async def record_success(self, bucket: str, endpoint: str, ms: float) -> None:
        async with self._lock:
            c = self._calls.setdefault(bucket, self._empty_calls())
            c["total"] += 1
            c["success"] += 1
            c["total_ms"] += ms
            self._consecutive_failures = 0
            self._window.append(True)
            self._trim_window()
            self._last_success_iso = self._now_iso()

    async def record_failure(
        self,
        bucket: str,
        endpoint: str,
        exc: BaseException,
        *,
        status: int | None,
        ms: float,
        message: str,
    ) -> None:
        async with self._lock:
            c = self._calls.setdefault(bucket, self._empty_calls())
            c["total"] += 1
            c["total_ms"] += ms
            if status == 429:
                c["rate_limited"] += 1
            self._serving["remote_errors"] += 1
            self._consecutive_failures += 1
            self._window.append(False)
            self._trim_window()
            self._last_error = {
                "ts": self._now_iso(),
                "kind": _classify_kind(exc),
                "bucket": bucket,
                "endpoint": endpoint,
                "status": status,
                "ms": round(ms, 1),
                "message": message[:UPSTREAM_MSG_MAX],
            }

    # ── 主动：探测（内部含递增退避重试）──
    async def probe_once(self) -> None:
        if not self._key_ok:
            # 凭据无效时**不发探测**：免得把自己的配置问题报成上游故障。
            async with self._lock:
                self._probe_rounds += 1
                self._probe_last_error = {
                    "ts": self._now_iso(),
                    "kind": "config",
                    "status": None,
                    "ms": None,
                    "message": "跳过探测：本地凭据不可用（" + self._key_note + "）",
                }
            return

        headers = {"Authorization": "Bearer " + self._api_key}
        last_exc: BaseException | None = None
        last_resp: "httpx.Response | None" = None
        last_ms: float | None = None
        for attempt in range(1, REMOTE_PROBE_ATTEMPTS + 1):
            t0 = time.monotonic()
            try:
                async with _build_outbound_client(timeout=REMOTE_PROBE_TIMEOUT) as client:
                    last_resp = await client.get(REMOTE_PROBE_PATH, headers=headers)
                last_ms = (time.monotonic() - t0) * 1000
                last_exc = None
                break
            except Exception as exc:  # 探测就是要兜住一切并分型上报
                last_ms = (time.monotonic() - t0) * 1000
                last_exc = exc
                last_resp = None
                if attempt < REMOTE_PROBE_ATTEMPTS:
                    await asyncio.sleep(REMOTE_PROBE_BACKOFF_BASE * (2 ** (attempt - 1)))

        async with self._lock:
            self._probe_rounds += 1
            self._probe_last_ms = round(last_ms, 1) if last_ms is not None else None
            status = last_resp.status_code if last_resp is not None else None
            self._probe_last_status = status
            if last_exc is not None:
                self._probe_fail_rounds += 1
                self._probe_last_error = {
                    "ts": self._now_iso(),
                    "kind": _classify_kind(last_exc),
                    "status": None,
                    "ms": self._probe_last_ms,
                    "attempts": REMOTE_PROBE_ATTEMPTS,
                    "message": _describe_upstream_error(last_exc, None),
                }
                self._consecutive_failures += 1
                self._window.append(False)
            elif status is not None and status < 400:
                self._probe_last_ok_iso = self._now_iso()
                self._last_success_iso = self._probe_last_ok_iso
                self._probe_last_error = None
                self._key_rejected = False
                self._consecutive_failures = 0
                self._window.append(True)
            elif status in (401, 403):
                # 上游可达、凭据被拒 → **配置错误**，不是上游故障：不累加失败计数。
                self._key_rejected = True
                self._probe_last_error = {
                    "ts": self._now_iso(),
                    "kind": "config",
                    "status": status,
                    "ms": self._probe_last_ms,
                    "message": "上游可达但凭据被拒（HTTP %d）：%s"
                    % (status, last_resp.text[:UPSTREAM_MSG_MAX]),
                }
                self._consecutive_failures = 0
                self._window.append(True)
            else:
                self._probe_last_error = {
                    "ts": self._now_iso(),
                    "kind": "http_error",
                    "status": status,
                    "ms": self._probe_last_ms,
                    "message": "HTTP %s: %s"
                    % (status, last_resp.text[:UPSTREAM_MSG_MAX] if last_resp is not None else ""),
                }
                self._consecutive_failures += 1
                self._window.append(False)
            self._trim_window()

    # ── 快照 ──
    def _state_locked(self) -> str:
        if not self._key_ok or self._key_rejected:
            return "misconfigured"
        if self._consecutive_failures >= UPSTREAM_FAIL_THRESHOLD:
            return "down"
        if self._consecutive_failures > 0:
            return "degraded"
        if self._last_success_iso is not None:
            return "healthy"
        return "unknown"

    async def serving_snapshot(self) -> dict:
        async with self._lock:
            return dict(self._serving)

    async def snapshot(self) -> dict:
        async with self._lock:
            state = self._state_locked()
            calls: dict[str, dict] = {}
            for bucket, c in self._calls.items():
                total = c["total"]
                calls[bucket] = {
                    "total": total,
                    "success": c["success"],
                    "errors": total - c["success"],
                    "rate_limited": c["rate_limited"],
                    "avg_ms": round(c["total_ms"] / total, 1) if total else None,
                }
            fails = sum(1 for ok in self._window if not ok)
            return {
                "state": state,
                "ok": state in ("healthy", "unknown"),  # unknown 取乐观：避免启动/首轮探测窗口内的假告警；需严格判据请直接读 upstream.state
                "api_key": {"usable": self._key_ok, "note": self._key_note},
                "checked_at": self._now_iso(),
                "consecutive_failures": self._consecutive_failures,
                "fail_threshold": UPSTREAM_FAIL_THRESHOLD,
                "probe": {
                    "path": REMOTE_PROBE_PATH,
                    "interval_sec": REMOTE_PROBE_INTERVAL,
                    "attempts_per_round": REMOTE_PROBE_ATTEMPTS,
                    "rounds": self._probe_rounds,
                    "fail_rounds": self._probe_fail_rounds,
                    "last_status": self._probe_last_status,
                    "last_ms": self._probe_last_ms,
                    "last_ok_at": self._probe_last_ok_iso,
                    "last_error": self._probe_last_error,
                },
                "calls": calls,
                "window": {
                    "size": UPSTREAM_WINDOW_SIZE,
                    "n": len(self._window),
                    "failures": fails,
                },
                "last_success_at": self._last_success_iso,
                "last_error": self._last_error,
            }


# ── 嵌入服务 ──────────────────────────────────────

def create_app(
    port: int,
    local_first: bool,
    remote_api_key: str = SILICONFLOW_API_KEY,
) -> FastAPI:
    """
    创建 FastAPI 应用实例。

    local_first=True  → PORT_LOCAL_FIRST，本地模型优先
    local_first=False → PORT_REMOTE_FIRST，远端服务优先
    """
    manager = ModelManager()
    # 对外 client 一律经 _build_outbound_client()：强制 HTTP/1.1 + 固定连接池 + 不信任代理env
    http_client = _build_outbound_client()

    mode_label = "本地优先" if local_first else "远端优先"
    # 远端 RPM/TPM 限流（配额见顶部 REMOTE_QUOTAS，按模型分开）：
    # 所有远端请求按到达顺序串行转发（FIFO），并受令牌桶约束。
    limiter_embed = RateLimiter(
        rpm=REMOTE_QUOTAS[BUCKET_EMBED]["rpm"],
        tpm=REMOTE_QUOTAS[BUCKET_EMBED]["tpm"],
    )
    limiter_rerank = RateLimiter(
        rpm=REMOTE_QUOTAS[BUCKET_RERANK]["rpm"],
        tpm=REMOTE_QUOTAS[BUCKET_RERANK]["tpm"],
    )

    # 上游健康追踪：被动记录真实调用 + 主动周期探测（探针任务在 lifespan 里启动）
    tracker = UpstreamHealth(remote_api_key)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """启动时和关闭时的生命周期管理。"""
        # 启动后台 TTL 检查任务
        task = asyncio.create_task(_ttl_loop())
        # 启动上游健康探测任务（独立证据：无流量时被动记录会一直停在 unknown）
        probe_task = asyncio.create_task(_probe_loop())
        # 启动时把传输策略与凭据状态打一行——无头模式下这是最先可见的一行。
        key_ok, key_note = _validate_api_key(remote_api_key)
        logger.info(
            "server_bge [%s] started on port %d | transport http2=%s h2_available=%s "
            "trust_env=%s keepalive_expiry=%s | api_key=%s",
            mode_label,
            port,
            OUTBOUND_HTTP2,
            _H2_AVAILABLE,
            OUTBOUND_TRUST_ENV,
            OUTBOUND_LIMITS.keepalive_expiry,
            "ok" if key_ok else ("不可用（" + key_note + "）"),
        )
        yield
        task.cancel()
        probe_task.cancel()
        await http_client.aclose()
        logger.info("server_bge [%s] stopped", mode_label)

    app = FastAPI(
        title=f"BGE Embedding Server ({mode_label})",
        version="0.1.0",
        lifespan=lifespan,
    )

    # ── 后台任务 ──

    async def _ttl_loop() -> None:
        """每 60 秒检查一次模型 TTL。"""
        while True:
            await asyncio.sleep(60)
            unloaded = await manager.unload_expired()
            if unloaded:
                logger.info("TTL sweep: unloaded %d model(s)", unloaded)

    async def _probe_loop() -> None:
        """每 REMOTE_PROBE_INTERVAL 秒探测一次上游可达性。

        首轮先 sleep：让服务先把 HTTP 端口起好、日志打出来，不抢启动窗口。
        探测自身异常在 probe_once 内部已兜住并分型上报；此处再兜一层，异常即记录，
        绝不让任务静默死掉（以静默降级为耻）。
        """
        while True:
            try:
                await asyncio.sleep(REMOTE_PROBE_INTERVAL)
                await tracker.probe_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "upstream probe loop error: %s: %s", type(exc).__name__, exc
                )

    # ── 辅助函数 ──

    async def _embed_local(
        model_name: str,
        texts: list[str],
        normalize: bool = True,
    ) -> list[list[float]]:
        """本地模型嵌入。"""
        model = await manager.get_model(model_name)
        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(
            None,
            lambda: model.encode(
                texts,
                normalize_embeddings=normalize,
                show_progress_bar=False,
            ),
        )
        return embeddings.tolist()

    async def _embed_remote(
        model_name: str,
        texts: list[str],
    ) -> list[list[float]]:
        """硅基流动远端嵌入（带上游健康取证）。"""
        # 远端限流排队（按到达顺序；tokens = 字符数估算，中文 ~1 token/字）
        await limiter_embed.acquire(sum(len(t) for t in texts))

        # 远端 429（配额抖动）：退避重试（1s/2s/4s，最多 3 次），仍失败才上抛
        t0 = time.monotonic()
        resp = None
        try:
            for attempt in range(3):
                resp = await http_client.post(
                    "/embeddings",
                    json={
                        "model": model_name,
                        "input": texts,
                        "encoding_format": "float",
                    },
                    headers={"Authorization": f"Bearer {remote_api_key}"},
                )
                if resp.status_code != 429 or attempt == 2:
                    break
                await asyncio.sleep(2**attempt)
            resp.raise_for_status()
        except Exception as exc:
            await tracker.record_failure(
                BUCKET_EMBED,
                "/embeddings",
                exc,
                status=getattr(resp, "status_code", None),
                ms=(time.monotonic() - t0) * 1000,
                message=_describe_upstream_error(exc, resp),
            )
            raise
        await tracker.record_success(
            BUCKET_EMBED, "/embeddings", (time.monotonic() - t0) * 1000
        )
        data = resp.json()
        # 按 index 排序
        sorted_data = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in sorted_data]

    def _should_use_local(model_name: str) -> bool:
        """判断是否应该使用本地模型。"""
        cfg = MODELS.get(model_name)
        if cfg is None:
            return False
        if local_first:
            # PORT_LOCAL_FIRST：本地优先，除非显式加 remote: 前缀
            return True
        else:
            # PORT_REMOTE_FIRST：远端优先，除非显式加 local: 前缀
            return False

    def _resolve_model_name(raw: str) -> tuple[str, bool]:
        """
        解析模型名称，返回 (实际模型名, 是否强制本地)。

        "local:BAAI/bge-m3" → 强制本地
        "remote:xxx" → 强制远端
        "BAAI/bge-m3" → 按端口策略决定
        """
        force_local = False
        if raw.startswith("local:"):
            force_local = True
            raw = raw[6:]
        elif raw.startswith("remote:"):
            force_local = False
            raw = raw[7:]
        return raw, force_local

    def _get_remote_model_name(model_name: str) -> str:
        """获取远端模型名称。"""
        cfg = MODELS.get(model_name)
        if cfg and cfg.get("remote_available"):
            return cfg.get("remote_model", model_name)
        return model_name

    # ── 生成 streaming 响应 ──

    async def _stream_embeddings(
        texts: list[str],
        model_name: str,
        use_local: bool,
    ) -> AsyncGenerator[str, None]:
        """逐条输出 embedding（ndjson over chunked）。

        流一旦开始就无法再改 HTTP 状态码，所以限流/上游错误以
        `{"type": "error"}` 事件**显式送达**，而不是让流莫名其妙地断掉。
        调用方应检查 error 事件（fail loud，不静默截断）。
        """
        total = len(texts)
        for i, text in enumerate(texts):
            try:
                if use_local:
                    emb = await _embed_local(model_name, [text])
                else:
                    emb = await _embed_remote(model_name, [text])
            except (RateLimiterBusy, RateLimiterOversize) as exc:
                yield json.dumps({
                    "type": "error",
                    "index": i,
                    "total": total,
                    "error": type(exc).__name__,
                    "busy": isinstance(exc, RateLimiterBusy),
                    "message": str(exc),
                }, ensure_ascii=False) + "\n"
                return
            yield json.dumps({
                "type": "progress",
                "index": i,
                "total": total,
                "embedding": emb[0],
            }, ensure_ascii=False) + "\n"

        yield json.dumps({
            "type": "done",
            "total": total,
        }) + "\n"

    # ── API 端点 ──

    @app.get("/health")
    async def health():
        """
        健康检查 + 令牌桶用量 + 上游健康状况。

        返回码约定：**恒 200**，`status` 恒为 "ok"——它只表示"本服务进程存活"。
        上游故障体现在 body（`ok` / `upstream.state` / `upstream.last_error`）与
        **被明确归因的 config 错误**里。

        为什么不因上游故障返 503：
        scripts/diagnose_import.py、scripts/reimport_ocr_night.py、
        scripts/_debug_verify_giant.py 与 docs/OPERATIONS.md 都按 200 判活，返 503 会把
        这几个现成消费者一起打翻。无头模式（PECMD 隐藏输出）下 /health 是唯一可见窗口，
        故错误信息必须自足：kind + ms + status + message 四项齐备，单看一行即可定案。
        """
        key_ok, key_note = _validate_api_key(remote_api_key)
        upstream = await tracker.snapshot()
        return {
            # ── 向后兼容字段（原有消费者原样可用）──
            "status": "ok",
            "mode": mode_label,
            "port": port,
            "loaded_models": {
                name: f"{ttl:.0f}s remaining"
                for name, ttl in manager.get_loaded_models().items()
            },
            # ── 以下为新增 ──
            # 综合位：服务存活 且 上游不在 down/degraded/misconfigured。
            # 严格消费者查这个，宽松消费者仍查 status == "ok"。
            "ok": upstream["ok"],
            "checked_at": upstream["checked_at"],
            "config": {
                "api_key_usable": key_ok,
                "api_key_note": key_note,
                # 只报"是否可用"与来源，**永不回显 key 本体**。
                "api_key_source": (
                    "环境变量 SILICONFLOW_API_KEY" if key_ok else "未生效"
                ),
            },
            # 对外传输策略回显：改动/升级后核对这些字段即知强制 H1 是否在生效
            # （对标 tools/DSH-升级铁律.md 里"必须核对 force-h1 applied:true"那条）。
            "transport": {
                "http_version": "HTTP/1.1",
                "http2": OUTBOUND_HTTP2,
                "http1_forced": OUTBOUND_HTTP2 is False,
                "h2_available": _H2_AVAILABLE,
                "trust_env": OUTBOUND_TRUST_ENV,
                "limits": {
                    "max_connections": OUTBOUND_LIMITS.max_connections,
                    "max_keepalive_connections": OUTBOUND_LIMITS.max_keepalive_connections,
                    "keepalive_expiry": OUTBOUND_LIMITS.keepalive_expiry,
                },
                "read_timeout": REMOTE_HTTP_TIMEOUT,
            },
            # 本进程服务量：区分本地/远端命中，便于判断"到底走没走远端"
            "serving": await tracker.serving_snapshot(),
            # 令牌桶用量：embed 与 rerank 是**独立桶**，逐桶单列，不做跨桶相加。
            "buckets": {
                "embed": {"model": BUCKET_EMBED, **limiter_embed.snapshot()},
                "rerank": {"model": BUCKET_RERANK, **limiter_rerank.snapshot()},
            },
            "upstream": upstream,
        }

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        """
        OpenAI 兼容嵌入 API（stream 增强）。

        请求体:
        {
            "input": "文本" | ["文本1", "文本2"],
            "model": "BAAI/bge-large-zh-v1.5",
            "encoding_format": "float",
            "stream": false
        }
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        raw_model = body.get("model", "BAAI/bge-large-zh-v1.5")
        input_data = body.get("input", "")
        stream = body.get("stream", False)

        model_name, force_local = _resolve_model_name(raw_model)

        # 处理 input（单字符串或数组）
        if isinstance(input_data, str):
            texts = [input_data]
        elif isinstance(input_data, list):
            texts = [str(t) for t in input_data]
        else:
            raise HTTPException(status_code=400, detail="input 必须是字符串或字符串数组")

        if not texts:
            raise HTTPException(status_code=400, detail="input 不能为空")

        # 决定本地还是远端
        use_local = force_local or _should_use_local(model_name)

        # ★ 超窗处理（local/remote 两条路都适用）：
        #   reject → 直接 413；average → 展开成多块，算完再聚合；
        #   truncate → 截断（旧行为）。见顶部 EMBED_OVERFLOW_MODE 注释。
        prepared, mapping, max_len = _plan_overflow(texts, model_name)
        if len(prepared) != len(texts):
            logger.info("超窗展开：%d 条输入 → %d 条待嵌入", len(texts), len(prepared))

        # 计数（本地/远端分开，便于判断实际走了哪条路）
        await tracker.record_request(use_local)

        # streaming 模式：逐条吐，**不做聚合**（展开后的块会各自成行）
        if stream:
            return StreamingResponse(
                _stream_embeddings(prepared, model_name, use_local),
                media_type="application/x-ndjson",
                headers={
                    "X-Accel-Buffering": "no",
                },
            )

        # 同步模式
        try:
            if use_local:
                embeddings_list = await _embed_local(model_name, prepared)
            else:
                remote_model = _get_remote_model_name(model_name)
                if not remote_api_key:
                    raise HTTPException(
                        status_code=502,
                        detail="远端服务需要 SILICONFLOW_API_KEY，未配置",
                    )
                embeddings_list = await _embed_remote(remote_model, prepared)
        except RateLimiterBusy as exc:
            # 排队超时 → 503 busy（对齐 server_paddle.py 的项目约定：客户端跳过、下轮续跑）
            return JSONResponse(
                status_code=503,
                content={
                    "error": "busy, retry later",
                    "busy": True,
                    "waited_sec": round(exc.waited_s, 1),
                    "acquire_timeout_sec": exc.limit,
                    "detail": exc.detail,
                },
            )
        except ValueError as exc:
            # 含 RateLimiterOversize（ValueError 子类）：单笔超 TPM，明说要拆
            raise HTTPException(status_code=400, detail=str(exc))
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"远端服务错误: {exc.response.status_code} {exc.response.text[:200]}",
            )

        # ★ 聚合：average 模式下展开的块在此合并回「一条输入一个向量」。
        #   权重 = 各块字符数（长块信息多）；合并后重新 L2 归一化。
        if any(m is not None for m in mapping):
            merged: list[list[float]] = []
            p = 0
            for m in mapping:
                if m is None:
                    merged.append(embeddings_list[p])
                    p += 1
                else:
                    start, cnt, weights = m
                    merged.append(_weighted_mean(embeddings_list[start:start + cnt], weights))
                    p = start + cnt
            embeddings_list = merged

        return {
            "object": "list",
            "data": [
                {"object": "embedding", "index": i, "embedding": emb}
                for i, emb in enumerate(embeddings_list)
            ],
            "model": model_name,
            "usage": {
                "prompt_tokens": sum(len(t) for t in texts),
                "total_tokens": sum(len(t) for t in texts),
            },
        }

    @app.post("/v1/rerank")
    async def rerank(request: Request):
        """
        Cohere 风格 Rerank API（兼容硅基流动）。

        请求体:
        {
            "model": "BAAI/bge-reranker-v2-m3",
            "query": "...",
            "documents": ["doc1", "doc2"],
            "top_n": 3,
            "stream": false
        }
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        raw_model = body.get("model", "BAAI/bge-reranker-v2-m3")
        query = body.get("query", "")
        documents = body.get("documents", [])
        top_n = body.get("top_n", len(documents))

        model_name, force_local = _resolve_model_name(raw_model)

        if not query:
            raise HTTPException(status_code=400, detail="query 不能为空")
        if not documents:
            raise HTTPException(status_code=400, detail="documents 不能为空")

        use_local = force_local or _should_use_local(model_name)

        # 计数（本地/远端分开，便于判断实际走了哪条路）
        await tracker.record_request(use_local)

        try:
            if use_local:
                model = await manager.get_model(model_name)
                loop = asyncio.get_event_loop()

                # sentence-transformers 的 rerank 用法
                pairs = [[query, doc] for doc in documents]
                scores = await loop.run_in_executor(
                    None,
                    lambda: model.predict(pairs, show_progress_bar=False),
                )

                # 转为 float 列表
                score_list = scores.tolist() if hasattr(scores, "tolist") else list(scores)
            else:
                remote_model = _get_remote_model_name(model_name)
                if not remote_api_key:
                    raise HTTPException(
                        status_code=502,
                        detail="远端服务需要 SILICONFLOW_API_KEY，未配置",
                    )
                await limiter_rerank.acquire(len(query) + sum(len(d) for d in documents))
                # 远端 429（配额抖动）：退避重试（1s/2s/4s，最多 3 次）
                t0 = time.monotonic()
                resp = None
                try:
                    for attempt in range(3):
                        resp = await http_client.post(
                            "/rerank",
                            json={
                                "model": remote_model,
                                "query": query,
                                "documents": documents,
                                "top_n": top_n,
                            },
                            headers={"Authorization": f"Bearer {remote_api_key}"},
                        )
                        if resp.status_code != 429 or attempt == 2:
                            break
                        await asyncio.sleep(2**attempt)
                    resp.raise_for_status()
                except Exception as exc:
                    await tracker.record_failure(
                        BUCKET_RERANK,
                        "/rerank",
                        exc,
                        status=getattr(resp, "status_code", None),
                        ms=(time.monotonic() - t0) * 1000,
                        message=_describe_upstream_error(exc, resp),
                    )
                    raise
                await tracker.record_success(
                    BUCKET_RERANK, "/rerank", (time.monotonic() - t0) * 1000
                )
                data = resp.json()
                return data

        except RateLimiterBusy as exc:
            # 排队超时 → 503 busy（对齐 server_paddle.py 的项目约定：客户端跳过、下轮续跑）
            return JSONResponse(
                status_code=503,
                content={
                    "error": "busy, retry later",
                    "busy": True,
                    "waited_sec": round(exc.waited_s, 1),
                    "acquire_timeout_sec": exc.limit,
                    "detail": exc.detail,
                },
            )
        except ValueError as exc:
            # 含 RateLimiterOversize（ValueError 子类）：单笔超 TPM，明说要拆
            raise HTTPException(status_code=400, detail=str(exc))
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"远端服务错误: {exc.response.status_code} {exc.response.text[:200]}",
            )

        # 本地 rerank 结果
        indexed = list(enumerate(score_list))
        indexed.sort(key=lambda x: x[1], reverse=True)
        top = indexed[:top_n]

        return {
            "object": "list",
            "model": model_name,
            "results": [
                {
                    "index": idx,
                    "relevance_score": float(score),
                }
                for idx, score in top
            ],
            "usage": {
                "total_tokens": sum(len(query) + len(d) for d in documents),
            },
        }

    return app


# ── 启动入口 ──────────────────────────────────────

def run_server(port: int, local_first: bool) -> None:
    """
    在指定端口运行 BGE 服务。

    此函数在子进程中运行。
    """
    import uvicorn

    app = create_app(port=port, local_first=local_first)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
    )


def main() -> None:
    """
    同时启动两个 BGE 服务进程：
      - PORT_LOCAL_FIRST：本地优先
      - PORT_REMOTE_FIRST：远端优先（知识库搜索用）
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] server_bge: %(message)s",
    )

    logger.info("Starting BGE servers...")
    logger.info(f"  Port {PORT_LOCAL_FIRST}: 本地优先")
    logger.info(f"  Port {PORT_REMOTE_FIRST}: 远端优先 (知识库)")

    p1 = multiprocessing.Process(
        target=run_server,
        args=(PORT_LOCAL_FIRST, True),
        name=f"bge-{PORT_LOCAL_FIRST}-local",
    )
    p2 = multiprocessing.Process(
        target=run_server,
        args=(PORT_REMOTE_FIRST, False),
        name=f"bge-{PORT_REMOTE_FIRST}-remote",
    )

    p1.start()
    p2.start()

    logger.info(f"Both servers started. PID {PORT_LOCAL_FIRST}={p1.pid}, {PORT_REMOTE_FIRST}={p2.pid}")

    try:
        p1.join()
        p2.join()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        p1.terminate()
        p2.terminate()
        p1.join()
        p2.join()


if __name__ == "__main__":
    main()
