#!/usr/bin/env python3
# 深度诊断：导入进程卡在哪（wchan + 连接 + DB processing 状态）
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.core.db import get_db

PID = "17502"


def sh(cmd: str) -> str:
    r = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True)
    return r.stdout.strip()


def main() -> int:
    print("== 导入进程状态 ==")
    print(sh(f"ps -o pid,stat,pcpu,etime,wchan:40,cmd -p {PID}"))
    print("\n== 进程线程（哪个线程在忙）==")
    print(sh(f"ps -L -o pid,tid,stat,pcpu,wchan:30 -p {PID} | head -20"))
    print("\n== 9005(BGE)/9008(OCR) 连接（挂起请求？）==")
    print(sh("ss -tnp | grep -E '9005|9008' | head -20"))
    print("\n== OCR server 最近 CPU 增量（10 秒采样）==")
    p1 = sh("ps -o time= -p 1583")
    time.sleep(10)
    p2 = sh("ps -o time= -p 1583")
    print(f"  10 秒内 OCR server CPU 时间: {p1} -> {p2}")
    print("\n== DB processing 状态的书 ==")
    db = get_db()
    rows = db.execute_sql(
        "SELECT key_metadata, status, expected_chunks, created_at "
        "FROM documents_book WHERE status != 'done' ORDER BY created_at DESC LIMIT 10"
    )
    for r in rows:
        print(f"  {r['created_at']} {r['status']:<12} {r['key_metadata'][:40]} exp={r['expected_chunks']}")
    print("\n== 最近 chunk 写入 ==")
    # 注：MAX(created_at) 无索引会全表扫；反向扫主键取最新行（毫秒级）
    rows = db.execute_sql(
        "SELECT created_at AS m FROM documents_book_chunk ORDER BY chunk_id DESC LIMIT 1"
    )
    print(f"  {rows[0]['m'] if rows else '无'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
