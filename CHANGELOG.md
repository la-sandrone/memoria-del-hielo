# CHANGELOG

格式：按日期倒序。每条记「**为什么**」而不只是「改了什么」。

> **注**：`PROJECT_SPEC.md` / `scripts/init_db.sql` / `src/cli/mhi_dispatch.py` /
> `tests/test_text_trust_note.py` 的未提交改动属于更早的工作，不在本文件记录范围。

---

## 2026-10-25 — server_bge v2：事件叫醒排队 + AIMD 自适应并发

### 动机

v1 的 `RateLimiter.acquire` 是这样的：

```python
async with self._serial:          # 全局锁
    while True:
        self._refill()
        if req_ok and tok_ok: … return
        await asyncio.sleep(0.05)     # ← 持锁轮询
```

两个病，是同一件事的两面：

1. **等待期间不释放锁**——`_serial` 被当成「闸门 + 睡眠室」，所有其他请求只能在锁上盲等。
2. **每 50ms 醒一次盲查**——没有「资源可用」这个事件，只能定时自问。

对照 NT 的 IRP 队列：IRP 的真实形态是「进队列 → `STATUS_PENDING` → 资源就绪时
**`KeSetEvent`** → 完成」，**不是每 50ms 自己问一次能不能走**。v1 是后者。

而 asyncio 的事件循环本身也不是「队列消费循环」——它是**派发表**（与 x86 的 IDT、
NT 的 SSDT 同构：一张表 + 一个分发器 + 一个出让机制）。派发表上的正确等待原语是
`Future`，不是 `sleep`。

### 变更

**`RateLimiter` 重写（`server_bge.py`）**

| 维度 | v1 | v2 |
|---|---|---|
| 等待 | `while + sleep(0.05)` 轮询 | `await Future`，`_pump()` 里 `set_result` 叫醒 |
| 公平 | `asyncio.Lock`（近似 FIFO） | 显式 `deque` 队列，**严格不跳号** |
| 锁 | 必须有（等待在锁里） | **整个删掉**（临界区全是同步语句，单线程天然原子） |
| 唤醒时机 | 固定 50ms 心跳 | **解析求解**「还需多少秒」后 `call_later`；算不出就不装定时器 |
| 并发 | 恒为 1（串行闸门——当年没有令牌桶时的权宜之计） | AIMD，起点 = `REMOTE_MAX_INFLIGHT`（16），管在途数 |
| 503 | 无建议 | `Retry-After` + `retry_after_sec` |

**关键判断：队首在等「并发槽」时不装定时器。** 对端何时返回不可预知 → 时刻算不出来 →
改由 `release()` 事件驱动。这是「事件对象」与「轮询」的本质区别：能算的算，不能算的等。

**AIMD（新增，与令牌桶正交）**

- 令牌桶管**速率**（RPM/TPM，配额硬约束，不能违反）
- AIMD 管**同时在途数**（`_inflight` vs `_limit`）
- 起点 = 天花板（「先开到指定的最高」），撞 429/超时 ×0.5（下限 1），连续成功 8 笔 +1
- 判据只看**最终**结果：`_embed_remote` 内部的 429 退避重试成功**不算**拥塞
- 4xx 语义错误（400/401/413）**不喂**拥塞信号——降并发不会让它变好
- `REMOTE_MAX_INFLIGHT=1` 时 AIMD 退化为恒等（完全串行）；默认 **16**
  ——「一路去」指**拓扑**（所有远端流量汇聚到一个出口拿真实配额数据），
  不是「一次只准发一个包」。串行是当年没有令牌桶时的权宜之计（用户 2026-10-25 原话：
  「本来这个设计是为了强行满足 RPM 和 TPM 设计要求的」），令牌桶成型后即放开。
  **实测对比**：`1` → 6.7 笔/秒（402 RPM，只用到 20% 配额）；`16` → 32.0 笔/秒
  （逼近令牌桶上限 33.3，`congestion_events=0`）

**端点改造**：两处 `await limiter.acquire(...)` →
`async with limiter.slot(tokens=...)`，`finally` 保证 `release()`，并发槽不会漏。

### 测试

`tests/test_rate_limiter.py`：21 例，**零依赖**（不 import pytest——本仓 serv-venv 全没装），
既能 `python tests/test_rate_limiter.py` 直接跑，也能被 pytest 收集。覆盖：

严格 FIFO 不跳号 / 超时离队 / 取消不留僵尸 / 并发槽不漏 / AIMD 升降与下限 /
429 与 400 的判据分叉 / `_seconds_until` 的解析性 / 队列空不留定时器 / `/health` 口径。

集成冒烟：`httpx.MockTransport` 假上游，验证 `slot()` 真被调用、200 后
`acquired_total=1` 且 `inflight=0`、429 后 `congestion_events=1` 且 `max_inflight=4 → limit=2`。

### 生效条件

**运行中的进程是旧代码。** `~/ai-services/server_bge.py` 是指向本仓的 symlink，
但已启动的 uvicorn 不会重载模块——**需重启 `server_bge` 两个端口上的进程**才生效。

---

## 2026-10-25 — bigram 切换（影子表）、server_bge 超窗策略、多语言修正

### 动机

1. **jieba 分词的漏搜**：与 melquiades 同因——词典覆盖头部，古典学专著里的
   专名、自造词、术语切错边界。
2. **`server_bge` 对超长文本静默截断**：5 个副本（主文件、公开发布版、3 个 `.bak`）
   全都没有截断/分块逻辑，只声明 `max_length: 8192`。调用方以为向量覆盖了全文，
   实际尾部被丢且无任何报错。
3. **非 CJK 分支只认 ASCII**：西里尔/希腊/带变音拉丁整段丢弃（古典学材料里成片）。

### 变更

**`server_bge.py`：超窗三档策略**

```python
EMBED_OVERFLOW_MODE   = os.getenv("EMBED_OVERFLOW_MODE", "reject")   # reject|average|truncate
EMBED_WINDOW_RATIO    = float(os.getenv("EMBED_WINDOW_RATIO", "0.85"))
EMBED_OVERFLOW_STATUS = 413   # HTTP 413 Content Too Large（RFC 9110 §15.5.14）
```

| 档 | 行为 |
|---|---|
| `reject`（默认） | 超窗直接 **413**，detail 写清 token 数 vs 窗口。**让调用方自己决定怎么切**——静默截断是最坏的一种"帮你" |
| `average` | 按窗口 85% 切块 → 各块嵌入 → **按字符数加权平均** + 重新 L2 归一化 |
| `truncate` | 旧行为，仅兼容保留 |

切点**吸附到字素簇边界**（UAX #29，`regex` 的 `\X`）——直接按字符切会把
emoji ZWJ 序列（`👨‍👩‍👧‍👦` 是 7 个码点）、组合重音符、天城文连字切成两半。

依据：Late Chunking（arXiv:2409.04701）明确把「split → encode → **mean pooling**」
当作既有实践。按长度加权是其变体。

**数据迁移：`documents_book_chunk` 影子表切换（jieba → bigram）**

- 62.5 万行 / 17 GB，**102.2 分钟**完成
- 方案：建影子表（同结构、无 FULLTEXT/VECTOR 索引）→ `INSERT ... SELECT` 搬数据
  → Python 只补 `content_tokenized` 一列 → 建索引 → RENAME
- **为什么不用「摘索引→改数据→建索引」**：MariaDB 对 FULLTEXT 的 DDL **一律 rebuild
  整表**（INPLACE 只省锁不省 IO），那样要复制表两趟（实测 91 min + 82 min）
- 旧表已 DROP，释放 **16.9 GB**（总库 32.6 → 15.69 GiB）
- 断点续传靠 `_shadow_progress` 水位线表（Windows 自动更新杀过一次后台，重跑无损续上）

**查询侧切 bigram**

- `src/core/tokenizer.py`：`tokenize_cjk_ub()` / `build_cjk_boolean()`；
  非 CJK 分支扩到全 Unicode 字母
- `src/core/search.py`：CJK 走 `build_cjk_boolean`，非 CJK 仍走原分词
- `src/importers/{book,chat,onenote}_importer.py`：写入侧改 `tokenize_cjk_ub`

### 数据库调优（与 melquiades 同一实例）

- `innodb_buffer_pool_size` 8G → **16G**（数据 15.69 GiB，已能全放下）
- `long_query_time` 1 → **3 s**（对「单次 2–12 秒是常态」的查询，1 秒等于没设）

### 文档

`README.md` 加了三段警告：① Zipf 长尾 + Unicode 边缘情况 ② 数据量已到中小型网站
级别、请严肃对待性能与 MariaDB/MySQL 差异 ③ 向量检索是近似的（ANN）。

工具描述（`search_docs` / `find_books`）补了 ANN 与多语言边界。


### ⚠️ 大坑：带 FULLTEXT 的表，改列注释也要重建整表

**症状**：想改 `content_tokenized` 的列注释（`jieba` → `bigram`），
`ALTER TABLE ... MODIFY COLUMN ... COMMENT '...'` 卡住/重建十几 GB。

**实测对比**：

| 表 | `ALGORITHM` | 结果 |
|---|---|---|
| **无 FULLTEXT**（试探用） | `INSTANT` | ✅ 成功，**25.9 ms** |
| **有 FULLTEXT**（真实表） | `INSTANT` | ❌ `ERROR 1846` |
| 同上 | `NOCOPY` | ❌ `ERROR 1846` |
| 同上 | `COPY` | ✅ 但**重建整表 + 重建 FTS**（17 GB → 1–1.5 小时） |

**错误信息里的 Reason 才是关键**：

```
ALGORITHM=INSTANT is not supported.
Reason: InnoDB presently supports one FULLTEXT index creation at a time.
Try ALGORITHM=COPY
```

**不是「改注释要重建」，而是「表上有 FULLTEXT 时 InnoDB 的 ALTER 实现不支持
非 COPY 算法」**——跟注释内容、跟列类型都无关。这是 MariaDB 的实现限制。

**结论 / 纪律**：

- ❌ **不要为了改一个注释单独跑 `MODIFY COLUMN`**
- ✅ **攒着**：下次真要动这张表时（改列类型、加索引、分词口径再变），
  把 `MODIFY COLUMN ... COMMENT` 一起写进**同一条 ALTER**，一次 COPY 全解决
- ⚠️ 也别想着「先删 FULLTEXT 再改」——删 FULLTEXT 本身也要 rebuild（实测 91 分钟），
  两头都亏

**当前状态**：`documents_book_chunk.content_tokenized` 的注释仍是旧文案
（`应用层 jieba 分词结果`），**有意不修**，等下次重建时合并处理。
（`draftpad` 的同名列注释已是新文案——那张表小，之前顺手改过。）

### 已知边界（写清不修）

| 问题 | 现状 |
|---|---|
| 向量路是 HNSW 近似 | 62.5 万点实测 top-1 92%、top-10 100%；`mhnsw_ef_search` 20→400 无效；**不可用于精确查找/判重** |
| 俄希拉丁在 `content_tokenized` 里为空 | **不影响查询**——非 CJK 走 `content` 列，不碰 tokenized（实测 `Византийская` fulltext 命中 48.9） |
| 跨语言语义检索 | 向量路承担。实测中文查「拜占庭帝国的体育教育与竞技运动」→ 命中俄文书《Физическая культура и спорт в Византийской империи》对应章节 |
| 希腊文/拉丁文屈折变化（`Πλάτων` vs `Πλάτωνος`） | 需词形还原，属独立工程，**以后再说** |
| `content_tokenized` 列注释仍写「jieba」 | 改列注释要 `MODIFY COLUMN`（大表重建），不急 |
