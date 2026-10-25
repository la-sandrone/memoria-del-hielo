"""测试: server_bge.RateLimiter —— FIFO 叫号、有界等待、AIMD、并发槽不泄漏。

背景（2026-10-25 v2）：v1 的 `acquire` 是 `while True: ... await asyncio.sleep(0.05)`
**持锁轮询**——等待期间 `_serial` 不释放，且每 50ms 醒一次盲问「能走了吗」。
v2 换成 Future 事件叫醒。本文件守住五件事：

  1. 严格 FIFO —— 队首走不了时**不跳号**（跳号就是饥饿的开始）
  2. 有界 —— 超时抛 RateLimiterBusy，且**把自己从队列摘掉**（否则队首永久卡死）
  3. 无僵尸 —— 取消的等待者不占位
  4. 并发槽不漏 —— `slot()` 任何出口（正常/异常/取消）都 release()
  5. AIMD 方向正确 —— 429/超时降、连续成功升、下限不被击穿

**零依赖设计**：不 import pytest（本仓的 serv-venv 全都没装 pytest）。
  · 直接跑：`python tests/test_rate_limiter.py`
  · 装了 pytest 的环境照常收集（每个 test_* 都是 async 函数 + 自带 mark）

只测纯逻辑：`sentence_transformers` 被 stub 掉，不加载任何真模型。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import httpx

# ── 先挡住重依赖再 import（server_bge 顶层 import torch/sentence_transformers）──
_st = types.ModuleType("sentence_transformers")
_st.SentenceTransformer = object
_st.CrossEncoder = object
sys.modules.setdefault("sentence_transformers", _st)

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server_bge import (  # noqa: E402
    RateLimiter,
    RateLimiterBusy,
    RateLimiterOversize,
)

# ── pytest 可选（有则用它的 mark/raises，无则回落到内置实现）──
try:
    import pytest  # type: ignore
except ImportError:  # pragma: no cover - 本仓默认走这条
    pytest = None


def _asyncio_mark(fn):
    """有 pytest 时打 asyncio 标记；没有则原样返回。"""
    return pytest.mark.asyncio(fn) if pytest is not None else fn


class _Expect:
    """`pytest.raises` 的零依赖替代：断言抛出指定异常类型。"""

    def __init__(self, exc_type: type[BaseException]) -> None:
        self.exc_type = exc_type
        self.value: BaseException | None = None

    def __enter__(self) -> "_Expect":
        return self

    def __exit__(self, et, ev, tb) -> bool:
        if et is None:
            raise AssertionError("期望抛 %s，但没有异常" % self.exc_type.__name__)
        if not issubclass(et, self.exc_type):
            return False          # 让意外的异常继续上抛（不吞错）
        self.value = ev
        return True


_raises = pytest.raises if pytest is not None else _Expect


# ── 1. 快路径 ────────────────────────────────────────────────────────────


@_asyncio_mark
async def test_fast_path_grants_without_queue_or_timer():
    """空队列 + 容量够 → 立即放行：零 Future、零定时器、零调度。"""
    lim = RateLimiter(rpm=6000, tpm=0)
    t0 = asyncio.get_running_loop().time()
    await lim.acquire()
    elapsed = asyncio.get_running_loop().time() - t0

    assert elapsed < 0.05, "快路径不该引入任何等待"
    assert lim._inflight == 1
    assert lim._acquired == 1
    assert len(lim._waiters) == 0
    assert lim._wake_handle is None, "没人排队就不该装定时器"
    lim.release()


# ── 2. 严格 FIFO ─────────────────────────────────────────────────────────


@_asyncio_mark
async def test_strict_fifo_order_under_serial_cap():
    """max_inflight=1 时，放行顺序必须等于到达顺序（不跳号、不重排）。"""
    lim = RateLimiter(rpm=0, tpm=0, max_inflight=1)
    order: list[int] = []

    async def worker(i: int) -> None:
        await lim.acquire()
        try:
            order.append(i)
            await asyncio.sleep(0.005)
        finally:
            lim.release()

    await asyncio.gather(*(worker(i) for i in range(6)))

    assert order == list(range(6)), "放行顺序被重排了：%r" % (order,)
    assert lim._inflight == 0


@_asyncio_mark
async def test_head_not_skipped_when_too_big_for_bucket():
    """队首 token 需求过大时，**不许**跳过它去放行后面的小请求。

    这是「跳号 = 饥饿」的直接判据：小请求若被放行，说明叫号器跳过了队首。
    """
    lim = RateLimiter(rpm=0, tpm=1000, window=60.0, max_inflight=8)
    lim._tok_tokens = 500.0          # 制造「小请求能过、大请求不能过」的局面

    big = asyncio.create_task(lim.acquire(tokens=800))
    await asyncio.sleep(0)           # 让它先入队
    small = asyncio.create_task(lim.acquire(tokens=10))
    await asyncio.sleep(0.05)        # 给足叫号机会

    assert not small.done(), "队首（800 tokens）过不去时，后面的小请求不该被放行"
    assert len(lim._waiters) == 2

    big.cancel()
    small.cancel()
    for t in (big, small):
        with _raises(asyncio.CancelledError):
            await t


# ── 3. 有界等待 / 无僵尸 ─────────────────────────────────────────────────


@_asyncio_mark
async def test_timeout_raises_busy_and_leaves_queue():
    """超时必须抛 Busy，而且**把自己摘掉**——否则队首永久卡死。"""
    lim = RateLimiter(rpm=1, tpm=0, window=3600.0)   # 补充速率 1/3600 每秒
    await lim.acquire()                              # rpm 只有 1，已耗尽

    with _raises(RateLimiterBusy) as ei:
        await lim.acquire(timeout=0.05)

    assert lim._busy_events == 1
    assert len(lim._waiters) == 0, "超时的等待者必须离队"
    detail = ei.value.detail if ei.value is not None else ""
    assert "waiters=" in detail
    lim.release()


@_asyncio_mark
async def test_cancel_leaves_no_zombie_at_head():
    """取消的等待者不能留在队首占位（Future 已 done → _pump 会摘掉）。"""
    lim = RateLimiter(rpm=0, tpm=0, max_inflight=1)
    await lim.acquire()                     # 占满唯一并发槽

    task = asyncio.create_task(lim.acquire())
    await asyncio.sleep(0)
    assert len(lim._waiters) == 1

    task.cancel()
    with _raises(asyncio.CancelledError):
        await task

    assert len(lim._waiters) == 0, "被取消的等待者必须离队"
    lim.release()


@_asyncio_mark
async def test_release_wakes_head_waiter():
    """release() 是事件驱动的放行源：槽一放，队首立刻被叫醒（无需等定时器）。"""
    lim = RateLimiter(rpm=0, tpm=0, max_inflight=1)
    await lim.acquire()

    async def second() -> None:
        await lim.acquire()
        lim.release()

    task = asyncio.create_task(second())
    await asyncio.sleep(0)
    assert len(lim._waiters) == 1
    assert lim._wake_handle is None, "队在等并发槽（时间不可算）→ 不该装定时器"

    lim.release()                           # ← 叫醒源
    await asyncio.wait_for(task, timeout=1.0)
    assert lim._inflight == 0


# ── 4. 并发槽不泄漏 ──────────────────────────────────────────────────────


@_asyncio_mark
async def test_slot_releases_on_exception():
    lim = RateLimiter(rpm=0, tpm=0, max_inflight=2)
    with _raises(RuntimeError):
        async with lim.slot():
            raise RuntimeError("boom")
    assert lim._inflight == 0


@_asyncio_mark
async def test_inflight_never_exceeds_limit():
    lim = RateLimiter(rpm=0, tpm=0, max_inflight=2)
    peak = 0

    async def worker() -> None:
        nonlocal peak
        async with lim.slot():
            peak = max(peak, lim._inflight)
            await asyncio.sleep(0.005)

    await asyncio.gather(*(worker() for _ in range(10)))

    assert peak <= 2, "并发门没挡住：峰值 %d" % peak
    assert lim._peak_inflight <= 2
    assert lim._inflight == 0, "10 笔跑完必须归零"
    assert lim._acquired == 10


@_asyncio_mark
async def test_oversize_rejected_without_queueing():
    """单笔超 TPM：桶永远满足不了 → 立即拒，**不排队**（排队必永久阻塞）。"""
    lim = RateLimiter(rpm=0, tpm=100)
    with _raises(RateLimiterOversize):
        await lim.acquire(tokens=101)
    assert lim._oversize_rejected == 1
    assert len(lim._waiters) == 0
    assert lim._inflight == 0


# ── 5. AIMD ──────────────────────────────────────────────────────────────


@_asyncio_mark
async def test_aimd_starts_at_ceiling():
    """起点 = 天花板（「先开到指定的最高」，不是从 1 慢启动）。"""
    lim = RateLimiter(rpm=0, tpm=0, max_inflight=8)
    assert lim._limit == 8


@_asyncio_mark
async def test_aimd_multiplicative_decrease_with_floor():
    lim = RateLimiter(rpm=0, tpm=0, max_inflight=8, aimd_decrease_factor=0.5)
    lim.report_congestion()
    assert lim._limit == 4
    lim.report_congestion()
    assert lim._limit == 2
    for _ in range(6):
        lim.report_congestion()
    assert lim._limit == 1, "下限是 1，不能归零"
    assert lim._congestion_events == 8


@_asyncio_mark
async def test_aimd_additive_increase_up_to_ceiling():
    lim = RateLimiter(rpm=0, tpm=0, max_inflight=4, aimd_increase_after=2)
    for _ in range(3):
        lim.report_congestion()      # 4 → 2 → 1 → 1
    assert lim._limit == 1

    lim.report_success()
    assert lim._limit == 1, "未达阈值不增"
    lim.report_success()
    assert lim._limit == 2, "连续成功 2 笔 → +1"

    for _ in range(20):
        lim.report_success()
    assert lim._limit == 4, "不得超过天花板"


@_asyncio_mark
async def test_aimd_single_lane_is_identity():
    """max_inflight=1（默认「只一路去」）时 AIMD 退化为恒等，行为与 v1 一致。"""
    lim = RateLimiter(rpm=0, tpm=0, max_inflight=1)
    for _ in range(50):
        lim.report_success()
    assert lim._limit == 1
    lim.report_congestion()
    assert lim._limit == 1


@_asyncio_mark
async def test_slot_feed_success_on_clean_exit():
    lim = RateLimiter(rpm=0, tpm=0, max_inflight=2, aimd_increase_after=1)
    lim._limit = 1
    async with lim.slot():
        pass
    assert lim._limit == 2
    assert lim._inflight == 0


@_asyncio_mark
async def test_slot_feed_congestion_on_429():
    lim = RateLimiter(rpm=0, tpm=0, max_inflight=4)
    req = httpx.Request("POST", "http://upstream.invalid/embeddings")
    resp = httpx.Response(429, request=req)

    with _raises(httpx.HTTPStatusError):
        async with lim.slot():
            raise httpx.HTTPStatusError("429", request=req, response=resp)

    assert lim._limit == 2
    assert lim._congestion_events == 1
    assert lim._inflight == 0


@_asyncio_mark
async def test_slot_ignores_semantic_4xx():
    """400 是**我们自己的问题**，降并发不会让它变好——不许喂拥塞信号。"""
    lim = RateLimiter(rpm=0, tpm=0, max_inflight=4)
    req = httpx.Request("POST", "http://upstream.invalid/embeddings")
    resp = httpx.Response(400, request=req)

    with _raises(httpx.HTTPStatusError):
        async with lim.slot():
            raise httpx.HTTPStatusError("400", request=req, response=resp)

    assert lim._limit == 4, "语义错误不该触发乘性降"
    assert lim._congestion_events == 0
    assert lim._inflight == 0


@_asyncio_mark
async def test_slot_feed_congestion_on_timeout():
    lim = RateLimiter(rpm=0, tpm=0, max_inflight=4)
    with _raises(httpx.ReadTimeout):
        async with lim.slot():
            raise httpx.ReadTimeout("Read timed out")
    assert lim._limit == 2


# ── 6. 定时唤醒是「解析求解」不是「盲查」 ────────────────────────────────


@_asyncio_mark
async def test_seconds_until_is_analytic():
    """1 rpm/60s = 1 笔/秒；桶空 → 还需约 1 秒。算得出来，才配叫「事件」。"""
    lim = RateLimiter(rpm=60, tpm=0, window=60.0)
    lim._req_tokens = 0.0
    delay = lim._seconds_until(0)
    assert delay is not None
    assert 0.95 <= delay <= 1.05, "解析解偏了：%r" % (delay,)


@_asyncio_mark
async def test_seconds_until_none_when_slot_exhausted():
    """等的是**并发槽**（对端何时返回不可预知）→ 算不出时刻，返回 None。

    这时不装定时器、改由 release() 事件驱动——这正是 v1 轮询想做而做不到的事。
    """
    lim = RateLimiter(rpm=6000, tpm=0, max_inflight=1)
    lim._inflight = 1
    assert lim._seconds_until(0) is None


@_asyncio_mark
async def test_no_leaked_timer_after_queue_drains():
    lim = RateLimiter(rpm=60000, tpm=0, max_inflight=1)
    await lim.acquire()
    assert lim._wake_handle is None
    lim.release()
    assert lim._wake_handle is None, "队列已空，不该留着空转句柄"


# ── 7. /health 口径 ──────────────────────────────────────────────────────


@_asyncio_mark
async def test_snapshot_reports_concurrency_block():
    lim = RateLimiter(rpm=2000, tpm=500000, max_inflight=4)
    snap = lim.snapshot()
    assert snap["rpm"]["limit"] == 2000
    assert snap["tpm"]["accounting"] == "char_estimate"
    con = snap["concurrency"]
    assert con["limit"] == 4
    assert con["max_inflight"] == 4
    assert con["inflight"] == 0
    assert con["waiters"] == 0
    assert con["policy"].startswith("AIMD")
    assert con["limit_history"] == [], "冷启动时还没有任何 AIMD 变动"


# ── 零依赖 runner（`python tests/test_rate_limiter.py`）─────────────────

if __name__ == "__main__":
    cases = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failures = 0
    for name, fn in cases:
        try:
            if asyncio.iscoroutinefunction(fn):
                asyncio.run(fn())
            else:
                fn()
        except BaseException as exc:      # noqa: BLE001 - runner 要如实报告每一例
            failures += 1
            print("  FAIL  %s\n         %s: %s" % (name, type(exc).__name__, exc))
        else:
            print("  PASS  %s" % name)

    print("\n%d/%d passed" % (len(cases) - failures, len(cases)))
    sys.exit(1 if failures else 0)
