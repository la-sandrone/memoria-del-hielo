"""
测试: src/core/http_client.py —— 本地 HTTP 客户端必须免疫 NO_PROXY 脏值。

背景（2026-08-29 实测）：httpx 0.28.1 构造 Client 时把 NO_PROXY 每一项当 URL
解析，带方括号的 IPv6 条目 "[::1]" 会被首个 ':' 切碎 →
InvalidURL: Invalid port: ':1]' → search_docs / 导入 / OCR 全体当场死亡。
本测试在敌意环境下构造客户端：若有人把 trust_env=False 去掉，这里必须红。
"""

import httpx
import pytest

from src.core.http_client import local_client, local_get


@pytest.fixture
def hostile_proxy_env(monkeypatch):
    """复刻现场环境：NO_PROXY 含带括号的 IPv6 条目 + 本地代理。"""
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1,[::1]")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8994")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:8994")
    return "localhost,127.0.0.1,::1,[::1]"


def test_local_client_immune_to_bracket_ipv6_no_proxy(hostile_proxy_env):
    """敌意 NO_PROXY 下，local_client 必须能构造成功。"""
    client = local_client(30.0)
    try:
        assert client.timeout.connect == 30.0
    finally:
        client.close()


def test_plain_httpx_client_is_actually_broken(hostile_proxy_env):
    """反向锚定：裸 httpx.Client() 在这个环境下确实会炸（证明测试有效）。"""
    with pytest.raises(httpx.InvalidURL):
        httpx.Client(timeout=5.0)


def test_local_client_timeout_passthrough(hostile_proxy_env):
    client = local_client(15.0)
    try:
        assert client.timeout.connect == 15.0
        assert client.timeout.read == 15.0
    finally:
        client.close()


def test_local_get_parametrization_does_not_raise_on_env(hostile_proxy_env):
    """local_get 在敌意环境下构造请求（不真发网；只验证客户端初始化不炸）。"""
    # 指向必然拒绝连接的端口：要发生的是 ConnectError，绝不是 InvalidURL
    with pytest.raises(httpx.HTTPError) as excinfo:
        local_get("http://127.0.0.1:1/health", timeout=0.5)
    assert not isinstance(excinfo.value, httpx.InvalidURL)
