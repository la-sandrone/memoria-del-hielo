# CHANGELOG

格式：按日期倒序。每条记「**为什么**」而不只是「改了什么」。

> **注**：`PROJECT_SPEC.md` / `scripts/init_db.sql` / `src/cli/mhi_dispatch.py` /
> `tests/test_text_trust_note.py` 的未提交改动属于更早的工作，不在本文件记录范围。

---

## 2026-10-27 — 攒批写入：6218 次连接 → 108 次，顺手治好 10048

### 动机

导入 Housecroft（3109 chunk）跑到 **608/3109** 崩在：

```
mariadb.OperationalError: Can't connect to server on '127.0.0.1' (10048)
```

**10048 不是"服务没起"。** 服务没起给的是 `10061 (ECONNREFUSED)`；10048 是
`WSAEADDRINUSE`——本地端口/四元组耗尽。这个区分决定了整条排查方向，查错方向会白烧一小时。

现场取证（都在本机实测）：

| 观测 | 值 |
|---|---|
| MariaDB 服务 | RUNNING（`sc query MariaDB12.3.2`） |
| 实际端口 | **9002**（`my.ini` 的 `port=`，不是 3306） |
| 指向 9002 的 TIME_WAIT | **474 条**（local 是 9002 → **服务端先关**） |
| 动态端口 | 49152–65535 = 16384，其中 ~800 被 Hyper-V/WSL 类预留 |
| `mariadb.err` | 无本次崩溃记录 → **服务端没崩，是客户端连不上** |

根因在 `_cache_set`：每个 chunk 一次 `execute_sql` → 每条语句一条新 TCP 连接。
3109 chunk 的书 = **读 3109 + 写 3109 ≈ 6218 次连接**；每次断开留一条 TIME_WAIT
（Windows 取 2×MSL = 120 s），把客户端的临时端口按四元组锁住两分钟。

**"即弃连接"并没有免除状态。** 它只是把状态从服务端挪进了内核，而那个状态
你没法 `rollback`，只能等。设计前提是"连接是稀缺资源、要长用"——短连接风暴下，
这份账两分钟要记几百次。

### 变更

**新增 `src/core/sqlbatch.py`（SqlBatcher）**

| 维度 | 做法 |
|---|---|
| 攒批 | N 行拼成一条多值 `INSERT … ON DUPLICATE KEY UPDATE` |
| 双上限 | 行数 `max_rows=256`；**语句字节硬上限 `max_sql_bytes=4 MB`**（**预测式**：加之前先算，超了先 flush） |
| 单行超限 | 抛 `SqlBatchTooLarge`，**不截断、不丢行、不静默降级** |
| 连接 | 每次 flush 走现有 `Database.transaction()`：一条连接、异常自动回滚 |
| 瞬时错误 | `10048/2002/2006/2013/1205/1213` 指数退避（0.5→8 s，5 次）；upsert 幂等，重试安全 |
| 包太大 | `1153` **不盲重试**（同一包重发必然再炸）→ 上限砍半 + 二分重发，行数不丢 |
| 包限自适应 | 首次 flush 前查 `@@session.max_allowed_packet`，硬上限压到 **75%**；**只压不涨** |

**`src/core/embedding.py`**

- `_cache_set` 从"执行一条 INSERT"改为**只入队**
- 新增 `_cache_get_many`：300 个 hash 一条 `IN` 查询（读侧同样批量化）
- `embed_one` 立刻 `flush_cache()`（单条语义不变）；`embed_batch` 每批 HTTP 后刷一次

**`src/core/db.py` 零改动** —— 连接语义、即弃策略、事务边界全部不动。

### 实测

| | 改前 | 改后 |
|---|---|---|
| 读缓存 | 3109 | ~11 |
| 写缓存 | 3109 | ~97 |
| **合计连接** | **6218** | **~108** |

- 服务端 `max_allowed_packet = 67108864`（64 MB）→ 4 MB 上限有 16 倍余量，**不用调**
- 重跑 Housecroft：**2477/2477 嵌入、3109 chunks 一次过**（就是这次事故的那本书）
- 离线单测 6 项全过：行数上限 / 字节上限预 flush / 单行超限抛错 / 1153 拆半 / 参数个数校验 / 探测只压不涨

### 两条留下来的判断

1. **瞬时错误 ≠ 确定性错误。** 10048/2013 可以退避重试；1153 重试只会再炸一次。
   把 1153 塞进 `RETRY_ERRNOS` 是最容易犯的错——会得到五次必然失败的重试。
2. **"即弃连接"不等于"一条语句一次连接"。** 前者是策略（不跨重复用），后者是实现细节。
   这次事故的教训是：策略没错，实现把它推到了极端，于是省下的服务端状态全变成
   内核里的 TIME_WAIT。

---

## 2026-10-25 — CJK 全文检索：jieba → unigram+bigram（附 `search_OMNIA`）

### 动机

三件事叠在一起，根因是同一个：**中文没有词边界，而索引是按词建的**。

1. `jieba` 的词典覆盖头部热词，但对古典学专名、自造词、跨语种音译，切分边界与真实查询不一致
   —— 实测约 **30% 的查询漏搜**，且不报错。
2. `innodb_ft_min_token_size` 默认 3，而它**按字符计**——1–2 字的中文词**全部进不了索引**。
   症状：`「会话」` 用 LIKE 能中，用 FULLTEXT 返回 0。
3. `SET GLOBAL innodb_ft_min_token_size=1` 返回成功但**毫无效果**：它是 startup-only 参数，
   必须改 `my.ini` 重启。

### 变更

| 层 | 改动 |
|---|---|
| 分词 | 新增 `tokenize_cjk_ub()` / `build_cjk_boolean()`：CJK 按**单字 + 双字**切，非 CJK 维持原分词 |
| 索引 | `my.ini` 加 `innodb_ft_min_token_size=1`（startup-only，重启生效） |
| 索引参数 | `innodb_ft_cache_size` 64M→**512M**、`innodb_ft_sort_pll_degree` 2→**8**（12 核里原来只用 2 核） |
| 查询 | `build_cjk_boolean("拜占庭")` → `+拜占 +占庭`（三字词在 bigram 索引里只剩两个 token） |
| 跨源 | 新增 `omnibus.py` + `search_OMNIA` / `message_context` 两个工具 |

### 实测

- `documents_book_chunk` 的 FULLTEXT 重建：**3 小时 10 分**（00:08→03:17）；
  日志显示 `Total InnoDB FTS size 1.7GB exceeds innodb_ft_cache_size 67108864`
  → 调参后预计降到 40–70 分钟
- 单字查询恢复：`绘` 5,976 命中；`拜占庭` → `+拜占 +占庭` 1,375
- 俄文走 `content` 列（非 CJK 空格分词）：`Византийская` 48.9 分，中文查询能命中俄文书对应章节

### 四条踩过的坑（MariaDB ≠ MySQL）

| 现象 | 真相 |
|---|---|
| `ALTER TABLE t DROP INDEX a, ADD FULLTEXT a(...)` 0 秒且无效 | 定义相同 → MariaDB 优化成**空操作**；必须拆两条语句 |
| `ALGORITHM=INPLACE` 期望不重建 | MariaDB 对 FULLTEXT **仍重建整表**，INPLACE 只保证并发 DML |
| `GRANT ... ON db.*` 明明有 DROP 却报拒 | `GRANT` 里 `_` 是**通配符**——`memoria_del_hielo` 与 `memoria\_del\_hielo` 两行并存，只命中其一 |
| `mariadb -N --raw` 输出用 `splitlines()` 切 | 正文含 `\x0c`/`\u2028` → 行数虚高 16 倍；改 `TO_BASE64` 通道传输 |

> 数据侧（`documents_book_chunk` 62.5 万行的 jieba→bigram）在「2026-10-25 — bigram 切换（影子表）」那条里记，本条只记查询侧口径。

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

---

## 2026-10-24 — 513 本古典学文献入库：tag 方案、幻觉注记与三个 CLI 陷阱

### 动机

本地语料目录下 514 个 zip 要一次性进库。三件事同时要定：
**tag 怎么表达归属**（决定以后能不能泛搜）、**幻觉要可查**（决定这些文字能不能信）、
**命令必须能跑完**（跑一半静默退出，比跑错更难查）。

### 变更

**tag 三级方案（两次返工）**

| 迭代 | 决定 | 代价 |
|---|---|---|
| v1 目录树三级（含书系名） | 层级太细，泛搜无益 | **296 本 tag 错位，返工** |
| v2 二级（一级 + 单元） | 保留归属、可泛搜 | — |
| v3 去掉总标签 `解析素材5`，追加宽 tag（`神话学、宗教学、人类学` / `历史`） | 总标签无检索价值 | — |

**幻觉注记**：`documents_book.errata_stats`（JSON）取代两个计数列
（`kangxi_radical_count` / `pua_count`，迁移 008 删除），判据与词表唯一权威源是
`src/core/text_errata.py`；新增 `mhi annotate` CLI 与 `scripts/backfill_errata_stats.py`。
**词表不落库**，所以不会出现"库里一份、代码里一份"的漂移。

**三个真 bug**（都属于"静默半途而废"）

1. tag 三级错位 → 296 本要按路径反推重算（`kb/repair_tags.py`）
2. **目录名含逗号** → `--tags a,b` 被当成分隔符，4 本进不去；加 `validate_single_tag`
3. **根散装文件分支无异常保护** → 一条命令整体中止；拆成逐文件导入（`kb/imp_rootfiles.sh`）

### 实测

- **514/514 入库**，注记 **345/345** 落库，五项验收全绿（`kb/acceptance.py`）
- 并发加到 **10 路**后撞上游 50 万字符/分钟配额（机器本身全闲、零锁错误）
- 动库前留 **4.54 GB** 全量备份 + 按 guid 精确回退脚本（`undo_import.sh`）

### 两条判断

1. **混排词指标退役**：它测的是**体裁**（词典类必然"混排"），不是质量，误伤全部词典。
2. **后台守护用 `systemd-run --user` 而不是 `nohup`**：DSH 重启会清掉
   `dsh-subprocess-*.scope` 的 cgroup，nohup 的 `PPID=1` 是假象，进程照样被杀
   —— 10-14 与 10-20 两次停机都是这个。

---
