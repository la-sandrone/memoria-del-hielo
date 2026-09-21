# Memoria del Hielo — 项目规格书

> **阅读对象**：实现本项目的 AI/LLM。本文是唯一权威的项目规格来源。
> **原则**：不确定任何 API 签名、SQL 语法、库版本时，先查文档再动手。不脑补。

---

## 1. 项目身份

| 项 | 值 |
|----|-----|
| 项目名 | Memoria del Hielo |
| 项目目录 | 本仓库根目录（下文出现的 `<project-root>` 均指它） |
| MCP 服务名 | `mcp-memoria.del.hielo` |
| 语言 | Python 3.12+ |
| 配置格式 | TOML（`config.toml`，项目根目录） |

---

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────┐
│                    MCP / CLI 入口层                      │
│  mcp-memoria.del.hielo (MCP Server)                     │
│  mhi_import_book / mhi_import_chat / mhi_import_onenote  │
│  mhi_clean_chat / mhi_search / mhi_tag                  │
├─────────────────────────────────────────────────────────┤
│                    业务逻辑层                             │
│  importers/   cleaners/   search_engine/   tag_manager/ │
├──────────────┬────────────────────┬─────────────────────┤
│   MariaDB    │   server_bge.py    │   server_paddle.py  │
│   12.3.2     │   BGE-M3 嵌入/重排  │   PaddleOCR 3.7.0   │
│ 127.0.0.1:   │  HTTP 9005/9006   │   HTTP / stdin      │
│    9002      │                    │                     │
├──────────────┴────────────────────┴─────────────────────┤
│              Everything HTTP API (图片查找)               │
│              localhost:9000/?search=...&json=1           │
└─────────────────────────────────────────────────────────┘
```

**数据流**：文件 → 清洗/提取 → MariaDB 入库（原文 + 关键元数据 + VECTOR 嵌入） → 全文搜索 / 向量搜索 → LLM

**三类别处理差异**：

| 类别 | 表 | 嵌入 | 全文搜索 | key_metadata |
|------|-----|------|---------|-------------|
| 图书 + 游戏剧情 | `documents_book` | ✅ BGE-M3 1024维 | ✅ jieba FULLTEXT | 书名（原始文件名） |
| AI对话 | `documents_chat` | ✅ BGE-M3 1024维 | ✅ jieba FULLTEXT | 对话摘要（自动生成） |
| OneNote笔记 + 剪报 | `documents_onenote` | ❌ 不需要 | ✅ jieba FULLTEXT | 标题优先，否则首50字 |

---

## 3. 外部服务

### 3.1 MariaDB

| 项 | 值 |
|----|-----|
| Host | 127.0.0.1（⚠️ 不能写 `localhost`——MariaDB 会解析到 Unix socket） |
| Port | 9002 |
| Database | `memoria_del_hielo` |
| 用户 | 专用用户，`'%'` 连接，仅对该库有权限 |
| 字符集 | `utf8mb4`，collation `utf8mb4_uca1400_ai_ci` |
| 全文搜索 | InnoDB FULLTEXT + jieba 分词（如 MariaDB 无 jieba 插件则用 `WITH PARSER ngram`，token_size=2） |
| VECTOR | `VECTOR(1024)`，HNSW 索引 `M=16 DISTANCE=cosine` |
| 密码 | 从环境变量 `MHI_DB_PASSWORD` 读取 |

> ⚠️ **每表只能建一个 VECTOR INDEX**（MariaDB 硬约束至 12.3 LTS）。
> 需要 VECTOR INDEX 的列必须 `NOT NULL`。
> 向量查询必须带 `LIMIT`，否则全表扫描。

### 3.2 server_bge.py

WSL 路径：`server_bge.py`（本仓根目录）

| 端点 | 端口 | 策略 |
|------|------|------|
| `/v1/embeddings` | 9005 | 本地优先（BGE-M3 on GPU） |
| `/v1/rerank` | 9006 | 远端优先（硅基流动） |

> 请求可在 `model` 字段中加 `local:` 或 `remote:` 前缀强制覆盖端口默认策略。
> API 兼容 OpenAI Embeddings 格式和 Cohere Rerank 格式。
> 模型：BAAI/bge-m3，1024 维，ModelScope 缓存懒加载，10 分钟 TTL 后释放 GPU 显存。

**嵌入调用示例**：
```python
requests.post("http://127.0.0.1:9005/v1/embeddings", json={
    "model": "BAAI/bge-m3",
    "input": ["要嵌入的文本"]
})
# → {"data": [{"embedding": [0.123, ...], "index": 0}], ...}
```

### 3.3 server_paddle.py（待新写）

输入：图片 Base64 或二进制
输出：OCR 文本 + 置信度
接口：HTTP（可指定端口）或 stdin/stdout 管道
底层：PaddleOCR 3.7.0，`PaddleOCR(lang="ch")` + `ocr.predict(input=np.array(img))`

### 3.4 Everything HTTP API

搜索文件：
```
GET http://localhost:9000/?search={filename}&json=1&path_column=1&count=100
```
返回 JSON 数组，每项含 `name`、`path`、`size`、`date_modified`。

---

## 4. 数据库 Schema

### 4.1 三张主表（统一骨架）

```sql
-- documents_book（图书 + 游戏剧情）
CREATE TABLE documents_book (
    guid         VARCHAR(36) PRIMARY KEY,          -- UUID
    bucket       VARCHAR(64) NOT NULL,              -- 五个桶之一
    tags         TEXT DEFAULT '',                   -- 英文逗号分割；OMNIA 为保留哨兵，见 §4.6
    key_metadata MEDIUMTEXT DEFAULT '',             -- 书名（原始文件名去扩展名）
    content      MEDIUMTEXT NOT NULL,               -- 全文
    embedding    VECTOR(1024) NOT NULL,             -- BGE-M3 嵌入
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FULLTEXT idx_ft_content (content) WITH PARSER ngram,
    VECTOR INDEX idx_vec (embedding) M=16 DISTANCE=cosine
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;

-- documents_chat（AI对话）
CREATE TABLE documents_chat (
    guid         VARCHAR(36) PRIMARY KEY,
    bucket       VARCHAR(64) NOT NULL,
    tags         TEXT DEFAULT '',
    key_metadata MEDIUMTEXT DEFAULT '',             -- 对话摘要
    content      MEDIUMTEXT NOT NULL,
    embedding    VECTOR(1024) NOT NULL,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FULLTEXT idx_ft_content (content) WITH PARSER ngram,
    VECTOR INDEX idx_vec (embedding) M=16 DISTANCE=cosine
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;

-- documents_onenote（OneNote笔记 + 剪报）
CREATE TABLE documents_onenote (
    guid         VARCHAR(36) PRIMARY KEY,
    bucket       VARCHAR(64) NOT NULL,
    tags         TEXT DEFAULT '',
    key_metadata MEDIUMTEXT DEFAULT '',             -- 标题 or 前50字
    content      MEDIUMTEXT NOT NULL,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FULLTEXT idx_ft_content (content) WITH PARSER ngram
    -- ⚠️ 无 VECTOR——OneNote 笔记太短，全文搜索够用
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;
```

### 4.2 图片表

```sql
CREATE TABLE images (
    md5_hash   CHAR(32) PRIMARY KEY,
    file_path  VARCHAR(1024) NOT NULL,              -- 硬链接目标路径
    ref_count  INT UNSIGNED DEFAULT 1,
    ocr_text   MEDIUMTEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 4.3 类别配置表

```sql
CREATE TABLE category_config (
    category         VARCHAR(32) PRIMARY KEY,       -- 'book' / 'chat' / 'onenote'
    table_name       VARCHAR(64) NOT NULL,           -- 对应主表名
    use_embedding    BOOLEAN DEFAULT TRUE,
    use_rerank       BOOLEAN DEFAULT TRUE,
    key_metadata_desc TEXT DEFAULT '',               -- key_metadata 字段说明
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- 初始数据
INSERT INTO category_config VALUES
('book',    'documents_book',    TRUE, TRUE,  '书名（从原始文件名提取）'),
('chat',    'documents_chat',    TRUE, TRUE,  '对话摘要（自动生成）'),
('onenote', 'documents_onenote', FALSE, FALSE, '标题优先，否则正文前50个Unicode可见字符');
```

### 4.4 桶配置表

```sql
CREATE TABLE bucket_config (
    bucket     VARCHAR(64) PRIMARY KEY,              -- '图书' / '游戏剧情' / 'AI对话' / 'OneNote笔记' / '剪报'
    category   VARCHAR(32) NOT NULL,                 -- 所属类别
    description TEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (category) REFERENCES category_config(category)
) ENGINE=InnoDB;

-- 初始数据
INSERT INTO bucket_config VALUES
('图书',       'book',    '正式出版物、论文等'),
('游戏剧情',   'book',    '游戏剧本、剧情分析'),
('AI对话',     'chat',    '与AI助手的完整对话记录'),
('OneNote笔记', 'onenote', '个人笔记、零散想法'),
('剪报',       'onenote', '网页剪辑、文章收藏');
```

---

### 4.5 书桶 chunk 层（切块政策 v2，2026-08-28 定稿）

`documents_book_chunk` 表（scripts/migrate_001_chunk.sql 建表；
scripts/migrate_002_chunk_offsets.sql 加偏移/长度列）：

```sql
CREATE TABLE documents_book_chunk (
    chunk_id       BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    book_guid      VARCHAR(36) NOT NULL,            -- FK → documents_book.guid (CASCADE)
    chunk_seq      INT UNSIGNED NOT NULL,           -- 书内序号 1 起
    heading_path   VARCHAR(512) DEFAULT '',          -- 章节路径（如 "第二章 > 第三节"）
    char_start     INT UNSIGNED NOT NULL DEFAULT 0,  -- 码点偏移（0-based；SUBSTRING 需 +1）
    char_len       INT UNSIGNED NOT NULL DEFAULT 0,  -- 码点长度（原文覆盖区间长）
    visible_start  INT UNSIGNED NOT NULL DEFAULT 0,  -- 可见码点偏移（切块口径，护栏对账）
    visible_len    INT UNSIGNED NOT NULL DEFAULT 0,
    byte_start     INT UNSIGNED NOT NULL DEFAULT 0,  -- NFC 后 UTF-8 字节偏移（C 侧直读）
    byte_len       INT UNSIGNED NOT NULL DEFAULT 0,
    grapheme_len   INT UNSIGNED NOT NULL DEFAULT 0,  -- 字素簇数（统计口径，无对应偏移）
    has_pua        TINYINT(1) NOT NULL DEFAULT 0,    -- 区间有 PUA → 检索回查全文表取原文
    content        MEDIUMTEXT NOT NULL,              -- 净化版（PUA → U+FFFD 等长替换）
    content_tokenized MEDIUMTEXT NOT NULL,
    embedding      VECTOR(1024) NOT NULL,
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    FULLTEXT idx_ft_chunk (content_tokenized) WITH PARSER ngram,
    VECTOR INDEX idx_vec_chunk (embedding) M=16 DISTANCE=cosine
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;
```

**切块政策 v2**：

- **净化/保真分离**：全文表 `documents_book.content` 永远存原文（含 PUA，保真）；chunk 表存净化版。
  等长替换论证：PUA 与 U+FFFD 同为 1 码点 / 1 可见 / UTF-8 3 字节 / 1 字素簇 →
  四口径逐字符等长，偏移与长度对两者通用（回查零换算）。
  检索回查：`IF(c.has_pua, SUBSTRING(b.content, c.char_start + 1, c.char_len), c.content)`
- **坐标系基准**：全部相对"清洗后 NFC 文本"（0-based）。三偏移四长度各司其职：
  char=SUBSTRING 定位 / visible=护栏对账 / byte=C 侧直读 / grapheme=统计
  （MariaDB 无字素概念，grapheme 无对应偏移——设计决策）
- **尺寸口径**：`max_chars=2500` 为**可见码点**（空白不计载荷）；双保险物理护栏：
  可见 ≤ max_chars 且 码点×2 ≤ 8192 token（BGE-M3 上限）
- **切点完整性**：字符硬切点对齐字素簇边界（UAX#29，`regex \X`）；实现隔离在
  `core/unicode_utils.py`（将来换 PyICU 只改该文件）。段落/句子边界假设天然为簇边界
- **覆盖区间语义**：`[char_start, char_start+char_len)` 为原文真实覆盖窗口。
  单节/无 overlap chunk：`char_len == len(content)`；overlap/多节合并 chunk：
  content 含人造 `\n\n` 分隔符而区间不含 → 回查返回无缝原文
- **实现**：`core/unicode_utils.py`（可见码点/PUA 净化/字素簇/前缀和）+
  `core/chunker.py`（切块 v2，净化入口 + 可见口径 + 簇对齐 + 精确偏移）
- **依赖**：`regex>=2024.5.15`（UAX#29；ai-services 已装 2026.5.9）

### 4.6 Tag 语义（哨兵 OMNIA 与写入校验）

**权威定义**：`src/core/tags.py`（纯函数、无依赖）。哨兵值、SQL 保留字表、非法字符表
只在该文件定义，其余模块一律 import——禁止各处硬写。

**读路径**（`search_docs` / `mhi search` / `list_books` / `set_default_tags`）：

| 输入 | 语义 |
|------|------|
| 不传 / 空串 / `OMNIA` | 搜全部数据：不生成 tag 谓词，三者**同义** |
| `化学` | 只搜该 tag 命中项 |
| `a,b` | 任一命中即入选（OR） |
| `OMNIA,化学` | **报错**：ALL OR &lt;tag&gt; 恒等于 ALL，无意义 |
| SQL 保留字 / 含结构性字符 | **报错** |

收敛点：`core/search.py::search_documents()` 单点规范化（CLI 与 MCP 都经此）；
`mcp_server.list_books()` 调用同一函数（它是 LIKE 粗匹配，语义与 search 的精确匹配不同，
但哨兵与非法值处理一致）。

**写路径**（`mhi import --tags`、`mhi tag --add`、目录名/子目录名自动 tag）：

- `OMNIA` 是保留哨兵，**永不落库**——写入即报错（防止哨兵被污染后语义失效）；
- tag 不得是 SQL 保留字（`SELECT`/`IN`/`DATA`…），不得含引号/分号/反引号/反斜杠/换行/制表；
- 逗号是分隔符：多值输入按逗号切分；**单值来源**（目录名）含逗号直接报错——
  否则会被静默切成两个 tag；
- 校验落点：`core/tag_manager.py`（数据层闸门）+ 三个 importer 的 tags 拼装处。
  存量数据不做写时拦截（由体检 SQL 负责），避免脏数据让后续操作寸步难行。

**定位**：这套校验是「输入卫生 + 早失败」，**不是注入防线**——注入防线是全项目沿用
绑定参数（`?` 占位符），禁止任何地方退化为字符串拼接 SQL。

**体检入口**：`mhi tag --audit`（全库只读体检：空 tags / 哨兵落库 / CSV 破损 /
保留字与非法字符；`healthy=false` 时退出码 1，便于脚本与巡检接入；判据与写路径校验同源）。
`mhi-import-book` 胜利退出时也会提示「库内 N 本书无任何 tag」——
这是「漏打 tag」的显式探测器（隐式探测器实测不工作：3 本 / 3493 chunk 长期未被察觉）。

等价 SQL（都应返回 0 行）：

```sql
SELECT COUNT(*) FROM documents_book WHERE FIND_IN_SET('OMNIA', tags);
SELECT COUNT(*) FROM documents_book WHERE tags LIKE ',%' OR tags LIKE '%,' OR tags LIKE '%,,%';
```

---

## 5. config.toml

```toml
[database]
host = "127.0.0.1"
port = 9002
name = "memoria_del_hielo"
user = "mhi_user"
# password from env MHI_DB_PASSWORD

[embedding]
model = "BAAI/bge-m3"
dimension = 1024
local_endpoint = "http://127.0.0.1:9005/v1/embeddings"
remote_endpoint = "http://127.0.0.1:9006/v1/embeddings"
rerank_local = "http://127.0.0.1:9005/v1/rerank"
rerank_remote = "http://127.0.0.1:9006/v1/rerank"

[ocr]
endpoint = "http://127.0.0.1:9008/ocr"
# or "pipe" for stdin/stdout mode

[everything]
http_url = "http://localhost:9000/"

[images]
storage_dir = "images"           # 相对于项目根目录
ocr_enabled = true

[cache]
dir = "cache"                    # 相对路径，存放嵌入向量缓存
# 缓存格式: JSON Lines，每行 {text_hash: [float32_array]}
# 与数据库独立——--reset-db 不删除缓存

[import]
# 默认批量导入时的参数
default_bucket_onenote = "OneNote笔记"
default_bucket_clipping = "剪报"
default_bucket_book = "图书"
default_bucket_game = "游戏剧情"
default_bucket_chat = "AI对话"
```

---

## 6. 目录结构

```
memoria-del-hielo/
├── config.toml                  # 配置文件
├── PROJECT_SPEC.md              # 本文件
├── INDEX.md                     # 模块索引（按模块化自文档规范维护）
├── pyproject.toml               # Python 项目元数据
├── server_bge.py                # 常驻嵌入 & 重排代理（9005 本地优先 / 9006 远端优先）
├── server_paddle.py             # PaddleOCR HTTP 服务（9008）
│
├── src/
│   ├── mcp_server.py            # MCP Server 入口（mcp-memoria.del.hielo）
│   ├── mcp_tools/               # MCP 工具实现
│   │   ├── __init__.py
│   │   ├── import_book.py
│   │   ├── import_chat.py
│   │   ├── import_onenote.py
│   │   ├── search.py
│   │   ├── tag.py
│   │   └── cleanup.py
│   │
│   ├── cli/                     # CLI 工具
│   │   ├── __init__.py
│   │   ├── mhi_import_book.py
│   │   ├── mhi_import_chat.py
│   │   ├── mhi_import_onenote.py
│   │   ├── mhi_clean_chat.py
│   │   ├── mhi_search.py
│   │   ├── mhi_tag.py
│   │   └── mhi_cleanup_images.py
│   │
│   ├── core/                    # 共享核心模块
│   │   ├── __init__.py
│   │   ├── db.py                # MariaDB 连接（即弃连接，不用池）
│   │   ├── config.py            # 配置加载
│   │   ├── embedding.py         # 嵌入向量缓存 + 调用
│   │   ├── search.py            # 全文搜索 + 向量搜索
│   │   ├── image_pipeline.py    # 图片发现 → MD5 → OCR → 硬链接
│   │   └── tag_manager.py       # Tag 增删改查
│   │
│   ├── importers/               # 各类导入器
│   │   ├── __init__.py
│   │   ├── book_importer.py
│   │   ├── chat_importer.py
│   │   └── onenote_importer.py
│   │
│   └── cleaners/                # 清洗器
│       ├── __init__.py
│       └── chat_cleaner.py      # AI 对话清洗
│
├── images/                      # 图片存储目录（{md5} 无扩展名）
├── cache/                       # 嵌入向量缓存
├── tests/
│   └── ...
└── scripts/
    ├── init_db.sql              # 建库建表脚本
    └── reset_db.sql             # 删库重建脚本（调试用）
```

---

## 7. 核心模块职责

| 模块 | 职责 | 关键约束 |
|------|------|---------|
| `core/db.py` | MariaDB 连接池，`execute_sql()` 统一入口，连接串从 `config.toml` + 环境变量读取 | 不直接写 connection string，走配置 |
| `core/config.py` | 加载 `config.toml`，验证必填字段，提供 `get(key)` 接口 | 启动时一次性加载并校验 |
| `core/embedding.py` | 调用 server_bge.py 获取嵌入向量，缓存层（`{text_hash: vector}` JSON Lines） | 断点续传：入库前先查缓存，命中跳过；重排走远程端口 |
| `core/search.py` | 双路搜索：全文搜索（MATCH...AGAINST）+ 向量搜索（VEC_DISTANCE_COSINE），合并结果 | 全文搜索优先（快速筛选），向量精排；返回时附带 key_metadata |
| `core/image_pipeline.py` | 从 Markdown 提取图片引用 → 按路径查找 → Everything 兜底 → MD5 → OCR → 硬链接 | 找到图后先查 `images` 表，命中则 `ref_count++` 并复用 OCR |
| `importers/*` | 每类素材的入库逻辑 | 单文件 + 批量两套入口，共用核心管线 |
| `cleaners/chat_cleaner.py` | AI 对话 Markdown → 清洗后的纯 Markdown + 摘要 | 见第 10 节 |

---

## 8. CLI 工具规格

所有 CLI 工具支持 `--help`；长任务支持 Ctrl+C 安全中断（当前批次落盘后退出，下次从缓存续跑）。

### 8.1 mhi_import_book

```
mhi_import_book <path> [OPTIONS]

选项：
  --bucket TEXT        目标桶（默认: "图书"）
  --tags TEXT          手动 tag，英文逗号分割
  --recursive          递归处理子目录
  --cache-dir PATH     嵌入缓存目录（默认: config.toml cache.dir）
  --no-embedding       跳过嵌入（调试用）

输入：单个 .md/.txt/.epub/.pdf 文件，或目录
流程：
  1. 读取文件 → 提取文本
  2. 提取书名（原始文件名去扩展名）→ key_metadata
  3. 扫描文中图片引用 → image_pipeline
  4. 嵌入（如开启）→ 缓存 → 入库 documents_book
```

### 8.2 mhi_import_chat

```
mhi_import_chat <path> [OPTIONS]

选项：
  --bucket TEXT        目标桶（默认: "AI对话"）
  --tags TEXT          手动 tag
  --cache-dir PATH     嵌入缓存目录
  --no-clean           跳过清洗（调试用，保留原始内容入库）
  --summary TEXT       手动指定摘要（默认自动生成）

输入：Chatbox 导出的 .md 文件
流程：
  1. 清洗（见第 10 节）
  2. 生成摘要 → key_metadata
  3. 结构化分解（按对话轮次分块）
  4. 嵌入 → 缓存 → 入库 documents_chat
```

### 8.3 mhi_import_onenote

```
mhi_import_onenote <path> [OPTIONS]

选项：
  --bucket TEXT        目标桶（默认: "OneNote笔记"）
  --tags TEXT          手动 tag
  --auto-tags          从子目录名自动提取 tag（默认开启）
  --no-auto-tags       禁用自动 tag
  --cache-dir PATH     缓存目录（OneNote 类无需嵌入，但 OCR 图片时可用）

输入：OneNoteMdExporter 导出的目录
流程：
  1. 遍历目录下 .md 文件
  2. 读取 YAML front matter → 提取 title
  3. key_metadata = title（如果不以 "..." 结尾）否则 前50个 Unicode 可见字符
  4. 如 --auto-tags，从子目录名提取 tag（如 "欧洲历史/4个β.md" → tag="欧洲历史"）
  5. 扫描图片引用 → image_pipeline
  6. 入库 documents_onenote（无嵌入）
```

### 8.4 mhi_clean_chat

```
mhi_clean_chat <input.md> [--output OUTPUT.md] [--summary]

独立的清洗工具——不入库，只输出清洗后的 Markdown。
--summary 则额外输出生成的对话摘要。

清洗规则见第 10 节。
```

### 8.5 mhi_search

```
mhi_search <query> [OPTIONS]

选项：
  --category TEXT      限定类别（book/chat/onenote，不指定则搜全部）
  --bucket TEXT        限定桶
  --tags TEXT          限定 tag（OMNIA = 搜全部数据，与不传同义；语义见 §4.6）
  --limit INT          返回条数（默认 10）
  --fulltext-only      仅全文搜索（跳过向量）
  --vector-only        仅向量搜索（跳过全文，需要嵌入模型）

输出：JSON 数组，每项含 guid、bucket、key_metadata、content 摘要、score
```

### 8.6 mhi_tag

```
mhi_tag <guid> [OPTIONS]

选项：
  --add TEXT           添加 tag（英文逗号分割，可多个）
  --remove TEXT        移除 tag
  --list               列出当前所有 tag
  --category TEXT      类别（book/chat/onenote）
```

### 8.7 mhi_cleanup_images

```
mhi_cleanup_images [--yes]

扫描 images 表 ref_count=0 的记录 → 列出清单 → 等用户确认 → 删除记录 + 物理文件。
不加 --yes 则仅列出不删除。
```

### 8.8 mhi_reset_db

```
mhi_reset_db [--yes]

⚠️ 删除并重建整个数据库（仅调试用）。
需要 --yes 确认。
⚠️ 不删除缓存目录——嵌入向量缓存独立于数据库。
```

---

## 9. MCP 工具规格

MCP Server 注册名：`mcp-memoria.del.hielo`

**设计边界**：MCP 面只暴露**只读**工具（检索 + 发现 + 会话设置）；
导入/管理/清理等运维操作一律下沉 CLI（`mhi-import-*` / `mhi-tag` / `mhi-clean-*`），不在 MCP 面。

| MCP Tool | 粒度 | 对应 CLI | 说明 |
|----------|------|---------|------|
| `search_docs` | chunk | `mhi_search` | 找**内容/段落**：全文 + 向量混合，rerank 精排。返回 `{results, search_meta}`；单路模式无命中会**自动补另一路**并在 `search_meta.fallback` 标注（防静默空手） |
| `find_books` | 书 | `mhi_find_book`（`mhi find-book`） | 找**书**：书名+目录语义近邻；**只排序、不判断库内是否存在** |
| `read_book_chunk` | chunk | —（S2B 钻取） | 取某 chunk 全文 + 前后邻域 |
| `set_default_tags` | — | —（会话级） | 本场默认 tag；`OMNIA` = 搜全部数据 |
| `list_buckets` | — | — | 桶枚举（含文档数） |
| `list_tags` | — | — | tag 云（含出现次数） |
| `list_books` | 书 | — | 按 bucket/tag **枚举**清单（LIKE 粗匹配，无语义） |

**三个工具怎么选**（同时写进各工具 docstring，供模型自行选用）：
`find_books` = 主题型找书 ｜ `search_docs` = 找具体内容（细节型查询） ｜ `list_books` = 按元数据枚举。

`find_books` 刻意**不做存在性判定**：书级信号（书名+目录）对细节型/术语型查询不敏感，
真实查询上任何阈值都会误伤约 27% 的"题材确实在库"的查询——标定数据见
`docs/真实检索查询分布案例.md` §五，实现约束见 `core/search.py::SearchEngine.find_books`。

MCP 工具共享 `core/` 模块，不复写业务逻辑。

> tag 语义（哨兵 `OMNIA` / 读路径折叠 / 写路径拦截）在 MCP 面与 CLI 面**完全一致**，
> 统一由 `core/tags.py` 决定（见 §4.6）——MCP 工具不自行解释 tag。

---

## 10. AI 对话清洗规格

### 输入格式

Chatbox 导出 Markdown。两种格式变体：

**格式 A（旧）**：`**user**:` / `**assistant**:` 标记
**格式 B（新）**：`## 🧑‍💻 User` / `## 🤖 Assistant` 标题

### 清洗规则

| 保留 | 清洗（删除） |
|------|------------|
| 用户消息全文 | System Prompt 大块（`**system**:` 块或开头的 System Prompt 区域） |
| AI 最终产出（代码、结论、决策） | 所有工具调用 JSON（搜索类 + 非搜索类，一律删除） |
| — | 搜索结果原文、拉取网页的完整内容 |
| — | AI 冗长推理铺垫（"让我想想…"、"我先查一下…"） |
| — | 对话摘要块（如有） |

### 结构化分解

清洗后的对话按轮次分块。一轮 = User消息 + AI回复。

```markdown
## 轮次 1

**用户**: ...

**助手**: ...

---

## 轮次 2

...
```

### 摘要生成

调用 server_bge.py 远端端口（9006）用 LLM 生成摘要？——**不**。摘要由本地规则生成：
- 提取首轮用户问题作为基础
- 如果对话有明显主题切换，记录切换点
- 最终摘要 ≤ 200 字中文

如用户通过 `--summary` 手动指定，则优先使用手动摘要。

---

## 11. 图片管线规格

```
Markdown 中的图片引用
        │
        ▼
  按路径查找（相对 → 绝对）
        │
   ┌── 找到 ──┐     没找到
   │           │       │
   │           ▼       ▼
   │      Everything HTTP API 搜索文件名
   │           │
   │      ┌── 找到 ──┐  没找到 → 记录警告，跳过
   │      │           │
   ▼      ▼           │
  计算 MD5             │
   │                   │
   ▼                   │
  SELECT images        │
  WHERE md5_hash       │
   │                   │
   ┌── 命中 ──┐        │
   │          │        │
   ▼          ▼        │
  ref_count++   OCR     │
  复用ocr_text  │       │
   │           ▼       │
   │      硬链接到     │
   │   images/{md5}   │
   │  (无扩展名)      │
   │  失败则复制      │
   │           │       │
   │           ▼       │
   │    INSERT images  │
   │           │       │
   └─────┬─────┘       │
         ▼             │
  插入 OCR 文本到 Markdown
  图片引用下方
  （如果能 OCR 到内容）
```

**硬链接策略**：`os.link()` 优先，`EXDEV`（跨设备）则 `shutil.copy2()`。

---

## 12. OneNote 导入规格

### 输入结构

OneNoteMdExporter 导出目录结构：
```
天依 的笔记本/
├── 欧洲历史/           ← 子目录 = 自动 tag
│   ├── 4个β.md
│   └── 《秘史》.md
├── 名人名言/
│   └── ...
└── ...
```

每个 .md 文件可能含 YAML front matter：
```yaml
---
title: 4个β
updated: 2026-06-08T23:53:00+08:00
created: 2026-06-08T23:53:00+08:00
---
```

### key_metadata 提取

```python
def extract_key_metadata(md_text: str) -> str:
    title = parse_yaml_front_matter(md_text).get("title", "")
    if title and not title.endswith("..."):
        return title
    # 否则取正文前50个 Unicode 可见字符
    body = strip_yaml_front_matter(md_text)
    visible = "".join(ch for ch in body if ch.isprintable() and ch not in '\n\r\t')
    return visible[:50]
```

### 自动 Tag

从目录结构中提取：文件 `欧洲历史/4个β.md` → 自动 tag = `"欧洲历史"`。
手动 `--tags` 与自动 tag 合并。

---

## 13. 图书导入规格

### 输入

单个 .md / .txt / .epub / .pdf 文件，或包含此类文件的目录。

### key_metadata

原始文件名（去扩展名）→ 书名。
如 `《三体》.md` → key_metadata = `"《三体》"`。

### 文本提取

| 格式 | 方法 |
|------|------|
| .md / .txt | 直接读取 |
| .epub | `ebooklib` 或 `calibre` |
| .pdf | `PyMuPDF`（fitz）或 pdfplumber |

---

## 14. 搜索规格

### 双路搜索流程

```
用户查询 "中世纪欧洲"
        │
        ├── 全文搜索（所有适用表）
        │   MATCH(content) AGAINST('中世纪 欧洲' IN BOOLEAN MODE)
        │   返回: [(guid, key_metadata, snippet, score_ft), ...]
        │
        └── 向量搜索（仅 book + chat）
            1. 查询文本 → server_bge.py → 1024维向量
            2. SELECT ... ORDER BY VEC_DISTANCE_COSINE(embedding, query_vec) LIMIT 20
            3. 返回: [(guid, key_metadata, snippet, score_vec), ...]
                    │
                    ▼
              合并去重，全文优先，向量补位
                    │
                    ▼
              返回结果，附带 key_metadata → LLM 对手方
```

### 返回格式

```json
[
  {
    "guid": "uuid",
    "bucket": "欧洲历史",
    "category": "onenote",
    "key_metadata": "4个β",
    "snippet": "拜占庭帝国...",
    "score_ft": 3.2,
    "score_vec": 0.87,
    "source": "fulltext|vector|both"
  }
]
```

---

## 15. 编码与字符集铁律

- **全程 UTF-8**：读文件 → 处理 → 入库 → 输出，无一例外。
- **NFC 归一化**：入库前所有文本 NFC 归一化。输入尽可能规范，输出和搜索尽可能宽松。
- **MariaDB 排序规则**：`utf8mb4_uca1400_ai_ci`（Unicode 14.0，accent insensitive, case insensitive）。
- **配置文件**：UTF-8 无 BOM。
- **环境变量**：数据库密码从 `MHI_DB_PASSWORD` 读取，不在 config.toml 中明文存储。

---

## 16. 调试与开发

### 16.1 嵌入缓存

- 格式：JSON Lines，`cache/` 目录下按哈希前缀分子目录
- 每条：`{"md5_hash_of_text": [0.123, 0.456, ...]}`
- 入库前先查缓存 → 命中则跳过嵌入调用
- 独立于数据库——`--reset-db` 不删除缓存

### 16.2 急停与续传

- 所有批量操作在循环/批次边界检查 `SIGINT`
- 收到中断信号 → 当前批次落盘 → 释放资源 → 退出
- 下次启动：缓存命中已处理的条目，从中断处继续
- 🚫 不产生半成品数据——每条入库是独立事务

### 16.3 调试参数

| 参数 | 说明 |
|------|------|
| `--reset-db` | 删库重建（需确认），清空所有数据但不删缓存 |
| `--cache-dir` | 指定缓存目录（覆盖 config.toml） |
| `--no-embedding` | 跳过嵌入（加速调试） |
| `--no-clean` | 跳过 AI 对话清洗（保留原始内容入库，调试用） |
| `--dry-run` | 只走流程不实际写入数据库 |

---

## 17. 关键设计决策（为何这样设计）

| 决策 | 理由 |
|------|------|
| 三表而非一表 | 三个类别的处理方式差异大（书有嵌入、笔记无嵌入），分开更清晰；且 MariaDB 每表仅一个 VECTOR INDEX，分表自然解决 |
| OneNote 无向量嵌入 | 零碎笔记 1-5KB，全文搜索已足够精确，嵌入反而增加延迟和存储成本 |
| key_metadata 而非纯向量 | 书名/标题/摘要作为第一跳筛选比 1024 维向量更快更准确，给 LLM 对手方提供上下文判断 |
| ChromaDB 退场 | 统一进 MariaDB 减少运维复杂度（一台数据库服务一切） |
| 图片永不自动删除 | 安全第一；`--cleanup-images` 由用户手动触发 |
| jieba + ngram 全文搜索 | Mroonga 无 Windows + Arch 不稳定；jieba 在 Python 侧分词后走 ngram 解析 |
| 每表一个 VECTOR 列（非两个） | MariaDB 硬约束；方案 A（换模型=ALTER TABLE）比方案 B（双表冗余）更简洁 |
| 硬链接优先 | 节省磁盘空间，同一图片多文档引用只存一份 |
| 嵌入缓存独立于 DB | 删库调试不丢失嵌入缓存，避免重复调用嵌入模型 |

---

## 18. 实现顺序（建议）

1. **基础设施**：`config.toml` → `core/config.py` → `core/db.py` → `scripts/init_db.sql` → 建库建表
2. **外部服务对接**：`core/embedding.py`（对接 server_bge.py）→ `core/image_pipeline.py`（对接 Everything + PaddleOCR）
3. **导入器**：`importers/onenote_importer.py`（最简单，无嵌入）→ `importers/book_importer.py` → `importers/chat_importer.py`
4. **清洗器**：`cleaners/chat_cleaner.py`
5. **搜索**：`core/search.py`（全文 + 向量双路）
6. **CLI**：`cli/mhi_*.py`
7. **MCP**：`mcp_server.py` + `mcp_tools/`
8. **测试**：`tests/` 覆盖所有导入路径 + 边缘情况
9. **INDEX.md**：按模块化自文档规范编写

---

## 附录 A：MariaDB VECTOR 常用操作

```sql
-- 插入向量（二进制模式，性能最优）
-- Python: np.asarray(emb, dtype=np.float32).tobytes()
INSERT INTO documents_book (guid, bucket, content, embedding)
VALUES (UUID(), '图书', '...', ?);

-- 读取向量（可读格式）
SELECT VEC_ToText(embedding) FROM documents_book WHERE guid = ?;

-- 向量搜索（⚠️ 必须有 LIMIT）
SELECT guid, key_metadata,
       VEC_DISTANCE_COSINE(embedding, VEC_FromText(?)) AS dist
FROM documents_book
ORDER BY dist
LIMIT 20;

-- ⚠️ 不要用 WHERE dist < threshold —— 不走 HNSW 索引，全表扫描
```

## 附录 B：图片搜索 Everything HTTP

```python
import urllib.parse, requests

def search_image(filename: str) -> list[dict]:
    url = f"http://localhost:9000/?search={urllib.parse.quote(filename)}&json=1&path_column=1&count=100"
    resp = requests.get(url, timeout=10)
    return resp.json()  # [{name, path, size, date_modified}, ...]
```

## 附录 C：PaddleOCR 3.7 调用

```python
from paddleocr import PaddleOCR

ocr = PaddleOCR(lang="ch", show_log=False)

# 图片路径
result = ocr.predict(input="image.png")
# 或 numpy array
import numpy as np
result = ocr.predict(input=np.array(pil_image))

# 提取
texts = result.rec_texts      # list[str]
scores = result.rec_scores    # list[float]
polys = result.dt_polys        # list[ndarray]
```

---

## 19. MinerU 脚注提取与图片后处理

### 19.1 背景

MinerU 默认输出将脚注内容归类到 `discarded_blocks`，不写入 Markdown 正文。
同时，MD 中的图片引用使用 MinerU 内部 hash（`/resources/images/HASH`），
无法直接映射到实际文件。

本节定义自动后处理管线：在 `mhi_import_book` 导入 MinerU 解析的 MD 时，
自动提取脚注入库并重映射图片路径，使后续的 `image_pipeline` 可正常处理。

### 19.2 检测与触发

**检测方式**：MD 同目录存在 `{stem}_middle.json` → 判定为 MinerU 输出，触发全部后处理。

```
hybrid_auto/
├── 书名.md
├── 书名_middle.json             ← 判据
├── 书名_content_list_v2.json     ← 图片映射来源（策略①）
└── images/                       ← 实际图片文件（策略②）
```

### 19.3 脚注提取流水线

```
_middle.json → pdf_info[page_idx].discarded_blocks
                    │
                    ├── 过滤: y > page_height × 0.70 (页面底部 30%)
                    ├── 过滤: type = page_number → 丢弃
                    ├── 过滤: 纯数字 / 点包围数字 → 丢弃
                    ├── 过滤: 文献编号 (178) (127,203) → 丢弃
                    ├── 过滤: 长度 < 3 → 丢弃
                    │
                    ▼
              INSERT page_footnotes
              (book_guid, page_idx, footnote_text, footnote_type, bbox)
```

**page_footnotes 表**：

| 列 | 类型 | 说明 |
|----|------|------|
| id | BIGINT PK | 自增 |
| book_guid | VARCHAR(36) FK → documents_book | CASCADE 删除 |
| page_idx | INT NOT NULL | MinerU 内部页索引（0-based） |
| footnote_text | MEDIUMTEXT NOT NULL | 脚注全文 |
| footnote_type | VARCHAR(32) | page_footnote / text |
| bbox | TEXT | JSON: `[x0, y0, x1, y1]` |

**实际效果**（以《火的记忆II》为例）：

| 指标 | 值 |
|------|-----|
| 总页数 | ~400 |
| 含脚注页 | 大量（每页 2-5 条） |
| middle.json discarded_blocks 中脚注 | 类型不统一：`page_footnote` / `text`（均为脚注内容） |
| 噪音过滤 | 页码（纯数字）、原书页码引用 `(178)` `(197)` |

### 19.4 图片路径重映射流水线

MinerU 的图片引用使用内部 hash。映射采用三层策略，按优先级逐级降级：

**三层匹配策略**（按优先级）：

| 优先级 | 策略 | 数据源 | 说明 |
|--------|------|--------|------|
| ① | VLM 描述精确匹配 | `_content_list_v2.json` → `content` 字段 | 描述文本精确相等 |
| ② | hash 直查文件名 | `images/` 目录 | MinerU 内部 hash = 实际文件 stem |
| ③ | 保留原样 | — | 无法解析时仅移除 `<details>` 块 |

**策略 ① — VLM 描述精确匹配**：

`content_list_v2.json` 中每个 `image` 条目同时包含 VLM 生成的描述文本
（`content` 字段）和实际文件路径（`image_source.path`），MD 中 `<details>` 块
也含有相同的描述文本。利用这一对应关系做精确匹配：

```
content_list_v2.json                  MD
┌─────────────────────┐              ┌──────────────────────────────┐
│ type: "image"       │              │ ![](/resources/images/764...)│
│ content:            │   VLM描述    │ <details>                    │
│   image_source:     │   精确匹配   │ <summary>natural_image</...> │
│     path: "images/  │◄──────────►│                              │
│      4547...jpg"    │              │ Abstract black-and-white...  │
│   content: "Abstra  │              │ </details>                   │
│     ct black-and-wh │              └──────────────────────────────┘
│     ite..."         │
└─────────────────────┘
        │
        ▼
  ![](/resources/images/764...) + <details>...</details>
        │  替换为
        ▼
  ![](images/45471770cb...jpg)
```

**策略 ② — hash 直查文件名**（兜底）：

MinerU MD 中 `/resources/images/2c56f1e2606f5ea6fa6d02ca871e457b` 的 hash 部分
就是实际文件名的 stem——`images/2c56f1e2606f5ea6fa6d02ca871e457b.jpg`。
按 `.jpg` / `.jpeg` / `.png` / `.webp` / `.bmp` / `.gif` 逐一尝试，命中即匹配。
此策略不依赖 content_list JSON，即使 JSON 缺失也能工作。

**为何不用顺序匹配**：`content_list_v2` 中 image 条目数量（如 5 条）可能多于 MD 中的
`![]()` 引用数（如 2 条）——MinerU 可能跳过部分图片的 MD 输出。

### 19.5 模块与集成点

**模块**：`src/core/mineru_postprocess.py`

| 公开方法 | 职责 |
|---------|------|
| `MineruPostprocessor.detect(md_path) → bool` | 检测 _middle.json |
| `extract_footnotes(md_path, book_guid) → int` | 提取脚注 → page_footnotes |
| `prepare_images(md_text, source_dir) → str` | 图片路径重映射（三层策略） |

**集成点**（`src/importers/book_importer.py` → `import_file()`）：

```
extract_text()
    │
    ├── detect(md_path) == True?
    │   └── prepare_images()   ← 图片路径重映射（三层策略）
    │
    ├── image_pipeline.process_image_refs()  ← OCR+MD5+硬链接
    │
    ├── tokenize + embedding
    │
    ├── INSERT documents_book
    │
    └── detect(md_path) == True?
        └── extract_footnotes()  ← 脚注入库
```

### 19.6 与搜索的交互

`page_footnotes` 表在搜索时通过 `book_guid` JOIN `documents_book`，
使脚注内容与正文在同一检索结果中并列呈现。未来可扩展：

- 搜索命中脚注 → 返回所在页的正文上下文
- 搜索命中正文 → 可选附带该页脚注

### 19.7 设计决策

| 决策 | 理由 |
|------|------|
| 脚注不写回 MD 正文 | 保持正文流干净，避免插入点定位错误 |
| page_footnotes 独立表 | 脚注和正文存储语义不同，分表查询更清晰 |
| VLM 描述做图片映射键 | MinerU 内部 hash 不对外暴露；描述文本是策略① |
| hash 直查做兜底 | MinerU 内部 hash = 实际文件 stem，90%+ 命中率；不依赖 content_list |
| page_idx 使用 MinerU 原始值（0-based） | 不做转换，避免页码偏移错误 |
| 文献编号 `(178)` 过滤 | 原书引文索引不是脚注内容，误收会污染检索结果 |
