#!/usr/bin/env python3
# 诊断：全量导入是否还在跑 + 服务状态 + 最近入库记录
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx

from src.core.config import get_config
from src.core.db import get_db


def main() -> int:
    # 1. python 进程（排除本脚本自身）
    out = subprocess.run(
        ["ps", "aux"], capture_output=True, text=True
    ).stdout
    pys = [l for l in out.splitlines() if "python" in l and "diagnose_import" not in l]
    print("== python 进程 ==")
    if not pys:
        print("  (无 python 进程——导入已结束或未运行)")
    for l in pys:
        print(" ", l[:160])

    # 2. 服务（端点从 config 读，避免硬编码端口与 config.toml 漂移）
    print("\n== 服务状态 ==")
    cfg = get_config()
    checks = [("BGE", "http://127.0.0.1:9005/v1/embeddings"),
              ("BGE2", "http://127.0.0.1:9006/health")]
    ocr_ep = str(cfg.get("ocr.endpoint", ""))
    if ocr_ep.startswith("http"):
        checks.insert(0, ("OCR", ocr_ep.rsplit("/", 1)[0] + "/health"))
    else:
        print(f"  OCR: 端点非 HTTP（{ocr_ep!r}），跳过预检")
    for name, url in checks:
        try:
            r = httpx.get(url, timeout=3, trust_env=False)  # 免疫 NO_PROXY 脏值
            print(f"  {name}: HTTP {r.status_code}  {url}")
        except Exception as exc:
            print(f"  {name}: {type(exc).__name__}: {exc}  {url}")

    # 3. 最近入库（最后 8 本）
    print("\n== 最近入库的书（documents_book.created_at DESC）==")
    db = get_db()
    rows = db.execute_sql(
        "SELECT key_metadata, created_at, status FROM documents_book "
        "ORDER BY created_at DESC LIMIT 8"
    )
    for r in rows:
        print(f"  {r['created_at']}  {r['status']:<10} {r['key_metadata'][:50]}")

    # 4. 最近 chunk 写入时间（判断嵌入/入库是否还在推进）
    #    注：MAX(created_at) 无索引 → 全表扫（实测 7.7s，149,737 行）；
    #    chunk_id 自增单调，反向扫主键即最新行（毫秒级）。
    rows = db.execute_sql(
        "SELECT created_at AS latest FROM documents_book_chunk ORDER BY chunk_id DESC LIMIT 1"
    )
    print(f"\n最近 chunk 写入: {rows[0]['latest'] if rows else '无'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
