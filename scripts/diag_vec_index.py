#!/usr/bin/env python3
"""诊断：向量索引重建进度观测（只读；Windows / WSL 均可跑）。

背景
    重建 documents_book_chunk 的向量索引（DROP + ADD VECTOR INDEX）是单线程建图的长任务
    （161,627 节点 × 1024 维，M=16 ≈ 10 分钟量级），MariaDB **不提供百分比进度**。
    本脚本用两路信号合成进度：
      · 数据库侧：ALTER 线程是否在跑 + 向量索引是否存在 → 判阶段
      · 文件侧：datadir 下 documents_book_chunk 的 #i#*.ibd（向量索引文件）体积增长 → 算进度/ETA

用法（Windows 侧）
    python scripts\\diag_vec_index.py                 常驻观测，Ctrl+C 退出
                                                      ★ 请在跑 ALTER 之前启动（见下）
    python scripts\\diag_vec_index.py --once           只打一次快照（跑 ALTER 前确认现状）
    python scripts\\diag_vec_index.py --interval 15    采样间隔秒（默认 30）
    python scripts\\diag_vec_index.py --target-mb 476  手动指定目标体积

重建命令（MariaDB 12.3.2 实测语法，别写错）
    ALTER TABLE documents_book_chunk DROP INDEX idx_vec_chunk;        ← 没有 DROP VECTOR INDEX 这种写法（语法错）
    ALTER TABLE documents_book_chunk ADD VECTOR INDEX idx_vec_chunk (embedding) M=16 DISTANCE=cosine;
      ⚠️ M/DISTANCE 必须显式写全：省略会退回默认「M=6 + euclidean」，与 cosine 查询不匹配
         → 索引会静默不被使用（索引与查询的距离度量必须一致）
    · 长 ALTER 请用命令行客户端跑，不要用 phpMyAdmin：PMA 默认 ExecTimeLimit=300s 会在中途报错，
      而服务端 ALTER 仍继续执行——容易出现「以为失败→重复执行」的误判

约定与边界
    · 目标体积默认取「启动时读到的当前索引文件体积」= 重建前的真实大小，故请在 DROP 之前启动；
      若启动太晚（索引已被删），用 --target-mb 指定（161,627 行 × 1024 维 ≈ 476 MB）。
    · 完成判定：先见到「索引缺失 + ALTER 在跑」，再见到「索引存在 + 无 ALTER」。
      启动太晚时只报快照，不误报完成。
    · ⚠️ 文件侧在本版本（MariaDB 12.3.2）**重建期间不可见**：DROP 后 #i#NN 被删、
      ADD 期间写临时文件、完成后才以 #i#NN 落盘 → 全程体积显示 0/- 是正常的，不是卡住。
      进度与完成判定实际由 DB 侧信号承担（实测有效）。
    · 全程只读：查询限于 @@datadir / information_schema（不碰 documents_book_chunk，
      避免 ALTER 持 MDL 期间被挂住）；每条查询带 read_timeout。
    · 输出重定向到文件时加 -X utf8（PEP 540）；真控制台直接跑即可（PEP 528 直写 Unicode）。

重建实测记录（2026-09-21，161,627 chunk × 1024 维，M=16 cosine）
    · 耗时 11 分 30 秒（官方 benchmark 外推的「10 分钟量级」得到验证）
    · 索引文件 476.0 MB → 444.0 MB（-6.7%：7.3 万删除行留下的死节点被回收）
    · 候地产出上限 62 行 → 1000 行（此前把 ef_search 拉到 10000 仍是 62）
      → 坐实「62 行上限 = 死节点耗尽探索预算」，与 ef_search / M 无关
    · 召回：索引 top-50 vs 暴力 top-50 = 50/50；ANN top-60 = 0.002s

依赖: 标准库 + mariadb（Windows 侧 Python312 已有）；连接参数读项目 config.toml（单一真相源）。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # 项目根（Windows / WSL 通吃，不硬编码盘符）
sys.path.insert(0, str(ROOT))

import mariadb  # noqa: E402  （Windows 侧已装；本脚本不复用 core.db，见下）

from src.core.config import get_config  # noqa: E402

CHUNK_PREFIX = "documents_book_chunk"
VEC_INDEX = "idx_vec_chunk"
# 进度行宽度随终端自适应（窄控制台换行会把进度条刷成滚屏）
WIDTH = max(60, shutil.get_terminal_size((120, 25)).columns - 1)
# 说明：这里直接 mariadb.connect 而不复用 core.db.get_db()——观测脚本要给单条查询设
# read_timeout，避免 ALTER 持 MDL 时把观测卡死；连接参数仍读 config.toml。

PHASE_IDLE = "就绪（索引存在，无 ALTER 在跑）"
PHASE_BUILDING = "建图中（ADD VECTOR INDEX 运行，单线程）"
PHASE_DROPPED = "索引缺失且无 ALTER（DROP 后未 ADD，或 ADD 失败）"
PHASE_FINISHING = "ALTER 收尾中"
PHASE_LATE = "未观测到重建过程（可能已结束）"


def connect():
    cfg = get_config()
    return mariadb.connect(
        host=cfg.get("database.host"),
        port=int(cfg.get("database.port")),
        user=cfg.get("database.user"),
        password=cfg.database_password,
        database=cfg.get("database.name"),
        connect_timeout=5,
        read_timeout=10,
    )


def query(sql: str, params=None):
    """单次即弃连接查询（观测用；失败返回 None，不打断循环）。"""
    try:
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params or ())
            return cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:
        print("\n  [观测告警] %s" % str(exc)[:100])
        return None


def _as_path(raw: str) -> Path:
    """Windows 侧原样用；WSL 侧把 D:\\... 折成 /mnt/<drive>/...（同一 datadir 的两种视图）。"""
    if os.name == "nt":
        return Path(raw)
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
    if m:
        return Path("/mnt/%s/%s" % (m.group(1).lower(), m.group(2).replace("\\", "/")))
    return Path(raw)


def datadir() -> Path | None:
    rows = query("SELECT @@datadir AS d")
    if not rows:
        return None
    return _as_path(str(rows[0][0]))


def dbdir() -> Path | None:
    """每个库的 .ibd 在 <datadir>/<database> 下（不是 datadir 根）。"""
    dd = datadir()
    if dd is None:
        return None
    return dd / str(get_config().get("database.name"))


def index_exists() -> bool | None:
    """用 information_schema.STATISTICS（不打开表 → 不需要 MDL，ALTER 期间也能查）。"""
    rows = query(
        "SELECT COUNT(*) AS n FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ? AND INDEX_NAME = ?",
        (CHUNK_PREFIX, VEC_INDEX),
    )
    if rows is None:
        return None
    return int(rows[0][0]) > 0


def altering() -> tuple[int, str]:
    """正在跑的 ALTER ... VECTOR INDEX 线程（秒数, 状态）。"""
    rows = query(
        "SELECT TIME, COALESCE(STATE, '') AS st FROM information_schema.PROCESSLIST "
        "WHERE INFO LIKE 'ALTER TABLE%%VECTOR INDEX%%' AND COMMAND <> 'Sleep'"
    )
    if not rows:
        return 0, ""
    return max(int(r[0]) for r in rows), str(rows[0][1])[:40]


def index_files(base: Path) -> list[tuple[Path, int, float]]:
    """chunk 表的 #i#*.ibd（向量索引文件），按 mtime 倒序。"""
    out: list[tuple[Path, int, float]] = []
    try:
        for p in base.glob("%s*#i#*.ibd" % CHUNK_PREFIX):
            st = p.stat()
            out.append((p, st.st_size, st.st_mtime))
    except OSError:
        pass
    return sorted(out, key=lambda x: -x[2])


def bar(pct: float, width: int = 28) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(width * pct / 100.0)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def hhmmss(sec: float) -> str:
    sec = max(0, int(sec))
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def snapshot() -> dict:
    base = dbdir()
    return {
        "datadir": datadir(),
        "dbdir": base,
        "exists": index_exists(),
        "alter": altering(),
        "files": index_files(base) if base else [],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="向量索引重建进度观测（只读）")
    ap.add_argument("--once", action="store_true", help="只打一次快照后退出")
    ap.add_argument("--interval", type=float, default=30.0, help="采样间隔秒（默认 30）")
    ap.add_argument("--target-mb", type=float, default=None, help="目标体积 MB（默认取启动时快照）")
    args = ap.parse_args()

    print("== 向量索引重建进度观测 ==")
    snap = snapshot()
    base = snap["dbdir"]
    if base is None:
        print("✗ 连不上数据库或读不到 @@datadir——检查 config.toml / MariaDB 是否在跑")
        return 2
    files = snap["files"]
    cur_bytes = files[0][1] if files else 0
    target = (args.target_mb * 1024 * 1024) if args.target_mb else float(cur_bytes)

    print("   datadir   : %s" % snap["datadir"])
    print("   索引文件  : %s" % (
        "、".join("%s %.1fMB" % (p.name, sz / 1048576.0) for p, sz, _ in files) or "(未发现 #i#*.ibd)"))
    print("   索引存在  : %s" % ("是" if snap["exists"] else "否"))
    print("   ALTER 在跑: %s" % (("是，已 %ds %s" % (snap["alter"][0], snap["alter"][1])) if snap["alter"][0] else "否"))
    print("   目标体积  : %s\n" % ("%.1f MB（启动时快照）" % (target / 1048576.0) if target else "(未知，请用 --target-mb 指定)"))
    if not files or snap["exists"] is False:
        print("   ⚠️ 文件侧当前不可见——本版本 ADD VECTOR INDEX 期间索引用临时文件，")
        print("      完成后才落成 #i#NN.ibd（DROP 后旧文件已删）。所以体积长期显示 0/- 是正常的，")
        print("      不是卡住；进度与完成判定以 DB 侧（索引是否存在 + ALTER 线程）为准。\n")
    if args.once:
        return 0

    seen_missing = not snap["exists"]
    started = time.time()
    last_t = started
    last_bytes = cur_bytes
    rate = 0.0
    last_log = started
    while True:
        time.sleep(args.interval)
        now = time.time()
        ex = index_exists()
        alt_sec, alt_state = altering()
        files = index_files(base)
        size = files[0][1] if files else 0
        dt = max(1e-6, now - last_t)
        if size >= last_bytes and (size - last_bytes) > 0:
            inst = (size - last_bytes) / dt
            rate = inst if rate == 0 else (0.7 * rate + 0.3 * inst)
        last_t, last_bytes = now, size

        if ex is False:
            seen_missing = True

        # 阶段判定
        if alt_sec and ex is False:
            phase = PHASE_BUILDING
        elif alt_sec and ex:
            phase = PHASE_FINISHING
        elif ex and not alt_sec:
            phase = PHASE_IDLE if seen_missing else PHASE_LATE
        else:
            phase = PHASE_DROPPED

        if seen_missing and ex and not alt_sec:
            print("\n✔ 重建完成：索引已存在且无 ALTER 在跑（总耗时 %s，最终体积 %.1f MB）"
                  % (hhmmss(now - started), size / 1048576.0))
            print("  接下来验证：")
            print("    · 索引是否被使用：EXPLAIN SELECT chunk_id FROM documents_book_chunk "
                  "ORDER BY VEC_DISTANCE_COSINE(embedding, VEC_FromText('[0.001,...]')) LIMIT 5;")
            print("      → key=idx_vec_chunk / type=index 即索引生效")
            print("    · 召回体检（可选）：scripts/diag_vec_recall.py（top-50 vs 暴力）")
            return 0

        pct = (100.0 * size / target) if target else 0.0
        eta = ((target - size) / rate) if (target and rate > 0 and size < target) else 0.0
        line = "[%s] %s %.1f/%.1f MB %s %5.1f%%  %.2f MB/s  ETA %s  已跑 %s   %s" % (
            phase.split("（")[0], (files[0][0].name if files else "-"),
            size / 1048576.0, target / 1048576.0, bar(pct), pct, rate / 1048576.0,
            hhmmss(eta) if eta else "--:--:--", hhmmss(now - started),
            ("ALTER %ds" % alt_sec) if alt_sec else "",
        )
        print("\r" + line[:WIDTH].ljust(WIDTH), end="", flush=True)
        if now - last_log > 300:            # 每 5 分钟留一条历史行（便于回看）
            print()
            print("  [%s] %s" % (time.strftime("%H:%M:%S"), line))
            last_log = now


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n(已退出观测；重建仍在服务端继续)")
        raise SystemExit(130)
