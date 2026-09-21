"""
文件名: http_client.py
摘要: 本地服务 HTTP 客户端工厂——统一 trust_env=False。
      为什么必须关掉 trust_env（2026-08-29 实测定案）：
        httpx（0.28.1）构造 Client 时会把 NO_PROXY 的每一项当作 URL 解析
        （URLPattern），而带方括号的 IPv6 条目（如 "[::1]"）会被首个 ':' 切碎，
        直接抛 InvalidURL: Invalid port: ':1]' —— 搜索/导入/OCR 全体当场死亡。
        实测：NO_PROXY 含 "[::1]" → 崩；含 "::1"（无括号）→ 正常；
        trust_env=False → 免疫。这不是我们的锅，但炸的是我们的进程。
      另外：本项目端点全是 127.0.0.1 本地服务（BGE/OCR/MariaDB/Everything），
      代理环境变量对它们没有任何意义，信任它只会引入故障面。
依赖: httpx
"""

import httpx


def local_client(timeout: float = 60.0) -> httpx.Client:
    """构造无视代理环境变量的 httpx.Client（本地服务专用）。

    所有指向 127.0.0.1 的调用都应走这里，不要直接 httpx.Client()。
    """
    return httpx.Client(timeout=timeout, trust_env=False)


def local_get(url: str, timeout: float = 5.0) -> httpx.Response:
    """httpx.get 的本地版（同样无视代理环境变量），供脚本/health 预检使用。"""
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        return client.get(url)
