#!/usr/bin/env python3

"""
文件名: server_bge.py
摘要: BGE 嵌入代理 — 常驻嵌入服务，双端口架构（PORT_LOCAL_FIRST 本地优先 / PORT_REMOTE_FIRST 远端优先）。
      五模型 CPU/GPU 懒加载，硅基流动远端接入，streaming ndjson。
依赖: fastapi, uvicorn, sentence-transformers, torch
"""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import os
import time
from contextlib import asynccontextmanager
from typing import Optional, AsyncGenerator

import httpx
import torch
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field


# ── 远端 API 配额（硅基流动；按模型分开，换后端只改这里）──
REMOTE_QUOTAS: dict[str, dict[str, int]] = {
    "BAAI/bge-m3": {"rpm": 2000, "tpm": 500000},
    "BAAI/bge-reranker-v2-m3": {"rpm": 2000, "tpm": 500000},
}
from sentence_transformers import SentenceTransformer

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
        "is_reranker": True,
    },
}

# 懒加载配置
MIN_TTL_SECONDS = 600  # 10 分钟
CPU_THREADS = max(1, os.cpu_count() or 4) // 2

# 硅基流动端点
SILICONFLOW_BASE = "https://api.siliconflow.cn/v1"

# ── 本地缓存检测 ───────────────────────────────────


def _find_modelscope_cache(model_name: str) -> str | None:
    """
    检查 ModelScope 缓存中是否有该模型的预下载版本。

    ModelScope 缓存结构为 ~/.cache/modelscope/models/{org}/{model_name}/
    模型文件直接放在该目录下（pytorch_model.bin、modules.json 等），
    没有 hub/ 或 snapshots/ 中间层。

    `download_models.py` 通过 snapshot_download() 预下载到该缓存。
    返回模型目录路径（SentenceTransformer 可直接加载），未命中返回 None。
    """
    cache_root = os.path.expanduser("~/.cache/modelscope")
    model_dir = os.path.join(cache_root, "models", model_name)
    if not os.path.isdir(model_dir):
        return None
    # 验证目录包含有效模型文件
    if not os.path.isfile(os.path.join(model_dir, "modules.json")):
        return None
    return model_dir


# ── 日志 ──────────────────────────────────────────

logger = logging.getLogger("server_bge")


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

    async def get_model(self, model_name: str) -> SentenceTransformer:
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

            # 先检查 ModelScope 本地缓存
            local_path = _find_modelscope_cache(model_name)
            if local_path:
                logger.info("  Found in ModelScope cache: %s", local_path)
                model_source = local_path
            else:
                logger.info("  Not in local cache, downloading from HuggingFace ...")
                model_source = model_name

            # 异步加载（在 executor 中运行以避免阻塞事件循环）
            loop = asyncio.get_event_loop()
            model = await loop.run_in_executor(
                None,
                lambda: SentenceTransformer(
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

    async def acquire(self, tokens: int = 0) -> None:
        """按到达顺序等待放行。tokens = 本次请求预估消耗 token 数（TPM 用）。"""
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
                    return
                await asyncio.sleep(0.05)


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
    http_client = httpx.AsyncClient(
        base_url=SILICONFLOW_BASE,
        timeout=REMOTE_HTTP_TIMEOUT,
    )

    mode_label = "本地优先" if local_first else "远端优先"
    # 远端 RPM/TPM 限流（配额见顶部 REMOTE_QUOTAS，按模型分开）：
    # 所有远端请求按到达顺序串行转发（FIFO），并受令牌桶约束。
    limiter_embed = RateLimiter(
        rpm=REMOTE_QUOTAS["BAAI/bge-m3"]["rpm"],
        tpm=REMOTE_QUOTAS["BAAI/bge-m3"]["tpm"],
    )
    limiter_rerank = RateLimiter(
        rpm=REMOTE_QUOTAS["BAAI/bge-reranker-v2-m3"]["rpm"],
        tpm=REMOTE_QUOTAS["BAAI/bge-reranker-v2-m3"]["tpm"],
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """启动时和关闭时的生命周期管理。"""
        # 启动后台 TTL 检查任务
        task = asyncio.create_task(_ttl_loop())
        logger.info("server_bge [%s] started on port %d", mode_label, port)
        yield
        task.cancel()
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
        """硅基流动远端嵌入。"""
        # 远端限流排队（按到达顺序；tokens = 字符数估算，中文 ~1 token/字）
        await limiter_embed.acquire(sum(len(t) for t in texts))

        # 远端 429（配额抖动）：退避重试（1s/2s/4s，最多 3 次），仍失败才上抛
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
        """逐条输出 embedding（ndjson over chunked）。"""
        total = len(texts)
        for i, text in enumerate(texts):
            if use_local:
                emb = await _embed_local(model_name, [text])
            else:
                emb = await _embed_remote(model_name, [text])
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
        """健康检查。"""
        return {
            "status": "ok",
            "mode": mode_label,
            "port": port,
            "loaded_models": {
                name: f"{ttl:.0f}s remaining"
                for name, ttl in manager.get_loaded_models().items()
            },
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

        # streaming 模式
        if stream:
            return StreamingResponse(
                _stream_embeddings(texts, model_name, use_local),
                media_type="application/x-ndjson",
                headers={
                    "X-Accel-Buffering": "no",
                },
            )

        # 同步模式
        try:
            if use_local:
                embeddings_list = await _embed_local(model_name, texts)
            else:
                remote_model = _get_remote_model_name(model_name)
                if not remote_api_key:
                    raise HTTPException(
                        status_code=502,
                        detail="远端服务需要 SILICONFLOW_API_KEY，未配置",
                    )
                embeddings_list = await _embed_remote(remote_model, texts)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"远端服务错误: {exc.response.status_code} {exc.response.text[:200]}",
            )

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
                data = resp.json()
                return data

        except ValueError as exc:
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
                "total_tokens": sum(len(q) + len(d) for d in documents),
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
