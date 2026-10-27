"""
文件名: sqlbatch.py
摘要: 攒批装置——把 N 条同构 upsert 攒成**一条**多值 SQL 发出去。
      解决"每条一次连接"在长任务上耗尽 Windows 临时端口（WSAEADDRINUSE 10048）的问题。

      三条硬规矩：
      1. **双上限**：行数上限 max_rows + 语句字节硬上限 max_sql_bytes。
         预测"再加一行会不会超"——超了先 flush，再加。
      2. **单行即超硬上限 → 抛 SqlBatchTooLarge**，绝不截断、绝不静默丢弃。
      3. **每次 flush 一条连接**（走 Database.transaction()），异常自动 rollback。

      瞬时错误（10048/2002/2006/2013/1205/1213）指数退避重试；
      upsert 幂等，重试安全。

依赖: core.db (get_db, Database)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Iterable, Sequence

import mariadb

from .db import Database, get_db

logger = logging.getLogger(__name__)

# ────────────────────────── 默认上限 ──────────────────────────
# 字节数是"整条 SQL + 全部参数"的估算值。MariaDB 服务端 max_allowed_packet
# 默认 16 MB，取 4 MB 留足余量（大包只会在 flush 时一次性出现）。
DEFAULT_MAX_SQL_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_ROWS = 256
# 保守的每行固定开销：括号、逗号、占位符、协议头
_PER_ROW_OVERHEAD = 64
# 文本参数按 1.05 倍估算（客户端转义最多把 ' 和 \ 各翻倍；
# 嵌入向量是纯数字 JSON，实际几乎无膨胀，这里只作保险）
_TEXT_INFLATE = 1.05

# 可重试的错误码：连接层瞬时故障 + 死锁/锁等待
RETRY_ERRNOS = {10048, 2002, 2006, 2013, 1205, 1213}
# 包太大：不能盲重试，必须砍上限 + 拆批
PACKET_TOO_LARGE_ERRNO = 1153
# 服务端上限只用 75%，留余量给协议头与转义
SERVER_LIMIT_HEADROOM = 0.75
# 砍上限的最低值：低于"单行 × 2"就说明拆不动了
MIN_CAP_FLOOR = 64 * 1024


class _PacketTooLarge(Exception):
    """内部信号：服务端报 1153，交给 flush 拆批。"""


class SqlBatchTooLarge(RuntimeError):
    """单行数据本身就超过语句硬上限——攒批救不了，必须显式失败。"""


class SqlBatcher:
    """把同构的行攒成一条多值 INSERT 发送。

    用法::

        batcher = SqlBatcher(
            table="embedding_cache",
            row_template="(?, ?, ?, NOW(), NULL)",
            tail="ON DUPLICATE KEY UPDATE vector_json = VALUES(vector_json)",
        )
        batcher.add((hash1, "bge-m3", json1))
        batcher.add_many(rows)
        batcher.flush()          # 或用 with batcher: ... 自动 flush

    参数:
        table: 目标表名。
        row_template: 单行占位符模板，如 "(?, ?, ?, NOW())"。
        tail: VALUES 之后的部分（ON DUPLICATE KEY UPDATE / 空串）。
        head: 可选，默认自动拼 "INSERT INTO {table} VALUES "。
        max_rows: 行数上限。
        max_sql_bytes: **语句硬上限**（估算字节）。
        retries: flush 失败重试次数。
        db: 可注入；默认 get_db() 单例。
    """

    def __init__(
        self,
        *,
        table: str,
        row_template: str,
        tail: str = "",
        head: str | None = None,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_sql_bytes: int = DEFAULT_MAX_SQL_BYTES,
        retries: int = 5,
        db: Database | None = None,
        probe_limit: bool = True,
    ) -> None:
        if max_sql_bytes <= 0 or max_rows <= 0:
            raise ValueError("max_rows / max_sql_bytes 必须为正")
        self.table = table
        self.row_template = row_template
        self.tail = tail
        self.head = head if head is not None else f"INSERT INTO {table} VALUES "
        self.max_rows = max_rows
        self.max_sql_bytes = max_sql_bytes
        self.retries = retries
        self._db = db
        self.probe_limit = probe_limit
        self._probed = False
        self.server_packet_limit: int | None = None

        self._n_params = row_template.count("?")
        self._base_bytes = len((self.head + self.tail).encode("utf-8"))
        self._template_bytes = len(row_template.encode("utf-8"))

        self._rows: list[tuple[Any, ...]] = []
        self._param_bytes = 0
        # RLock：add() 持锁时可能触发 flush()，同一线程需可重入
        self._lock = threading.RLock()

        # 统计（可观测用）
        self.stat_rows = 0
        self.stat_flushes = 0
        self.stat_max_bytes = 0
        self.stat_splits = 0   # 因包太大被拆批的次数

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = get_db()
        return self._db

    @staticmethod
    def _param_len(value: Any) -> int:
        if value is None:
            return 4
        if isinstance(value, str):
            return int(len(value.encode("utf-8")) * _TEXT_INFLATE)
        if isinstance(value, (bytes, bytearray)):
            return int(len(value) * 1.1)
        return 8  # 数字/布尔按 8 字节估

    def _estimate(self, extra_rows: int, extra_param_bytes: int = 0) -> int:
        """估算再加 extra_rows 行后的语句字节数。"""
        rows = len(self._rows) + extra_rows
        return (
            self._base_bytes
            + rows * (self._template_bytes + _PER_ROW_OVERHEAD)
            + int((self._param_bytes + extra_param_bytes) * _TEXT_INFLATE)
        )

    def _build(self, rows: Sequence[tuple[Any, ...]]) -> tuple[str, list[Any]]:
        sql = self.head + ",".join([self.row_template] * len(rows)) + self.tail
        params: list[Any] = []
        for row in rows:
            params.extend(row)
        return sql, params

    def _probe(self) -> None:
        """向服务端问一次 max_allowed_packet，把硬上限压到它的 75% 以内。

        只问一次；失败就沿用配置上限（探测失败不该阻断导入）。
        注意：即弃连接每次都拿到最新的 session 值——这是它比长连接强的地方。
        """
        if self._probed or not self.probe_limit:
            return
        self._probed = True
        try:
            rows = self.db.execute_sql("SELECT @@session.max_allowed_packet AS lim")
            limit = int(rows[0]["lim"]) if rows else 0
        except Exception as exc:                     # 探测失败不阻断主流程
            logger.warning("%s: 探测 max_allowed_packet 失败（沿用 %d）：%s",
                           self.table, self.max_sql_bytes, exc)
            return
        if limit <= 0:
            return
        self.server_packet_limit = limit
        capped = int(limit * SERVER_LIMIT_HEADROOM)
        if capped < self.max_sql_bytes:
            logger.info("%s: 服务端 max_allowed_packet=%d，硬上限 %d → %d",
                        self.table, limit, self.max_sql_bytes, capped)
            self.max_sql_bytes = max(MIN_CAP_FLOOR, capped)

    def _execute_with_retry(self, sql: str, params: Sequence[Any], n_rows: int) -> None:
        delay = 0.5
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                with self.db.transaction() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(sql, params)
                return
            except mariadb.Error as exc:
                errno = getattr(exc, "errno", None)
                last_exc = exc
                if errno == PACKET_TOO_LARGE_ERRNO:
                    raise _PacketTooLarge(str(exc)) from exc   # 交给上层拆批
                if errno not in RETRY_ERRNOS or attempt == self.retries:
                    break
                logger.warning(
                    "攒批写入失败（第 %d/%d 次，errno=%s: %s），%.1fs 后重试：%d 行",
                    attempt, self.retries, errno, exc, delay, n_rows,
                )
                time.sleep(delay)
                delay *= 2
        raise RuntimeError(
            f"攒批写入失败（{n_rows} 行，已重试 {self.retries} 次）：{last_exc}"
        ) from last_exc

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------
    def add(self, params: tuple[Any, ...] | list[Any]) -> None:
        """加入一行。会自动判断是否需要先 flush。"""
        if self._n_params and len(params) != self._n_params:
            raise ValueError(
                f"{self.table}: 参数个数 {len(params)} != 模板占位符 {self._n_params}"
            )
        pbytes = sum(self._param_len(p) for p in params)
        with self._lock:
            if self._estimate(1, pbytes) > self.max_sql_bytes:
                if not self._rows:
                    raise SqlBatchTooLarge(
                        f"{self.table}: 单行即超语句硬上限"
                        f"（估算 {self._estimate(1, pbytes)} > {self.max_sql_bytes} 字节）。"
                        "调大 max_sql_bytes，或把该列拆分/压缩后再入库。"
                    )
                self.flush()  # RLock 允许同一线程重入
                if self._estimate(1, pbytes) > self.max_sql_bytes:
                    raise SqlBatchTooLarge(
                        f"{self.table}: 单行即超语句硬上限（flush 后仍超）"
                    )
            self._rows.append(tuple(params))
            self._param_bytes += pbytes
            if len(self._rows) >= self.max_rows:
                self.flush()

    def add_many(self, rows: Iterable[Sequence[Any]]) -> None:
        """批量加入（逐行走同一套上限判断）。"""
        for row in rows:
            self.add(tuple(row))

    def flush(self) -> int:
        """把攒的全部行一次发出；返回写出的行数。

        遇到 1153（包太大）→ 硬上限砍半 → 拆成两批重发（递归最多砍到下限）。
        """
        with self._lock:
            if not self._rows:
                return 0
            rows = self._rows
            self._rows = []
            self._param_bytes = 0
        return self._send_with_split(rows)

    def _send_with_split(self, rows: list[tuple[Any, ...]]) -> int:
        """发送一批；包太大就砍上限、二分重发。行数不可丢失。"""
        self._probe()
        sql, params = self._build(rows)
        nbytes = len(sql.encode("utf-8")) + sum(self._param_len(p) for p in params)
        self.stat_max_bytes = max(self.stat_max_bytes, nbytes)
        try:
            self._execute_with_retry(sql, params, len(rows))
        except _PacketTooLarge as exc:
            if len(rows) == 1:
                raise SqlBatchTooLarge(
                    f"{self.table}: 单行即超服务端 max_allowed_packet"
                    f"（服务端 {self.server_packet_limit or '?'} 字节）：{exc}"
                ) from exc
            newcap = max(MIN_CAP_FLOOR, self.max_sql_bytes // 2)
            self.max_sql_bytes = newcap
            self.stat_splits += 1
            mid = len(rows) // 2
            logger.warning(
                "%s: 包太大（%d 行 / %d 字节）→ 上限砍到 %d，拆成 %d + %d 重发",
                self.table, len(rows), nbytes, newcap, mid, len(rows) - mid,
            )
            return self._send_with_split(rows[:mid]) + self._send_with_split(rows[mid:])
        self.stat_rows += len(rows)
        self.stat_flushes += 1
        logger.debug("%s: flush %d 行 / %d 字节（累计 %d 条语句）",
                     self.table, len(rows), nbytes, self.stat_flushes)
        return len(rows)

    @property
    def pending(self) -> int:
        return len(self._rows)

    def __enter__(self) -> "SqlBatcher":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.flush()
        else:
            # 上层已出错：丢弃未发出的一批（缓存类数据丢了顶多重算，
            # 但不掩盖原始异常）
            lost = len(self._rows)
            if lost:
                logger.warning("%s: 上层异常，丢弃未写出的 %d 行", self.table, lost)
                with self._lock:
                    self._rows = []
                    self._param_bytes = 0
        return False
