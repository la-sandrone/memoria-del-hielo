# Memoria del Hielo — 操作手册与检查单

> 版本：v1.0 / 对应架构规格书 §1–§18

---

## 1. 前置依赖检查单

| # | 项 | 验证方法 | ✅ |
|---|-----|---------|---|
| 1 | Python 3.12+ 虚拟环境 | `python --version` | ☐ |
| 2 | MariaDB 12+ 运行中 | `mariadb --version` | ☐ |
| 3 | mariadb（DB 驱动） | `python -c "import mariadb"` | ☐ |
| 4 | httpx + requests | `python -c "import httpx, requests"` | ☐ |
| 5 | jieba / numpy / Pillow | `python -c "import jieba, numpy, PIL"` | ☐ |
| 6 | chardet / regex | `python -c "import chardet, regex"` | ☐ |
| 7 | click / rich | `python -c "import click, rich"` | ☐ |
| 8 | **MCP 是两个包**：mcp + fastmcp | `python -c "import mcp, fastmcp"` | ☐ |
| 9 | PyMuPDF（可选，导入 PDF；懒加载） | `python -c "import fitz"` | ☐ |
| 10 | ebooklib（可选，导入 EPUB；懒加载） | `python -c "import ebooklib"` | ☐ |
| 11 | server_bge.py 运行中 | `curl http://localhost:10885/health` → `{"status":"ok"}` | ☐ |
| 12 | WSL 已挂载 `D:` 盘 | `ls /mnt/<drive>/` 能看到 Windows D 盘 | ☐ |

> 💡 核心依赖一次装齐：
> `uv pip install mariadb httpx requests numpy Pillow jieba chardet regex click rich mcp fastmcp`
> （分组与可选依赖见 `pyproject.toml`：PDF/EPUB 是 extra，两个常驻服务的依赖在 `embed` / `ocr` extra 里。
> ⚠️ `mcp` 与 `fastmcp` **是两个包**，只装前者时 MCP 服务起不来——这里有过前科。）

---

## 2. 数据库初始化

```sql
mariadb -u root -p

CREATE DATABASE IF NOT EXISTS memoria_del_hielo
  CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci;

CREATE USER IF NOT EXISTS 'mhi_user'@'%' IDENTIFIED BY '<你的密码>';
GRANT ALL PRIVILEGES ON memoria_del_hielo.* TO 'mhi_user'@'%';
FLUSH PRIVILEGES;

USE memoria_del_hielo;
SOURCE <project-root>/scripts/init_db.sql;
```

### 验证

```sql
SHOW TABLES;
-- 应看到 6 张表：
--   documents_book, documents_chat, documents_onenote,
--   images, category_config, bucket_config

SELECT * FROM category_config;
-- 3 行：book / chat / onenote

SELECT * FROM bucket_config;
-- 5 行：图书 / 游戏剧情 / AI对话 / OneNote笔记 / 剪报
```

---

## 3. 环境变量

项目读取环境变量 `MHI_DB_PASSWORD` 作为数据库密码（兜底）。建议设置：

```bash
# ~/.bashrc 或 ~/.zshrc
export MHI_DB_PASSWORD='你的数据库密码'
```

---

## 4. 配置检查

`config.toml` 内容示例（所有值都有默认值，不必须修改）：

```toml
[database]
host = "127.0.0.1"
port = 3306
user = "mhi_user"
password = ""      # 留空则读 MHI_DB_PASSWORD 环境变量
database = "memoria_del_hielo"

[embedding]
endpoint = "http://localhost:10885"
model = "BAAI/bge-large-zh-v1.5"

[search]
default_top_k = 10

[image]
everything_endpoint = "http://localhost:60001"
image_store_base = "<images 存放目录>"
```

---

## 5. 导入数据

### 图书 / 游戏剧情

```bash
# 单文件
python -m src.cli.mhi_import_book --path <书库>/红楼梦.md

# 目录（递归）
python -m src.cli.mhi_import_book --path <书库>/ --recursive

# 指定桶
python -m src.cli.mhi_import_book --path ... --bucket 游戏剧情
```

### AI 对话

```bash
# 单文件
python -m src.cli.mhi_import_chat --path <对话目录>/2026-07-20.md

# 目录
python -m src.cli.mhi_import_chat --path <对话目录>/ --recursive

# 仅清洗不入库（调试用）
python -m src.cli.mhi_clean_chat --input /path/to/file.md --with-summary
```

### OneNote 笔记

```bash
# 单文件
python -m src.cli.mhi_import_onenote --path <OneNote导出目录>/笔记本/分区/笔记.md

# 目录
python -m src.cli.mhi_import_onenote --path <OneNote导出目录>/ --recursive

# 关闭自动 tag
python -m src.cli.mhi_import_onenote --path ... --no-auto-tags
```

---

## 6. 搜索

```bash
# 全库搜索
python -m src.cli.mhi_search --query "神经网络"

# 限定类别
python -m src.cli.mhi_search --query "transformer" --category book

# 限定桶
python -m src.cli.mhi_search --query "Redis" --bucket "AI对话"

# 限定 tag
python -m src.cli.mhi_search --query "缓存" --tags "性能,架构"

# 仅全文或仅向量
python -m src.cli.mhi_search --query "bug" --fulltext-only
python -m src.cli.mhi_search --query "内存泄漏" --vector-only

# 更多结果
python -m src.cli.mhi_search --query "分布式" --limit 20
```

---

## 7. 启动 MCP Server

```bash
# stdio 模式（默认）
python -m src.mcp_server

# 或显式
python <project-root>/src/mcp_server.py
```

### 验证

```bash
# 健康检查 - 发一个初始化请求（MCP 的 initialize）
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"0.1.0","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' | python -m src.mcp_server

# 应返回 JSON-RPC 响应
```

---

## 8. Tag 管理

```bash
# 查看文档 tag
python -m src.cli.mhi_tag --guid <uuid> --category book

# 添加 tag
python -m src.cli.mhi_tag --guid <uuid> --category book --add "技术,数据库"

# 移除 tag
python -m src.cli.mhi_tag --guid <uuid> --category chat --remove "旧标签"
```

---

## 9. 图片清理

```bash
# 列出孤儿图片（ref_count=0）
python -m src.cli.mhi_cleanup_images

# 确认删除
python -m src.cli.mhi_cleanup_images --yes
```

---

## 10. 快速测试（冒烟测试）

```bash
# 1. 运行单元测试
python -m pytest tests/          # ⚠️ 必须用装了依赖的解释器（本项目在 uv venv 内跑：153 passed）

# 2. 搜索一个存在的中文词（验证 jieba + CJK 全文搜索）
python -m src.cli.mhi_search --query "测试" --limit 3

# 3. 搜索一个英文词（验证非 CJK 全文搜索）
python -m src.cli.mhi_search --query "python" --limit 3

# 4. 确认 content_tokenized 有内容
mysql -e "SELECT guid, LEFT(content_tokenized, 100) FROM memoria_del_hielo.documents_chat LIMIT 3;"
# 应看到空格分隔的 jieba 分词结果，如 "我 是 一个 测试"
```

---

## 11. 架构概览

```
用户输入
  │
  ├─ CLI    → python -m src.cli.mhi_*
  ├─ MCP    → FastMCP Server (stdio) → AI 客户端
  │
  ├─ core/tokenizer.py  → has_cjk() + tokenize() + tokenize_query()
  ├─ core/search.py     → SearchEngine.search() — CJK 感知双路
  │    ├─ CJK 查询     → content_tokenized 列（jieba 分词）
  │    ├─ 非 CJK 查询  → content 列（空格分词）
  │    ├─ 向量搜索     → VEC_DISTANCE_COSINE on embedding
  │    └─ 权重合并     → CJK: 向量0.8/全文0.2, 非CJK: 全文0.6/向量0.4
  │
  ├─ importers/         → 三类导入器（book/chat/onenote）
  ├─ cleaners/          → AI对话清洗器
  └─ MariaDB            → 6 表，三主表 + images + 配置 + 外键
```

---

## 12. 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| `FUNCTION ngram is not defined` | MariaDB 无 ngram parser（MySQL 才有） | 移除 `WITH PARSER ngram`，改用 `content_tokenized` 列 + jieba |
| `Column count doesn't match` | INSERT 列数不完整 | 显式列出列名：`INSERT INTO tbl (col1, col2) VALUES (?, ?)` |
| 中文搜索无结果 | FULLTEXT 没 tokenize + 搜错了列 | CJK 查询需走 `content_tokenized` 列 |
| `VECTOR 类型不存在` | MariaDB 版本 < 11.7 | 升级到 MariaDB 11.7+ |
| BGE 嵌入失败 | server_bge.py 没启动 | `python /path/to/server_bge.py` |

---

*维护者：AI 协作者 / <user>*
*文档版本：v1.0 / 2026-07-24*

---

## 13. 矿坑记录

### 13.1 MinerU ModelSingleton 内存泄漏（2026-07-24，2026-07-24 修正诊断）

**现象**：批量解析 PDF 时内存（系统 RAM，非显存）持续增长，像内存泄漏。设 `TMPDIR` 无效。

**根因**：MinerU VLM/hybrid 后端使用 `ModelSingleton` 单例缓存模型实例。
`clean_memory()` 只清 PyTorch 缓存，**不释放单例中的 VLM 模型引用**。
每次 PDF 解析完成后模型权重 + KV cache + lmdeploy 内部状态永远驻留，只增不减。

这是官方已知设计限制——Issue #4243，状态 `closed as not_planned`。
lmdeploy 后端和 vllm 后端都存在此问题，本质在 `mineru/backend/vlm/vlm_analyze.py` 的 ModelSingleton。

**有效方案**：每次处理一个 PDF 后**退出进程**，让 OS 回收全部内存。

- **CLI 模式**（推荐）：`mineru -p file.pdf -o out --backend hybrid-engine`，每次启动新进程
- **批量脚本**：`mineru_batch.py v3.0` — 每个 PDF 启动独立 `wsl bash -c "mineru ..."` 子进程
- **两进程模式**：`mineru-vllm-server` 独立进程 + `vlm-http-client`，推理进程挂掉重启不影响主服务
- **兜底**：定期重启 `mineru-api` 服务

**注意**：`/tmp` 问题（§13.1 旧诊断）是次要的——`mineru-api` 的 `fast_api.py` 只用 `tempfile.mkstemp` 做临时 ZIP，用完即删。真正的泄漏在 ModelSingleton，不是文件系统。

### 13.2 lmdeploy KV cache 逐页泄漏 — 单书 40GB+（2026-07-24）

**现象**：一本页数较多的书（如 400 页）在 MinerU VLM 解析期间直接吃掉 40GB+ 系统内存，
三本就 OOM。

**根因**：不是模型权重泄漏——权重是只读的，加载一次不再增长。泄漏的是 **lmdeploy 的
KV cache block table**。推断链路：

```
for page in pages:                    # 遍历 PDF 每一页
    vlm.inference(page_image)         # VLM 前向推理
    → lmdeploy 分配一批 KV cache blocks
    → 推理结束
    → ? 应该调 free_kv_cache() 归还 blocks
    → 实际：没调。blocks 标记为"使用中"，永不归还

下一页 → 新分配 blocks → 旧 blocks 还在
400 页 → 400 轮分配 → 物理内存爆炸
```

正常使用 lmdeploy 时，KV cache 是请求级的——请求结束，blocks 归还到 pool。
MinerU 的 VLM pipeline 把每页当作一次独立推理，但**没有在单页推理结束后归还 blocks**，
lmdeploy 以为请求仍在进行中。

**为什么 CLI 模式不受影响**：进程退出 → OS 回收整个地址空间 → block pool 连锅端。
每次起新进程 = 全新的 block pool = 不会累积。

**为什么三本就炸**：一本 400 页的书 ≈ 每页几百 MB 的 KV cache × 400 = 几十 GB。
三本 = 三个几十 GB 叠加 = 物理内存耗尽。

### 13.3 彩色进度条被管道吃掉

**现象**：`mineru_batch.py v3.1` 改用 `Popen(stdout=PIPE)` 逐行转发后，MinerU 的绿色 ✓/红色 ✗/进度条全部褪色。

**根因**：MinerU（以及绝大多数 CLI 工具）通过 `os.isatty(sys.stdout.fileno())` 检测输出端。Popen 的 `stdout=PIPE` 不是 TTY → 程序自觉关闭 ANSI 颜色。

**当前状态**：搁置。功能正常，纯美观问题。可能的修复方向：
- `pexpect` / `pty.spawn()` 伪终端——但 WSL 跨边界 + Windows 下 pty 支持不稳定
- 不做管道转发，直接让 `wsl.exe` 接管当前终端——但会丢失输出捕获（无法做 timeout/错误摘要）
- 接受现状——这是 UNIX 管道的四十年老问题，不值得为颜色引入 pty 的复杂度

### 13.4 API 事实报废的荒诞闭环（2026-07-24）

**完整逻辑链**：

```
MinerU 设计了一套 API（mineru-api）
    │
    └─ 官方推荐：常驻进程，模型加载一次，批量提交，省加载开销
         │
         └─ 实测：§13.1 模型引用不释放 + §13.2 KV cache 逐页泄漏
              │         = 三本 OOM
              │
              └─ 用户报 Issue #4243 → 官方回复：not_planned
                   │
                   └─ 我们被迫回到 CLI（mineru -p file -o out）
                        │
                        └─ CLI 每次启动新进程，OS 在进程退出时全量回收
                             │
                             └─ 模型每次重新加载（从磁盘到内存）
                                  │
                                  └─ 反而正常。因为 OS 的进程边界
                                      比 MinerU 的 clean_memory() 可靠一万倍
```

**讽刺的核心**：API 模式的设计目标（省模型加载时间）→ 因同一条代码路径上的
内存泄漏而不可用 → 被迫回到每次重新加载模型的 CLI 模式 → API 想解决的那个问题
不但没解决，API 本身成了更大的问题。

**教训**：不是 Python/GC 的问题——是谁申请谁释放这条工程纪律，在 GC 语言里同样
是铁律。ModelSingleton 申请了模型引用和 KV cache，把释放责任推给 GC，GC 看到
引用还在（单例），当然不动。"GC 会处理一切"的幻觉在这条问题链上从头炸到尾。

**MinerU 的后台处理机制导致我们事实报废了它的 API 模式，转而使用每次重新启动
WSL 子进程的 CLI 模式——这一决策的核心逻辑记载于本条矿坑记录。**

---

## 14. 迁移记录

> ⚠️ 下面这些迁移的效果**已全部并入 `scripts/init_db.sql`**（2026-09-21 与现网库逐列校对）。
> 迁移脚本本身记录的是"当时怎么改的"，属作者个人项目历史，**未随发布版提供**——
> 全新安装只需跑 `init_db.sql` 一个脚本。本节留作变更沿革备查。

### 14.1 migrate_001_chunk.sql（2026-08-27）

chunk 层建表：documents_book_chunk（双 FULLTEXT + VECTOR HNSW + FK CASCADE，
source_path_hash 幂等锚点在 documents_book）。已执行。

### 14.2 migrate_002_chunk_offsets.sql（2026-08-28）

切块政策 v2 加列：char_len / visible_start / visible_len / byte_start / byte_len /
grapheme_len / has_pua（三偏移四长度 + PUA 标志）。已执行（ALTER 成功，7 列落位）。

- 存量行新列默认 0/0，**存量重导策略待定**（用户拍板后执行，勿手改数据）
- 新代码路径：`core/unicode_utils.py` + `core/chunker.py` v2（净化入口、可见口径、
  字素簇对齐、精确偏移）
- 新依赖：`regex>=2024.5.15`（UAX#29 字素簇，ai-services 已装 2026.5.9）
- 检索回查：search.py / mcp_server.py 已改 `IF(has_pua, SUBSTRING(...), content)`
- 验证：pytest 36/36；真实《普通地质学》干跑 270 chunks，max visible=2500 精确命中护栏

### 14.3 migrate_003_page_footnotes.sql（2026-08-28）

MinerU 脚注表（discarded_blocks 底部回收）：book_guid / page_idx / footnote_text /
footnote_type / bbox，UNIQUE 锚点 + INSERT IGNORE 幂等（force 重导不重复）。已执行。

### 14.4 migrate_004_chunk_status.sql（2026-08-28）

二段事务状态机加列：`status`（processing/done）+ `expected_chunks`（校验锚点）。
已执行。幂等检查 = status='done' **且** 实际 chunks == expected_chunks 才跳过；
processing（一段后崩溃）/ 破损（done 但 chunks 数不符，如 commit 响应丢失）→ 删旧强制重导。

### 14.5 删除 documents_book.content_tokenized（2026-08-28，root ALTER）

死列：书表无全文索引、检索只用 chunk 表的 content_tokenized。删除后单行
（content 全文）不再被 jieba 分词膨胀一倍。代码同步：记账 INSERT 去列 + 删全书
jieba 计算；init_db.sql 同步。无 migrate 文件（ALTER 已直改）。

---

## 15. 2026-08-28 会话变更与新增用法

### 15.1 CLI

- **mhi-import-book** 新增选项：
  - `--parallel N`（默认 3，书级并行线程池；`1` = 串行 + 阶段 `\r` 进度）
  - `--no-dir-tag`（禁止一级子目录自动 tag）
  - `--exclude-dirs 名1,名2`（排除一级子目录）
  - `--tags` 本批叠加（与子目录 tag 合并为 "来源,学科"）
  - 进度显示：tty → **rich 表格**（总进度条 + 每本书一行 + 阶段内进度条）；非 tty / rich 缺失 → 纯文本降级
  - **Ctrl+C 两级**：第一次 = 停当前书（worker 边界检查 stop_check，事务回滚，未启动取消）；第二次 = os._exit 立即全停
- **mhi-remove-book**：`mhi-remove-book [<path>] [--guid G1,G2] [--yes] [--dry-run]`
  - 两种选择模式**互斥**，必须给其一：`<path>`（文件 = 精确匹配；目录 = 前缀匹配，`LOCATE` 避开 LIKE 反斜杠转义坑）／`--guid`（逗号分割、可重复，主键精确匹配，大小写不敏感）
  - guid 未命中：命中 0 个 → exit 1；部分未命中 → 列清单，**须加 `--yes`** 才继续
  - 预览行：guid + key_metadata + chunks + source_path（guid 模式必须靠路径二次核对）
  - chunks 级联删除、page_footnotes 级联 + 显式删双保险、images 保留（全局去重表，重导 MD5 复用）
  - 内部单轨：两模式统一「先解析成 guid 列表 → 再按 guid 删除」（`_build_delete_statements`）
- 全部 CLI 补齐 `__main__` 入口（`python -m src.cli.xxx` 可执行）

### 15.2 导入机制

- **二段事务状态机**：一段记账（INSERT book，status='processing' + expected_chunks）
  → 二段（chunks 分批 200 行/批 + UPDATE status='done'，同一事务原子）
- **幂等**：done 且 actual == expected 才 skip；processing / 破损 → 删旧强制重导
- **即弃连接**（db.py 重写）：无连接池。`execute_sql()` 每次新建连接用完即焚、
  `transaction()` 每事务一连接。连接池/信号量/烂连接/占满告警全部移除。
  max_allowed_packet 由服务端控制（mariadb 驱动握手自动跟随，无需客户端参数）
- **图片判据**：MinerU `_content_list_v2.json` → block type 为
  image/equation_interline 的图跳过 OCR（但仍 MD5 去重/登记），
  table/chart 必 OCR；非 MinerU 书无 json 全部正常
- **OCR 语言降级链**：config `[ocr] lang_chain`（默认 `ch,en,la,el,ru`）——
  server_paddle 按链识别，首个文本非空且置信度 ≥ 阈值胜出（多模型懒加载）；
  图片未找到/OCR 失败改为书级汇总日志（不再逐张刷屏）

### 15.3 配置变更

- `[search] default_tags`：默认 tag fallback 链（显式 `--tags` > MCP `set_default_tags`
  会话级 > config 默认 > 不过滤）
- `[ocr] lang_chain` / `lang_confidence` / `skip_image_types`
- server_bge.py 顶部 `REMOTE_QUOTAS`：按模型 RPM/TPM（embed 与 rerank **独立令牌桶**，互不挤占）
- MariaDB `max_allowed_packet=64M`（my.ini `[mysqld]` 持久化）——巨书单行
  （content 全文 ~19MB）超 16MB 默认包限被服务端断连（104）

### 15.4 服务端部署

- **server_paddle.py**（仓库根目录）：`--port 9008（9007 被 DSH web 占用，勿用）/ --lang ch|en|la|el|ru /
  --device cpu|gpu / --cpu-threads（默认核数-1）/ --lang-confidence`；串行推理锁
  （排队超 120s → busy 503，客户端降级跳过该图下轮续跑）；`GET /health`
  - 本地起法：`server_paddle_wrap.sh`（激活 venv + 固定 9008 + 线程数）——本地环境脚本，未随发布版提供
  - paddlex 3.7.2 `rec_score` list bug 补丁：`patch_paddlex_recscore.py`（**重装 venv 后必须重打**）
    ——同属本地环境脚本，未随发布版提供。症状：多行文本块时 `rec_score` 是 `list[float]`，
    paddlex 直接拿它和阈值比较，抛 `TypeError: '>=' not supported between 'list' and 'float'`；
    补丁取 `max()`（任一行达标即保留该块）
- **server_bge.py**（仓库根目录）：REMOTE_QUOTAS 按模型限流（双令牌桶）；
  远端 429 退避重试（1s/2s/4s ×3）；串行 FIFO 转发；`GET /health`

### 15.5 注意事项

- **存量书 status**：状态机上线前导入的书仍为 'processing'（expected=0）——
  下一轮全量会强制重导。如不想重导，用 root 执行：
  ```sql
  UPDATE documents_book SET status='done',
    expected_chunks=(SELECT COUNT(*) FROM documents_book_chunk c
                     WHERE c.book_guid=documents_book.guid)
  WHERE status='processing';
  ```
- OCR 语言降级链只在首选语言空结果时触发（多数图首选直接命中，成本可控）
- rich 进度只在 tty 显示；MCP stdio / 重定向场景自动纯文本（stderr 安全）
- 并行导入进度由主循环 `time.sleep(2)` 轮询驱动（时间驱动刷新 + 可靠 Ctrl+C 接收点）；
  长任务书完成前每 2 秒重绘汇总行

---

## 16. 遗留记账（2026-08-29 登记 → 2026-09-21 已结清）

> 这两条在加 `mhi-remove-book --guid` 时实测发现，当时刻意不修以免范围膨胀。
> **2026-09-21 已全部处理**，结论留档（不再有"未处理"项）。

### 16.1 `page_footnotes` 外键：init_db.sql 与活库不一致 → 已补齐

- 当时状态：`init_db.sql` 声明了 `FOREIGN KEY (book_guid) REFERENCES documents_book(guid) ON DELETE CASCADE`，
  但**活库没有这条约束**——实测 `information_schema`：`memoria_del_hielo` 全库唯一外键是 `fk_chunk_book`，
  且 `page_footnotes` 的索引本身也已过时（`idx_book_page` → 实际是 `idx_footnotes_book` + `UNIQUE uq_footnote`，见 §14.3）。
- **处理（2026-09-21）**：先确认孤儿 footnotes = 0（引用完整性满足），再补齐约束：
  `ALTER TABLE page_footnotes ADD CONSTRAINT fk_footnotes_book FOREIGN KEY (book_guid)
  REFERENCES documents_book(guid) ON DELETE CASCADE;` —— 至此两侧一致。
- 代码侧不变：`mhi_remove_book._build_delete_statements` 与 `book_importer` 仍显式先删 footnotes。
  这是**双保险**：不依赖级联行为，对未打 FK 的历史库同样正确。

### 16.2 孤儿 footnotes 清理 → 已有脚本

- 历史 force 重导可能留下 `book_guid` 已不存在的 footnotes（当时靠 importer 手动清兜住）。
- 现状：`scripts/purge_orphan_footnotes.py`（默认 `--dry-run`，`--execute` 才真删）；
  实测当前 **0 条孤儿**（`LEFT JOIN documents_book ... WHERE b.guid IS NULL`）。
- 未做：`mhi-remove-book --orphan-footnotes` 这类 CLI 子命令——有独立脚本够用，暂不加。

---

## 17. Windows 编码行为备忘（控制台 / MSVCRT / Python）

> 起因：`mhi.cmd` 最初照抄了"保存代码页 → `chcp 65001` → 还原"的老套路。
> 后来发现两件事：**`chcp` 会顺带清屏**（chcp.com 的副作用），而且**根本没必要**。
> 把三层行为钉死在这里，免得后人（包括我）再折腾一遍。
> 起因之外的收获：这套结论是"官方文档 + 双场景实测"对上的，不是推测。

### 17.1 三层各管什么

| 层 | 行为 | 有损点 |
|----|------|--------|
| 控制台本体 | 内部就是 UTF-16；`WriteConsoleW`/`ReadConsoleW` 直接收发宽字符 | **无损** |
| 控制台的**字节**接口 | `WriteFile` / `WriteConsoleA` 写出的字节，按**控制台输出代码页**转回 UTF-16 显示 | 字符不在该 CP → 丢失/替换 |
| **MSVCRT stdio** | 默认是**字节模式**：连 `wprintf` 也要先用 CRT locale 把宽串转成多字节，再 `WriteFile` | **双重转码**，中间这层最冤 |

官方原文两条：

- MS 控制台代码页文档：*"It is recommended for all new and updated command-line
  applications to **avoid code pages and use Unicode**. UTF-16 formatted text can be
  sent to the W family of console APIs."* —— 字节接口才谈代码页，W 家族不谈。
- MS `_setmode` 文档：CRT 流默认不是 Unicode 模式，需显式 `_O_U16TEXT`/`_O_WTEXT`
  才能让 `wprintf` 走 Unicode（且窄函数在 Unicode 模式流上会断言）。
  → 病根是 **CRT 的字节取向**（对宽函数也先转码），不是控制台本体。控制台本体
  被"字节接口 + 代码页"这条兼容通道连累了。

### 17.2 Python 怎么替 CRT 擦了屁股（3.6+ / 3.7+）

| PEP | 结论 |
|-----|------|
| **PEP 528**（3.6，Final） | 流是**控制台缓冲区**（而非重定向文件）时，用 `_io.WindowsConsoleIO`：字节层要求 UTF-8 → 内部转 utf-16-le 交给 `WriteConsoleW`。**绕过 CRT，也绕过代码页**。`PYTHONLEGACYWINDOWSSTDIO` 可退回旧行为 |
| **PEP 529**（3.6） | 文件系统编码恒 UTF-8（实测 `sys.getfilesystemencoding()=utf-8`，与代码页无关）→ 中文路径读写两场景都 OK |
| **PEP 540**（3.7，Final） | `-X utf8` / `PYTHONUTF8=1`：stdio=UTF-8 + `surrogateescape`，`open()` 默认 UTF-8。**注意：`PYTHONIOENCODING` 对标准流的优先级高于 UTF-8 模式** |

关键分岔只看一个量：**`sys.stdout.isatty()`**

| 场景 | isatty | `stdout.encoding` | 打印中文/`⚠️` | 需要做什么 |
|------|--------|-------------------|---------------|-----------|
| 真控制台 | True | **utf-8**（PEP 528 生效，与 CP 无关） | 正常 | **什么都不用做**——不需要 chcp |
| 管道 / 重定向 | False | **gbk**（退回 locale 编码） | `UnicodeEncodeError` **当场崩** | 需要 `-X utf8` |

实测数据（Python 3.12.10，控制台 CP=936；`_enc_probe` 脚本，已删）：

| 场景 | plain（什么都不设） | `-X utf8` |
|------|--------------------|-----------|
| 真控制台（isatty=True） | enc=utf-8，中文/⚠️🔴 正常 | enc=utf-8，正常 |
| 管道（isatty=False，WSL 侧） | enc=gbk，打印 U+26A0 **崩溃** | enc=utf-8，正常 |
| 控制台 CP（ctypes 只读探测） | 936 / 936（未变动） | 936 / 936（未变动） |

### 17.3 本项目的规矩

1. **批处理脚本（`.cmd`）不调用 `chcp`**：会清屏 + 改共享控制台状态（其他进程也受影响）。
   编码交给 Python。
2. **Windows 侧入口一律带 `-X utf8`**：交互式无所谓，**管道/重定向是刚需**（否则中文提示直接崩）。
   用命令行开关而不是 `PYTHONIOENCODING` —— 不改子进程 env。全局 `PYTHONIOENCODING`
   若存在会盖过它（PEP 540 明文），排查编码问题时先看这个变量。
3. **`.cmd` 内容保持纯 ASCII**：cmd 按*当前*代码页逐行解析批处理，注释里的非 ASCII
   字节会被误读（尾字节还可能与 ASCII 配对吃掉字符）。
4. **WSL 侧没有这回事**：Linux 无代码页概念，UTF-8 是默认。MCP server 走
   `mcp-stdio.sh`（WSL），不受本节影响。
5. 数据链路的编码是另一码事（txt 的 BOM→UTF-8→GB18030 检测、PUA 等长净化等），
   不属本节。

护栏：`tests/test_mhi_dispatch.py::test_mhi_cmd_never_calls_chcp_and_relies_on_python_encodings`
断言 `mhi.cmd` 里**没有 chcp 命令行**、**必须有 `-X utf8`**、纯 ASCII + CRLF。

### 17.4 C/C++ 侧的历史经验（为什么这坑长这样）

**C：能擦** —— 自己写 `printfw`，把"组串"和"输出"拆开，绕开 CRT 的字节路径：

```c
int printfw(const wchar_t *fmt, ...) {
    wchar_t buf[2048];
    va_list ap; va_start(ap, fmt);
    int n = _vsnwprintf(buf, _countof(buf), fmt, ap);
    va_end(ap);
    if (n < 0) { n = (int)_countof(buf) - 1; buf[n] = L'\0'; }  /* 截断兜底 */
    HANDLE h = GetStdHandle(STD_OUTPUT_HANDLE);
    DWORD written = 0;
    if (h && h != INVALID_HANDLE_VALUE &&
        WriteConsoleW(h, buf, (DWORD)wcslen(buf), &written, NULL)) {
        return n;
    }
    /* 非控制台（重定向/管道）：WriteConsoleW 会失败，必须回退 WriteFile + 自定编码 */
    return fallback_write_utf8(buf);
}
```

四个坑（都是"绕开 CRT"的连带代价）：

1. **`WriteConsoleW` 在输出被重定向/管道时会失败**（句柄不是控制台缓冲区）→ 必须回退
   `WriteFile` 并自己定编码。这就是 Python `isatty()` 分岔的同一件事（§17.2）——
   C 侧要手写，Python 已经内置。
2. **绕过 CRT 就失去文本模式的 `\n` → `\r\n` 转换**（`_setmode` 文档明文：*"Line feed
   characters are translated into CR-LF combinations on output"*）→ 不自己补 `\r` 就是台阶式错行。
3. **VS2005 时代的 `_vsnwprintf` 截断时返回负值且不保证补 NUL**（MS 文档把它列为
   "为向后兼容保留"，截断/NUL 保证是安全增强版 `_vsnwprintf_s` 才有的）→ 两趟调用或手工补终止符。
4. **与 CRT 的 `printf` 混用 = 两套缓冲** → 顺序会乱；直写控制台前先 `fflush(stdout)`。

**C++：`std::wcout` 擦不动，所以结论是"不用它"** —— 这不是工程量问题，是结构问题：

- `printfw` 能擦，是因为 **printf 是函数**，换掉调用点即可；`wcout` 是**对象**：转码发生在
  `codecvt` facet（默认 classic locale，非 ASCII 直接失败/丢字），输出落在
  `basic_streambuf`，要擦就得同时替换 facet 与 streambuf，且**接管不了第三方代码里的
  `operator<<`**。
- 更要命的是 `_setmode` 文档的 Caution：*"Unicode mode is for wide print functions …
  is not supported for narrow print functions. Use of a narrow print function on a Unicode
  mode stream triggers an assert."* —— `cout` 与 `wcout` 共用同一个 fd，**二者不能共存**。
  所以"别用 wcout"是结构上被逼出来的结论，不是偷懒。
- 佐证：libc++ 直到 **2023-03**（D146398）才修好"Windows 上流被配置为 `_O_U16TEXT`/
  `_O_WTEXT` 时 `std::wcout` 不可用"。

**今天在 C++ 里怎么写**：能用 `std::print`/`std::format`（C++23）就用（其 Windows 控制台
路径由标准库实现决定，别自己再糊一层）；否则走 UTF-8 路线（控制台 CP 设 UTF-8，或直接
`WriteConsoleW`），并把 `wcout` 当**禁用项**（约定 + CI grep 拦），而不是"小心使用"。
