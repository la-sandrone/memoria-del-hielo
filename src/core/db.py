"""
文件名: db.py
摘要: MariaDB 无状态短连接（即弃）访问层。
      execute_sql() 每次新建连接、用完即焚；transaction() 每事务一连接。
      连接池/信号量/健康检查全部移除——连接存活期 = 一个请求，无烂连接可言。
      大包（巨书全文单行 >16MB）由服务端 max_allowed_packet 决定
      （mariadb 驱动握手时自动跟随服务端值，无客户端参数）。
依赖: mariadb>=1.1
      core.config (get_config)
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import mariadb

from .config import get_config

logger = logging.getLogger(__name__)


class Database:
    """MariaDB 无状态访问层（即弃连接）。"""

    def _connect(self) -> mariadb.Connection:
        cfg = get_config()
        db_cfg = cfg.get("database")
        return mariadb.connect(
            host=db_cfg["host"],
            port=db_cfg["port"],
            user=db_cfg["user"],
            password=cfg.database_password,
            database=db_cfg["name"],
        )

    def execute_sql(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        dictionary: bool = True,
    ) -> list[dict[str, Any]]:
        """统一 SQL 执行接口（即弃连接：用完即焚）。

        读操作 (SELECT/SHOW/DESC/EXPLAIN/WITH) → list[dict]；
        写操作 → []（cursor.rowcount/lastrowid 由 get_last_* 获取）。

        异常原样透传——不可修复的失败让异常上浮，不静默吞错。
        """
        conn = self._connect()
        try:
            with conn.cursor(dictionary=dictionary) as cursor:
                cursor.execute(sql, params)
                if self._is_query(sql):
                    return cursor.fetchall() or []
                global _last_rowcount, _last_insert_id
                _last_rowcount = cursor.rowcount
                _last_insert_id = cursor.lastrowid or 0
                conn.commit()
                return []
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """事务上下文：每事务一个连接；正常退出 commit，
        任何异常（含 KeyboardInterrupt，急停回滚）→ rollback 后原样上浮。
        """
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except mariadb.Error:
                pass  # 回滚失败不掩盖原始异常
            raise
        finally:
            conn.close()

    @staticmethod
    def _is_query(sql: str) -> bool:
        """粗略判断 SQL 是否为查询语句。"""
        stripped = sql.strip().upper()
        return stripped.startswith(("SELECT", "SHOW", "DESC", "EXPLAIN", "WITH"))


# ── 全局单例 ──────────────────────────────────────────────

_db: Database | None = None
_last_rowcount: int = 0
_last_insert_id: int = 0


def get_db() -> Database:
    """获取数据库单例。"""
    global _db
    if _db is None:
        _db = Database()
    return _db


def get_last_rowcount() -> int:
    """返回最近一次 execute_sql 的 rowcount（写操作影响行数）。"""
    return _last_rowcount


def get_last_insert_id() -> int:
    """返回最近一次 execute_sql 的 lastrowid（自增主键值）。"""
    return _last_insert_id
