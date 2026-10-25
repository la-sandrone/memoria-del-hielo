#!/usr/bin/env python3
"""诊断：全量导入是否还在跑 + 服务状态 + 最近入库记录。

无头安全（2026-09-27 加固）
    本脚本默认跑在无头模式下（PECMD 隐藏输出），一旦某一步抛异常，整个进程会当场死掉，
    **输出全丢且没人会发现**。故：
      · 每一步独立兜异常 —— 任一环节失败不得让整份诊断静默死掉。
      · 全程双写：stdout + logs/diagnose_import.log（追加，沿用本仓 logs/ 惯例）。
      · 末尾追加一行 `SNAPSHOT {...}` JSON，便于事后 grep / 机器解析（无头下唯一可查记录）。
      · 对 server_bge 的 /health 额外摘出 upstream 状态与传输策略，一并落账。

用法（两个平台均可）
    python scripts/diagnose_import.py
"""
from __future__ import annotations

import json
import subprocess  # noqa: S404 —— 仅用于本机 ps 快照，不接外部输入
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "logs" / "diagnose_import.log"

# /health 响应里值得落账的 upstream 字段（顺序即输出顺序）
_UPSTREAM_FIELDS = ("ts", "kind", "status", "ms", "message")


class Tee:
    """同时写 stdout 与日志文件。

    日志写失败**只警告一次**，绝不影响 stdout —— 诊断工具本身不能成为新的故障点。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None
        self._warned = False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("a", encoding="utf-8")
        except OSError as exc:
            print(
                "[warn] 日志文件不可写（%s: %s），本次仅输出到 stdout" % (type(exc).__name__, exc),
                file=sys.stderr,
            )

    def write(self, s: str) -> None:
        sys.stdout.write(s)
        if self._fh is not None:
            try:
                self._fh.write(s)
                self._fh.flush()
            except OSError as exc:
                if not self._warned:
                    self._warned = True
                    print(
                        "[warn] 写日志失败（%s: %s），后续仅输出到 stdout"
                        % (type(exc).__name__, exc),
                        file=sys.stderr,
                    )

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass


def main() -> int:
    out = Tee(LOG_PATH)
    summary: dict = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "python_procs": None,
        "services": {},
        "latest_book": None,
        "latest_chunk": None,
        "errors": [],
    }

    def say(line: str = "") -> None:
        out.write(line + "\n")

    def fail(step: str, exc: BaseException) -> None:
        """记录一步失败：既可见（print）又落账（summary）。"""
        detail = "%s: %s" % (type(exc).__name__, exc)
        summary["errors"].append({"step": step, "error": detail,
                                  "trace": traceback.format_exc(limit=3)})
        say("  [!] %s 失败：%s" % (step, detail))

    say("=" * 70)
    say("diagnose_import  @ %s" % summary["ts"])
    say("=" * 70)

    # ── 1. python 进程（排除本脚本自身）──
    say("\n== python 进程 ==")
    try:
        proc = subprocess.run(["ps", "aux"], capture_output=True, text=True)  # noqa: S603
        pys = [
            ln for ln in proc.stdout.splitlines()
            if "python" in ln and "diagnose_import" not in ln
        ]
        summary["python_procs"] = len(pys)
        if not pys:
            say("  (无 python 进程——导入已结束或未运行)")
        for ln in pys:
            say("  " + ln[:160])
    except Exception as exc:
        fail("python 进程", exc)

    # ── 2. 服务状态（端点从 config 读，避免硬编码端口与 config.toml 漂移）──
    say("\n== 服务状态 ==")
    try:
        from src.core.config import get_config

        cfg = get_config()
        checks = [
            ("BGE", "http://127.0.0.1:9005/v1/embeddings"),
            ("BGE2", "http://127.0.0.1:9006/health"),
        ]
        ocr_ep = str(cfg.get("ocr.endpoint", ""))
        if ocr_ep.startswith("http"):
            checks.insert(0, ("OCR", ocr_ep.rsplit("/", 1)[0] + "/health"))
        else:
            say("  OCR: 端点非 HTTP（%r），跳过预检" % ocr_ep)

        for name, url in checks:
            entry: dict = {"url": url}
            try:
                # trust_env=False：免疫 NO_PROXY 脏值（见 src/core/http_client.py 定案）
                resp = httpx.get(url, timeout=3, trust_env=False)
                entry["status"] = resp.status_code
                say("  %s: HTTP %s  %s" % (name, resp.status_code, url))
                # server_bge 的 /health 带回上游健康与传输策略 —— 一并摘进账
                if url.endswith("/health"):
                    try:
                        body = resp.json()
                        tr = body.get("transport") or {}
                        up = body.get("upstream") or {}
                        entry["ok"] = body.get("ok")
                        entry["http1_forced"] = tr.get("http1_forced")
                        entry["h2_available"] = tr.get("h2_available")
                        entry["upstream_state"] = up.get("state")
                        if up.get("last_error"):
                            entry["upstream_last_error"] = {
                                k: up["last_error"].get(k) for k in _UPSTREAM_FIELDS
                            }
                        say(
                            "        ok=%s  upstream=%s  http1_forced=%s"
                            % (entry.get("ok"), entry.get("upstream_state"),
                               entry.get("http1_forced"))
                        )
                        if entry.get("upstream_last_error"):
                            le = entry["upstream_last_error"]
                            say(
                                "        last_error: kind=%s ms=%s status=%s %s"
                                % (le.get("kind"), le.get("ms"), le.get("status"),
                                   str(le.get("message"))[:160])
                            )
                    except Exception as exc:
                        entry["parse_error"] = "%s: %s" % (type(exc).__name__, exc)
                        say("        (health 解析失败：%s)" % entry["parse_error"])
            except Exception as exc:
                entry["error"] = "%s: %s" % (type(exc).__name__, exc)
                say("  %s: %s  %s" % (name, entry["error"], url))
            summary["services"][name] = entry
    except Exception as exc:
        fail("服务状态", exc)

    # ── 3/4. 数据库：最近入库 + 最近 chunk ──
    say("\n== 最近入库的书（documents_book.created_at DESC）==")
    db = None
    try:
        from src.core.db import get_db

        db = get_db()
        rows = db.execute_sql(
            "SELECT key_metadata, created_at, status FROM documents_book "
            "ORDER BY created_at DESC LIMIT 8"
        )
        for r in rows:
            say("  %s  %-10s %s" % (r["created_at"], r["status"], r["key_metadata"][:50]))
        if rows:
            summary["latest_book"] = {
                "created_at": str(rows[0]["created_at"]),
                "status": rows[0]["status"],
            }
    except Exception as exc:
        fail("最近入库", exc)

    say("\n== 最近 chunk 写入 ==")
    try:
        if db is not None:
            # 注：MAX(created_at) 无索引 → 全表扫（实测 7.7s，149,737 行）；
            #     chunk_id 自增单调，反向扫主键即最新行（毫秒级）。
            rows = db.execute_sql(
                "SELECT created_at AS latest FROM documents_book_chunk "
                "ORDER BY chunk_id DESC LIMIT 1"
            )
            latest = rows[0]["latest"] if rows else None
            summary["latest_chunk"] = str(latest) if latest is not None else None
            say("  最近 chunk 写入: %s" % (latest if latest else "无"))
        else:
            say("  (数据库不可用，跳过)")
    except Exception as exc:
        fail("最近 chunk", exc)

    # ── 收尾：一行 JSON 快照（无头模式下的唯一可查记录）──
    summary["ok"] = not summary["errors"]
    say("")
    say("SNAPSHOT " + json.dumps(summary, ensure_ascii=False))
    out.close()
    # 有步骤失败时返回 1，便于调用方（PECMD/守护脚本）据退出码判断
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
