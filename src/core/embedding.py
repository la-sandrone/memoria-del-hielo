"""
文件名: embedding.py
摘要: 嵌入向量服务 + 缓存层（MariaDB，即弃连接）。
      调用 server_bge.py (HTTP API) 获取 BGE-M3 1024维嵌入和重排分数。
      缓存表 embedding_cache 主键 (text_hash, model)，TTL 由
      [cache] ttl_days 控制（0 = 永不过期）。
      铁律：绝不长连接——每次查询/写入新建连接，用完即焚。
依赖: httpx>=0.27
      core.config (get_config)
      core.db (get_db, get_last_rowcount)
      core.http_client (local_client —— trust_env=False，免疫 NO_PROXY 脏值)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

from .config import get_config
from .db import get_db, get_last_rowcount
from .http_client import local_client


class EmbeddingError(RuntimeError):
    """嵌入服务调用失败。"""


class EmbeddingService:
    """嵌入 + 重排服务，含本地缓存层。"""

    def __init__(self) -> None:
        cfg = get_config()
        e = cfg.get("embedding", {})
        self.local_emb: str = e.get("local_endpoint", "http://127.0.0.1:9005/v1/embeddings")
        self.remote_emb: str = e.get("remote_endpoint", "http://127.0.0.1:9006/v1/embeddings")
        self.local_rerank: str = e.get("rerank_local", "http://127.0.0.1:9005/v1/rerank")
        self.remote_rerank: str = e.get("rerank_remote", "http://127.0.0.1:9006/v1/rerank")
        self.model: str = e.get("model", "BAAI/bge-m3")
        self.rerank_model: str = e.get(
            "rerank_model", "remote:BAAI/bge-reranker-v2-m3"
        )
        self.dimension: int = int(e.get("dimension", 1024))

        cache_dir_name: str = cfg.get("cache.dir", "cache")
        # 相对于项目根或绝对路径
        cache_path = Path(cache_dir_name)
        if not cache_path.is_absolute():
            # 相对于配置文件所在目录
            cache_path = cfg.config_path.parent / cache_dir_name
        self.cache_dir: Path = cache_path.resolve()
        # 缓存有效期（天）；0 = 永不过期（expires_at 存 NULL）
        self.ttl_days: int = int(cfg.get("cache.ttl_days", 180))

        self._http = local_client(60.0)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def embed(
        self,
        texts: list[str],
        endpoint_hint: str = "",
    ) -> list[list[float]]:
        """获取一批文本的嵌入向量。

        参数:
            texts: 文本列表。
            endpoint_hint: "local" 强制本地，"remote" 强制远端，
                           "" 使用配置默认（本地优先）。

        返回:
            embeddings: list[list[float]]，每个文本对应一个 1024维向量。
        """
        if not texts:
            return []

        endpoint = self._resolve_endpoint(
            self.local_emb, self.remote_emb, self._effective_hint(endpoint_hint)
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "input": texts,
        }

        try:
            resp = self._http.post(endpoint, json=payload)
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"嵌入请求失败 ({endpoint}): {exc}") from exc
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise EmbeddingError(f"嵌入响应解析失败: {exc}") from exc

        # OpenAI Embeddings 格式
        data = body.get("data", [])
        data.sort(key=lambda x: x.get("index", 0))
        return [item["embedding"] for item in data]

    def embed_one(self, text: str) -> list[float]:
        """嵌入单条文本（带缓存）。"""
        cached = self._cache_get(text)
        if cached is not None:
            return cached

        vectors = self.embed([text])
        if not vectors:
            raise EmbeddingError("嵌入返回空结果")
        vector = vectors[0]

        self._cache_set(text, vector)
        return vector

    def embed_batch(
        self,
        texts: list[str],
        max_batch: int = 32,
        endpoint_hint: str = "",
        progress: Callable[[int, int], None] | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> list[list[float]]:
        """批量嵌入（逐条缓存 + 分批 HTTP 请求）。

        与逐条调 embed_one 语义一致，但未命中的文本按 max_batch
        合并出网——一本书 ~100 chunk 只需 3-4 次往返；
        断点重跑时缓存全命中，零网络请求。

        参数:
            texts: 文本列表（如一本书的全部 chunk）。
            max_batch: 每次 HTTP 请求携带的最大条数。
            endpoint_hint: "local" / "remote" / ""。
            progress: 可选回调 progress(done, total)，每批完成后调用。
            stop_check: 可选中断检查回调——每批前调用，返回 True 则
                raise KeyboardInterrupt（并行导入第一次 Ctrl+C 时快速
                中止当前书，不等剩余批次嵌入完成）。
        """
        if not texts:
            return []

        vectors: list[list[float] | None] = [None] * len(texts)
        miss_idx: list[int] = []
        for i, text in enumerate(texts):
            cached = self._cache_get(text)
            if cached is not None:
                vectors[i] = cached
            else:
                miss_idx.append(i)

        for start in range(0, len(miss_idx), max_batch):
            if stop_check is not None and stop_check():
                # 并行导入第一次 Ctrl+C：当前书快速中止（嵌入阶段逐批检查）
                raise KeyboardInterrupt
            batch_idx = miss_idx[start : start + max_batch]
            fetched = self.embed([texts[i] for i in batch_idx], endpoint_hint)
            if len(fetched) != len(batch_idx):
                raise EmbeddingError(
                    f"批量嵌入返回条数不匹配: {len(fetched)} != {len(batch_idx)}"
                )
            for k, i in enumerate(batch_idx):
                vectors[i] = fetched[k]
                self._cache_set(texts[i], fetched[k])
            if progress:
                progress(min(start + max_batch, len(miss_idx)), len(miss_idx))

        if any(v is None for v in vectors):
            raise EmbeddingError("embed_batch 内部错误: 存在未填充的向量槽位")
        return vectors  # type: ignore[return-value]

    def rerank(
        self,
        query: str,
        documents: list[str],
        endpoint_hint: str = "",
    ) -> list[float]:
        """对文档列表进行重排，返回与 query 的相关性分数。

        参数:
            query: 查询文本。
            documents: 候选文档列表。
            endpoint_hint: "local" / "remote" / ""（默认远端优先）。

        返回:
            scores: list[float]，与 documents 一一对应。
        """
        if not documents:
            return []

        endpoint = self._resolve_endpoint(
            self.local_rerank, self.remote_rerank, self._effective_hint(endpoint_hint)
        )
        payload: dict[str, Any] = {
            "model": self.rerank_model,
            "query": query,
            "documents": documents,
            "top_n": len(documents),
        }

        try:
            resp = self._http.post(endpoint, json=payload)
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"重排请求失败 ({endpoint}): {exc}") from exc
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise EmbeddingError(f"重排响应解析失败: {exc}") from exc

        # Cohere Rerank 格式
        results = body.get("results", [])
        scores = [0.0] * len(documents)
        for item in results:
            idx = item.get("index")
            if idx is not None and 0 <= idx < len(scores):
                scores[idx] = item.get("relevance_score", 0.0)
        return scores

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------

    def _text_md5(self, text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def _cache_get(self, text: str) -> list[float] | None:
        """查缓存：MariaDB 主键查询（即弃连接，无长连接）。

        TTL 语义：expires_at IS NULL = 永不过期；
        expires_at < NOW() = 已过期（视为未命中，下次写入覆盖）。
        """
        text_hash = self._text_md5(text)
        rows = get_db().execute_sql(
            "SELECT vector_json FROM embedding_cache "
            "WHERE text_hash = ? AND model = ? "
            "AND (expires_at IS NULL OR expires_at > NOW())",
            (text_hash, self.model),
        )
        if not rows:
            return None
        try:
            return list(json.loads(rows[0]["vector_json"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            # 坏数据不阻断主线流程——下次写入覆盖
            return None

    def _cache_set(self, text: str, vector: list[float]) -> None:
        """写缓存：upsert 到 MariaDB（即弃连接，无长连接）。"""
        text_hash = self._text_md5(text)
        payload = json.dumps(vector, ensure_ascii=False)
        db = get_db()
        if self.ttl_days and self.ttl_days > 0:
            db.execute_sql(
                "INSERT INTO embedding_cache "
                "(text_hash, model, vector_json, created_at, expires_at) "
                "VALUES (?, ?, ?, NOW(), DATE_ADD(NOW(), INTERVAL ? DAY)) "
                "ON DUPLICATE KEY UPDATE vector_json = VALUES(vector_json), "
                "expires_at = VALUES(expires_at)",
                (text_hash, self.model, payload, self.ttl_days),
            )
        else:
            db.execute_sql(
                "INSERT INTO embedding_cache "
                "(text_hash, model, vector_json, created_at, expires_at) "
                "VALUES (?, ?, ?, NOW(), NULL) "
                "ON DUPLICATE KEY UPDATE vector_json = VALUES(vector_json), "
                "expires_at = NULL",
                (text_hash, self.model, payload),
            )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _effective_hint(self, endpoint_hint: str) -> str:
        """解析端点选择 hint：显式传参优先，否则按 model 字段前缀。

        model = "remote:BAAI/bge-m3" → 默认远端（9006）
        model = "local:..."        → 默认本地（9005）
        """
        if endpoint_hint:
            return endpoint_hint
        if self.model.startswith("remote:"):
            return "remote"
        if self.model.startswith("local:"):
            return "local"
        return ""

    @staticmethod
    def _resolve_endpoint(
        primary: str, secondary: str, hint: str
    ) -> str:
        """解析端点选择逻辑。

        hint="local" → primary（本地端）
        hint="remote" → secondary（远端）
        hint="" → primary（配置中第一个，即本地端）
        """
        if hint == "local":
            return primary
        if hint == "remote":
            return secondary
        return primary


# ── 缓存维护 ──────────────────────────────────────────────


def cleanup_expired_cache() -> int:
    """清理过期嵌入缓存条目（expires_at < NOW()）。

    调用时机：导入胜利退出时自动清理 + CLI 手动清理（mhi-cleanup-emb-cache）。
    返回删除行数。即弃连接，无长连接。
    """
    get_db().execute_sql(
        "DELETE FROM embedding_cache WHERE expires_at IS NOT NULL AND expires_at < NOW()"
    )
    return get_last_rowcount()


# ── 全局单例 ──────────────────────────────────────────────

_service: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    global _service
    if _service is None:
        _service = EmbeddingService()
    return _service
