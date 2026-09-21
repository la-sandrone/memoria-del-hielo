#!/usr/bin/env bash
#
# mhi.sh —— Memoria del Hielo CLI 转发器（WSL / Linux）
#
# 用法:  mhi <子命令> [参数...]
#        mhi import "<书库根目录>" --tags "<语料 tag>"
#        mhi rm --guid 1006bb53-974d-4edd-aa8c-3e72d1cfd8a0 --dry-run
#        mhi help
#
# 说明:
#   * 参数用 "$@" 原样转交 src/cli/mhi_dispatch.py，不经任何重新解析。
#   * 不切换当前目录：相对路径参数按你敲命令时所在的目录解析。
#   * exec 替换进程，Ctrl+C 直达 python（两级中断语义不被转发层吞掉）。
#   * 解释器可用 MHI_PYTHON 覆盖，默认 ai-services venv，缺失则回退 python3。
#
# 安装（想在任何目录直接敲 mhi）:
#   ln -sf "$PWD/mhi.sh" ~/.local/bin/mhi      # 在项目根目录执行
#   chmod +x "$PWD/mhi.sh"
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 解释器：MHI_PYTHON 最优先；否则在常见 venv 位置里找；最后回退 python3
PY="${MHI_PYTHON:-}"
if [ -z "$PY" ]; then
    for cand in "$HOME/ai-services/bin/python" "$HOME/.venv/bin/python" "$ROOT/.venv/bin/python"; do
        if [ -x "$cand" ]; then PY="$cand"; break; fi
    done
fi
if [ -z "$PY" ]; then
    PY="$(command -v python3)"
fi

exec "$PY" "$ROOT/src/cli/mhi_dispatch.py" "$@"
