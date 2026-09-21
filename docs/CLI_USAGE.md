# Memoria del Hielo — CLI 快速参考

> 所有 CLI 工具通过 `python -m src.cli.<模块名>` 或注册的 entry point 运行。
> entry point 注册见 `pyproject.toml` → `[project.scripts]`。

---

## 1. 导入数据

### 图书 / 游戏剧情

```bash
# 单文件
mhi-import-book <书库>/红楼梦.md

# 目录递归
mhi-import-book <书库>/ --recursive

# 指定桶 + 标签
mhi-import-book <游戏目录>/剧本.txt --bucket 游戏剧情 --tags "RPG,剧情分析"

# 跳过嵌入（调试用，但注意 embedding 列 NOT NULL，会报错）
mhi-import-book /tmp/debug.md --no-embedding
```

可选参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--bucket TEXT` | 图书 | 目标桶 |
| `--tags TEXT` | — | 手动标签，英文逗号分割 |
| `--recursive` | 否 | 递归处理子目录 |
| `--no-embedding` | 否 | 跳过嵌入（调试用，schema 约束可能报错） |

### AI 对话

```bash
# 单文件
mhi-import-chat <对话目录>/2026-07-20.md

# 目录
mhi-import-chat <对话目录>/ --recursive

# 指定摘要（不指定则自动从首轮用户问题生成）
mhi-import-chat /path/to/chat.md --summary "关于 RAG 架构的讨论"
```

可选参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--bucket TEXT` | AI对话 | 目标桶 |
| `--tags TEXT` | — | 手动标签 |
| `--no-clean` | 否 | 跳过清洗（原始内容入库，调试用） |
| `--summary TEXT` | 自动生成 | 手动指定摘要 |
| `--recursive` | 否 | 目录递归 |

### OneNote 笔记

```bash
# 单文件
mhi-import-onenote <OneNote导出目录>/欧洲历史/4个β.md

# 目录（递归）
mhi-import-onenote <OneNote导出目录>/

# 关闭自动 tag（默认从子目录名提取 tag）
mhi-import-onenote /path/ --no-auto-tags
```

可选参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--bucket TEXT` | OneNote笔记 | 目标桶 |
| `--tags TEXT` | — | 手动标签 |
| `--auto-tags` | 开启 | 从子目录名自动提取 tag |
| `--no-auto-tags` | — | 禁用自动 tag |

---

## 2. 搜索

```bash
# 全库搜索
mhi-search 神经网络

# 限定类别
mhi-search transformer --category book

# 限定桶
mhi-search Redis --bucket "AI对话"

# 限定标签
mhi-search 缓存 --tags "性能,架构"

# 仅全文搜索 或 仅向量搜索
mhi-search bug --fulltext-only
mhi-search 内存泄漏 --vector-only

# 调整返回条数
mhi-search 分布式 --limit 20
```

可选参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--category TEXT` | 全部 | 限定类别 (book/chat/onenote) |
| `--bucket TEXT` | 全部 | 限定桶 |
| `--tags TEXT` | — | 限定标签 |
| `--limit INT` | 10 | 返回条数 |
| `--fulltext-only` | 否 | 仅全文搜索 |
| `--vector-only` | 否 | 仅向量搜索（需嵌入模型） |

**搜索行为（CJK 感知双路搜索）：**

| 查询类型 | 全文搜索列 | 向量权重 | 全文权重 |
|----------|-----------|---------|---------|
| 含中文（CJK） | `content_tokenized`（jieba 分词） | 0.8 | 0.2 |
| 纯英文/ASCII | `content`（空格分词） | 0.4 | 0.6 |

---

## 3. Tag 管理

```bash
# 查看文档 tag
mhi-tag <uuid> --category book

# 添加 tag
mhi-tag <uuid> --category book --add "技术,数据库"

# 移除 tag
mhi-tag <uuid> --category chat --remove "旧标签"

# 列出所有 tag
mhi-tag <uuid> --list
```

---

## 4. 独立清洗工具（不入库）

```bash
# 清洗后输出到 stdout
mhi-clean-chat /path/to/chatbox_export.md

# 输出到文件
mhi-clean-chat /path/to/input.md --output /path/to/output.md

# 额外输出自动生成的摘要
mhi-clean-chat /path/to/input.md --summary
```

---

## 5. 图片清理

```bash
# 列出孤儿图片（ref_count=0，不删除）
mhi-cleanup-images

# 确认删除
mhi-cleanup-images --yes
```

---

## 6. 数据库重置（⚠️ 调试用）

```bash
# 预览（只列不删）
mhi-reset-db

# 确认重置
mhi-reset-db --yes
```

> ⚠️ 删除并重建整个数据库。**不删除** `cache/` 目录（嵌入向量缓存独立于数据库）。

---

## 7. 环境变量

| 变量 | 用途 | 备注 |
|------|------|------|
| `MHI_DB_PASSWORD` | 数据库密码（兜底） | 仅 `config.toml` 中 `password = ""` 时生效 |
| `MHI_CONFIG` | 指定 config.toml 路径 | 不设置则按发现规则搜索 |
| `MHI_CACHE_DIR` | 嵌入向量缓存目录 | 覆盖 `config.toml [cache] dir` |

---

## 8. 急停与续传

所有批量操作（导入目录、递归搜索等）支持 `Ctrl+C` 安全中断：

1. 当前批次落盘（每条入库是独立事务）
2. 释放资源后退出
3. 下次启动时，缓存命中已处理的条目，从中断处继续

---

## 9. 配置密码优先级速查

```bash
# 方式 A：在 config.toml 中直接填写（优先级高）
#   [database]
#   password = "你的密码"

# 方式 B：设置环境变量（兜底）
export MHI_DB_PASSWORD='你的密码'

# 方式 C：留空 → 自动读环境变量（默认行为）
#   [database]
#   password = ""
```

---

*生成日期：2026-07-24*
*对应版本：PROJECT_SPEC.md v1.0 / OPERATIONS.md v1.0*

## 10. MinerU 输出自动后处理

导入 MinerU 解析的 MD 文件时，系统自动检测并执行两项后处理。

### 10.1 检测方式

导入文件同目录存在 `{文件名}_middle.json` → 判定为 MinerU 输出，触发后处理。

```
hybrid_auto/
├── 书的记忆.md                ← 导入此文件
├── 书的记忆_middle.json        ← 检测到此文件 → 触发
├── 书的记忆_content_list_v2.json ← 图片映射来源
└── images/                    ← 实际图片目录
```

### 10.2 自动后处理

| 处理项 | 数据来源 | 入库目标 | 说明 |
|--------|---------|---------|------|
| 脚注提取 | `_middle.json` → `discarded_blocks`（页面底部 30% 区域） | `page_footnotes` 表 | 过滤页码和文献编号 |
| 图片路径重映射 | `_content_list_v2.json` → `image_source.path` | 图片管线（OCR + MD5 + 硬链接 → `images` 表） | VLM 描述文本精确匹配 |

### 10.3 导入结果变化

导入 MinerU 文件时，返回结果新增字段：

```python
{
    "guid": "uuid",
    "status": "ok",
    "mineru": True,          # ← 是否检测到 MinerU 输出
    "footnotes": 156,        # ← 提取的脚注条数
}
```

### 10.4 脚注表结构

```sql
page_footnotes:
  id             BIGINT PK
  book_guid      VARCHAR(36) FK → documents_book.guid
  page_idx       INT            -- MinerU 内部页索引 (0-based)
  footnote_text  MEDIUMTEXT     -- 脚注全文
  footnote_type  VARCHAR(32)    -- page_footnote | text
  bbox           TEXT           -- JSON: [x0, y0, x1, y1]
```

### 10.5 查询示例

```sql
-- 查看某本书的第 145 页脚注
SELECT page_idx, footnote_text
FROM page_footnotes
WHERE book_guid = '...' AND page_idx = 145;

-- 书籍的脚注总数
SELECT book_guid, COUNT(*) AS cnt
FROM page_footnotes
GROUP BY book_guid;
```
