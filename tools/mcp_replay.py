#!/usr/bin/env python3
"""
文件名: mcp_replay.py
摘要: MCP 请求重放工具。通过 stdio 向 MCP Server 发送 JSONL 格式的请求，
      捕获响应并显示。用于测试、调试、回归验证。
用法:
    python tools/mcp_replay.py --server python -m src.mcp_server --replay tests/replay/basic.replay
依赖: 无外部依赖（纯标准库）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

# ── 参数 ───────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MCP 请求重放工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 运行基本重放
  python tools/mcp_replay.py --replay tests/replay/basic.replay

  # 显式指定 server 命令
  python tools/mcp_replay.py \\
      --server "python -m src.mcp_server" \\
      --replay tests/replay/smoke.replay

  # 只验证语法不执行
  python tools/mcp_replay.py --replay tests/replay/basic.replay --dry-run

重放文件格式:
  # 注释行
  @delay 200       # 发送前等待 200ms
  @label 搜索图书   # 给下一个请求贴标签
  @save var_name   # 从上一个响应中提取 result 保存到 {{var_name}}
  {"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
        """,
    )
    parser.add_argument(
        "--server",
        default="python -m src.mcp_server",
        help="MCP Server 启动命令（默认: python -m src.mcp_server）",
    )
    parser.add_argument(
        "-r", "--replay",
        required=True,
        help="重放文件路径（JSONL + 指令格式）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="每个请求的超时秒数（默认 30）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只解析重放文件，不执行",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="仅输出汇总，不打印每个响应的详情",
    )
    return parser.parse_args()


# ── 重放文件解析 ──────────────────────────────────────────

class ReplayStep:
    """单步重放指令。"""
    def __init__(
        self,
        request: dict | None = None,
        delay_ms: int = 0,
        label: str = "",
        save_var: str = "",
        line_no: int = 0,
    ):
        self.request = request
        self.delay_ms = delay_ms
        self.label = label
        self.save_var = save_var
        self.line_no = line_no


def parse_replay(filepath: str) -> list[ReplayStep]:
    """解析重放文件，返回步骤列表。"""
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    steps: list[ReplayStep] = []
    current_delay = 0
    current_label = ""
    current_save = ""

    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        line_no = i + 1

        # 空行
        if not line:
            continue

        # 注释
        if line.startswith("#"):
            continue

        # 指令
        if line.startswith("@"):
            parts = line[1:].split(None, 1)
            directive = parts[0].lower() if parts else ""
            value = parts[1] if len(parts) > 1 else ""

            if directive == "delay":
                try:
                    current_delay = int(value)
                except ValueError:
                    print(f"⚠️  第 {line_no} 行: @delay 值无效 '{value}'，忽略")
                    current_delay = 0
            elif directive == "label":
                current_label = value.strip()
            elif directive == "save":
                current_save = value.strip()
            else:
                print(f"⚠️  第 {line_no} 行: 未知指令 '@{directive}'，忽略")
            continue

        # JSON-RPC 请求行
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"❌ 第 {line_no} 行: JSON 解析失败 — {exc}")
            print(f"   原文: {line[:200]}")
            sys.exit(1)

        if not isinstance(request, dict):
            print(f"❌ 第 {line_no} 行: 期望 JSON 对象，得到 {type(request).__name__}")
            sys.exit(1)

        steps.append(ReplayStep(
            request=request,
            delay_ms=current_delay,
            label=current_label,
            save_var=current_save,
            line_no=line_no,
        ))

        # 重置指令（label/save 只绑定一步）
        current_label = ""
        current_save = ""
        # delay 保留（后续步骤也使用相同延迟，除非被覆盖）

    return steps


# ── 变量替换 ──────────────────────────────────────────────

def apply_variables(obj: dict, vars_dict: dict[str, str]) -> dict:
    """替换 JSON 字符串中的 {{var_name}} 占位符。"""
    raw = json.dumps(obj, ensure_ascii=False)
    for key, value in vars_dict.items():
        raw = raw.replace("{{" + key + "}}", value)
    return json.loads(raw)


# ── MCP stdio 通信 ────────────────────────────────────────

def _read_response(proc: subprocess.Popen) -> dict | None:
    """从进程 stdout 读取一行 JSON-RPC 响应。"""
    if proc.stdout is None:
        return None
    line = proc.stdout.readline()
    if not line:
        return None
    try:
        return json.loads(line.decode("utf-8").strip())
    except json.JSONDecodeError:
        return None


def run_replay(server_cmd: str, steps: list[ReplayStep], args: argparse.Namespace):
    """启动 MCP Server 子进程，按步骤发送请求，收集响应。"""
    if args.dry_run:
        print(f"✅ Dry-run: 解析到 {len(steps)} 步")
        for i, s in enumerate(steps):
            label = f" [{s.label}]" if s.label else ""
            method = s.request.get("method", "?")
            print(f"  {i+1}. {method}{label}  (line {s.line_no}, delay={s.delay_ms}ms)")
        return

    # 启动 MCP Server
    try:
        proc = subprocess.Popen(
            server_cmd.split(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
    except FileNotFoundError as exc:
        print(f"❌ 无法启动 MCP Server: {exc}")
        print(f"   命令: {server_cmd}")
        sys.exit(1)

    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    vars_dict: dict[str, str] = {}
    stats = {"sent": 0, "responses": 0, "errors": 0, "timeouts": 0}
    last_label = ""

    print(f"🚀 MCP Server 已启动 (PID {proc.pid})")
    print(f"   重放 {len(steps)} 步\n")

    for i, step in enumerate(steps):
        step_num = i + 1
        label = step.label or last_label
        if step.label:
            last_label = step.label

        # 延迟
        if step.delay_ms > 0:
            time.sleep(step.delay_ms / 1000.0)

        # 变量替换
        request = apply_variables(step.request, vars_dict)
        raw = json.dumps(request, ensure_ascii=False) + "\n"

        # 发送
        try:
            proc.stdin.write(raw.encode("utf-8"))
            proc.stdin.flush()
            stats["sent"] += 1
        except BrokenPipeError:
            print(f"❌ [step {step_num}] BrokenPipeError — MCP Server 已退出")
            stats["errors"] += 1
            break

        # 读取响应
        response = _read_response(proc)

        # 显示
        method = request.get("method", "?")
        req_id = request.get("id", "?")

        if response is None:
            tag = "⏰" if args.timeout <= 0 else "⚠️"
            print(f"  {tag} [{step_num}/{len(steps)}] {label}{' ' + method if label else method}(id={req_id}) 无响应")
            stats["timeouts"] += 1
            continue

        # 检查 error
        is_error = "error" in response and response["error"] is not None
        status = "✅" if not is_error else "❌"
        if is_error:
            stats["errors"] += 1
        stats["responses"] += 1

        # 提取 result 摘要
        result = response.get("result")
        result_preview = _preview_result(result)

        print(f"  {status} [{step_num}/{len(steps)}] {label}{' ' + method if label else method}(id={req_id})")
        if not args.summary_only:
            print(f"       {result_preview}")

        # @save: 保存整个 result 到变量
        if step.save_var and result is not None:
            if isinstance(result, str):
                # 尝试解析 JSON 字符串
                try:
                    parsed = json.loads(result)
                    if isinstance(parsed, list) and len(parsed) > 0:
                        first = parsed[0]
                        if isinstance(first, dict) and "guid" in first:
                            vars_dict[step.save_var] = first["guid"]
                            continue
                    vars_dict[step.save_var] = json.dumps(parsed, ensure_ascii=False)
                except (json.JSONDecodeError, TypeError):
                    vars_dict[step.save_var] = result[:200]
            elif isinstance(result, dict):
                if "guid" in result:
                    vars_dict[step.save_var] = result["guid"]
                else:
                    vars_dict[step.save_var] = json.dumps(result, ensure_ascii=False)
            elif isinstance(result, list) and len(result) > 0:
                first = result[0]
                if isinstance(first, dict) and "guid" in first:
                    vars_dict[step.save_var] = first["guid"]
                else:
                    vars_dict[step.save_var] = json.dumps(result, ensure_ascii=False)

        # 读取可能的 stderr（非阻塞方式，只读一点点）
        _drain_stderr(proc)

    # 关闭
    _send_exit(proc)
    _drain_stderr(proc)

    elapsed = proc.wait(timeout=5)

    print(f"\n{'='*50}")
    print(f"📊 统计:")
    print(f"   发送:     {stats['sent']}")
    print(f"   响应:     {stats['responses']}")
    print(f"   错误:     {stats['errors']}")
    print(f"   超时:     {stats['timeouts']}")
    print(f"   Exit:     {elapsed}")
    if vars_dict:
        print(f"   变量:     {vars_dict}")

    if stats["errors"] > 0:
        sys.exit(1)


def _preview_result(result) -> str:
    """生成 result 的一行摘要。"""
    if result is None:
        return "∅"

    if isinstance(result, str):
        if len(result) > 200:
            return result[:200] + "..."
        return result

    if isinstance(result, dict):
        keys = list(result.keys())
        preview = json.dumps(result, ensure_ascii=False)
        if len(preview) > 200:
            preview = preview[:200] + "..."
        return preview

    if isinstance(result, list):
        return f"[{len(result)} items]"

    preview = str(result)
    if len(preview) > 200:
        preview = preview[:200] + "..."
    return preview


def _send_exit(proc: subprocess.Popen):
    """发送 notifications/exit。"""
    if proc.stdin and proc.stdin.writable():
        try:
            msg = json.dumps({"jsonrpc": "2.0", "method": "notifications/exit", "params": {}}) + "\n"
            proc.stdin.write(msg.encode("utf-8"))
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass


def _drain_stderr(proc: subprocess.Popen):
    """非阻塞地读取 stderr 中的少量输出并显示。"""
    if proc.stderr is None:
        return
    import select
    try:
        if select.select([proc.stderr], [], [], 0.1)[0]:
            err_data = proc.stderr.read(4096)
            if err_data:
                text = err_data.decode("utf-8", errors="replace").strip()
                if text:
                    for line in text.split("\n"):
                        line = line.strip()
                        if line and "warning" not in line.lower():
                            print(f"  ⚠️  stderr: {line}")
    except (OSError, ValueError):
        pass


# ── 入口 ───────────────────────────────────────────────────

def main():
    args = parse_args()

    # 解析重放文件
    if not args.dry_run:
        print(f"📂 重放文件: {args.replay}")
    steps = parse_replay(args.replay)
    print(f"📋 解析到 {len(steps)} 步\n")

    run_replay(args.server, steps, args)


if __name__ == "__main__":
    main()
