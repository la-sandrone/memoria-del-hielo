# Memoria del Hielo — CLI 接口速查指南

> 生成于 2026-08-29。所有命令位于 `src/cli/`，两种运行方式：
> ① 项目根转发器：`mhi <子命令> [参数]`（推荐，见下节）；
> ② 直接调用：`python -m src.cli.<模块名> <参数>`（ai-services venv，WSL）。

---

## mhi 转发器（推荐入口）

项目根有 `mhi.cmd`（Windows 原生 Python）/ `mhi.sh`（WSL / bash），把长命令包成子命令：

```bash
mhi import "<corpus-root>\物理、天文、数学\某书\hybrid_auto\某书.md" --bucket 图书
mhi search "花岗岩的形成条件" --limit 5
mhi rm --guid 082d08ca-e324-450b-b2b9-9cc1cddea274,713476ad-... --dry-run
mhi help              # 全部子命令一览
mhi help import       # 等价于 mhi import --help
```

| 子命令 | 目标模块 | 别名 | 危险度 |
|--------|----------|------|--------|
| `import` | mhi_import_book | import-book | 低 |
| `search` | mhi_search | — | 无 |
| `tag` | mhi_tag | — | 无 |
| `rm` | mhi_remove_book | remove | ⚠️ 破坏性 |
| `images` | mhi_cleanup_images | cleanup-images | ⚠️ 破坏性 |
| `emb` | mhi_cleanup_emb_cache | cleanup-emb-cache | ⚠️ 破坏性 |
| `chat` | mhi_import_chat | import-chat | 低 |
| `onenote` | mhi_import_onenote | import-onenote | 低 |
| `clean-chat` | mhi_clean_chat | — | 无 |
| `reset` | mhi_reset_db | reset-db | 🔴 高危 |

**要点**

- 子命令之后的参数**原样转发**（两者等价：`mhi rm --guid X --dry-run` ≡ `python -m src.cli.mhi_remove_book --guid X --dry-run`）；退出码原样透传。
- **不切换当前目录**：相对路径参数按你敲命令时所在的目录解析；`config.toml` 由 `Config._find_config()` 的 `__file__` 兜底查找，与 cwd 无关。
- 子命令表只维护一份（`src/cli/mhi_dispatch.py`），不在 cmd/bash 里各写一套 → 不会漂移。新增 CLI 后若忘了登记，`tests/test_mhi_dispatch.py` 会红。
- Windows 端 `mhi.cmd`：**完全不碰代码页**（`chcp` 会清屏，且属于"改共享控制台状态"）。编码交给 Python：
  - 真控制台（`isatty=True`）：PEP 528 —— CPython 走 `WriteConsoleW`，字节层恒 UTF-8、**绕过活动代码页** → 不需要 chcp。
  - 管道/重定向（`isatty=False`）：退回 locale 编码（gbk），打印 `⚠️` 会 `UnicodeEncodeError` → 靠 **`-X utf8`**（PEP 540）钉住 UTF-8。
  - 该文件刻意**纯 ASCII**（cmd 按*当前*代码页逐行解析批处理）。
  - **机制与实测数据详见 `docs/OPERATIONS.md` §17**（三层责任链：控制台 / 字节接口 / MSVCRT；PEP 528/529/540）。
- 解释器可覆盖：Windows `set MHI_PYTHON=py -3.12`；WSL `export MHI_PYTHON=/path/to/python`（默认 `~/ai-services/bin/python`）。
- 装进 PATH 后可全局敲：
  - Windows：把项目根 `<project-root>` 加进 PATH。
  - WSL：`ln -sf <project-root>/mhi.sh ~/.local/bin/mhi`

---

## 总览

| 命令 | 用途 | 危险度 |
|------|------|--------|
| `mhi-import-book` | 导入图书/游戏剧情（md/txt/epub/pdf，chunk 层幂等） | 低 |
| `mhi-search` | 检索知识库（向量 + 全文混合，chunk 级） | 无 |
| `mhi-find-book` | 书级语义检索（找书：哪本书讲 X；只排序不判存在性） | 无 |
| `mhi-tag` | 管理书/聊天的 tag + 全库体检（`--audit`，只读） | 无 |
| `mhi-remove-book` | 按路径或 guid 删除已入库的书 | ⚠️ 破坏性 |
| `mhi-cleanup-images` | 清理引用计数为 0 的图片文件 + 记录 | ⚠️ 破坏性 |
| `mhi-cleanup-emb-cache` | 清理嵌入缓存（过期 / 全部 / 统计） | ⚠️ 破坏性 |
| `mhi-import-chat` | 导入 AI 对话记录 | 低 |
| `mhi-import-onenote` | 导入 OneNote 笔记 | 低 |
| `mhi-clean-chat` | 清洗单个对话文件 | 无 |
| `mhi-reset-db` | 删库重建（读 `scripts/init_db.sql`） | 🔴 高危 |

---

## mhi-import-book — 图书导入（主入口）

```
python -m src.cli.mhi_import_book <path> [选项]
```

### 参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `path` | 必填 | 文件（单本）或目录（批量）。目录支持 .md + .txt（递归，自动跳过隐藏文件与 MinerU 原始 PDF） |
| `--bucket` | 图书 | 目标桶 |
| `--tags` | 空 | 手动 tag，逗号分割（与子目录 tag 叠加） |
| `--recursive` | off | 单目录模式下递归子目录（子目录分派模式内部一律递归） |
| `--force` | off | 重新导入已存在的书（旧记录级联删除，**同时先清旧脚注**） |
| `--no-dir-tag` | off | 禁止一级子目录自动 tag（整根目录一次导入，tag=根目录名） |
| `--exclude-dirs` | 空 | 排除的一级子目录名，逗号分割；排除全部 → 回退单目录模式 |
| `--parallel` | 2 | 并行书数。**>2 有极大锁竞争风险**（chunk 表两个 FULLTEXT 索引并发插入互斥，会 Lock wait timeout），CLI 会打黄色警告 |

### 行为要点

- **目录分派**：根目录存在一级子目录 → 每个子目录一次导入，tag = 子目录名
  （`--tags <corpus-root>` + 子目录"化学" → 每本书 tags = "<corpus-root>,化学"）
- **幂等**：已导入的书自动跳过（断点恢复 = 重跑同一条命令）
- **Ctrl+C 两级**：第一次中断当前书（该书未入库，继续下一本）；第二次全停（已导书保留）
- **胜利退出**（无异常）自动清理过期嵌入缓存并打印条数；异常/Ctrl+C 不清理
- **支持格式**：.md / .txt（自动编码检测：BOM→UTF-8→GB18030）/ .epub / .pdf
- 图片管线：MinerU 引用解析 + 硬链接/复制 + OCR（server_paddle 在线时）
- 嵌入：缓存命中复用（MariaDB `embedding_cache`），未命中调 server_bge

### 示例

```bash
# 全量导入素材库（幂等，md/txt 都会扫）
python -m src.cli.mhi_import_book <corpus-root> --tags <corpus-root> --parallel 2

# 强制重导单本（补 OCR / 修数据）
python -m src.cli.mhi_import_book "<corpus-root>/物理、天文、数学/量子力学I (...)/hybrid_auto/量子力学I (...).md" --force

# 排除某些分类
python -m src.cli.mhi_import_book <corpus-root> --exclude-dirs "地质学,废弃"
```

---

## mhi-search — 检索

```
python -m src.cli.mhi_search <query> [选项]
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `query` | 必填 | 查询文本 |
| `--category` | 全部 | 限定类别 book/chat/onenote |
| `--bucket` | 全部 | 限定桶 |
| `--tags` | 全部 | 限定 tag；`OMNIA` = 搜全部数据（与不传同义） |
| `--limit` | 10 | 返回条数 |
| `--fulltext-only` | off | 仅全文搜索（无命中会自动补向量路并在 `search_meta.fallback` 标注） |
| `--vector-only` | off | 仅向量搜索（无命中会自动补全文路并标注） |
| `--plain` | off | 只输出结果数组（旧形状；默认输出含 `search_meta` 的对象） |

**返回结构**（默认）：

```json
{
  "results": [ ... ],
  "search_meta": {
    "mode": "hybrid",                       // 你请求的模式（fulltext_only / vector_only / hybrid）
    "returned": 10,
    "fallback": null                        // 非 null = 发生了自动回退
  }
}
```

`fallback` 形如 `{"to": "vector_only", "reason": "全文路无命中", "returned_after": 2}`——
**看到它就知道"不能据此判断库里没有"，只是原来的搜索方式打不中**。
`results` 为空且 `fallback` 为 `null` → 双路都无命中（这才是"库里大概率没有"的信号）。

输出：JSON（`ensure_ascii=False`，中文可读）。

### tag 语义（权威定义：`src/core/tags.py`）

| 输入 | 语义 |
|------|------|
| 不传 / `--tags ""` / `--tags OMNIA` | 搜全部数据（不生成 tag 谓词）——三者**同义** |
| `--tags 化学` | 只搜打了该 tag 的书/对话 |
| `--tags a,b` | 任一命中即入选（OR） |
| `--tags "OMNIA,化学"` | **报错**：ALL OR &lt;tag&gt; 恒等于 ALL，无意义 |
| `--tags select`（SQL 保留字） | **报错**：保留字作 tag 无信息量且制造结构歧义 |

写入侧约束（`mhi import --tags` / `mhi tag --add` / 目录名自动 tag 一并适用）：

- `OMNIA` 是保留哨兵，**永不落库**，写入即报错；
- tag 不得是 SQL 保留字（`SELECT`/`IN`/`DATA`…），也不得含单引号、双引号、反引号、
  分号、反斜杠及换行/制表；
- 逗号是分隔符：多值输入按逗号切分；**目录名自动 tag** 含逗号会直接报错
  （否则会被静默切成两个 tag）；
- 这套校验是「输入卫生 + 早失败」，**不是注入防线**——真正的防线是全项目沿用绑定参数。

存量体检（都应返回 0 行）：

```sql
SELECT COUNT(*) FROM documents_book WHERE FIND_IN_SET('OMNIA', tags);
SELECT COUNT(*) FROM documents_book WHERE tags LIKE ',%' OR tags LIKE '%,' OR tags LIKE '%,,%';
```

---

## mhi-find-book — 书级语义检索（找书）

```
python -m src.cli.mhi_find_book <query> [选项]
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `query` | 必填 | 主题描述（如「月球极区的水冰」） |
| `--bucket` | 全部 | 限定桶 |
| `--tags` | 全部 | 限定 tag；`OMNIA` = 搜全部数据（与不传同义） |
| `--limit` | 10 | 返回书数 |

输出：JSON `{query, books[], signal{}, note}` —— `books[].score = 1 − 余弦距离`；
`signal` 只给四个数字（`corpus_books / top1_distance / gap / z`），**不给存在性结论**。

**它只排序、不判断"库里有没有这个题材"**。标定数据（真实 104 条"题材确实在库"的查询上，
任何阈值都会误伤约 27%）与三个检索/发现工具的分工，见 `docs/真实检索查询分布案例.md` §五、§八。

```bash
mhi find-book "月球极区永久阴影区的水冰" --limit 5
mhi find-book "光学干涉仪的设计原理" --tags 光学系列
```

---

## mhi-tag — 标签管理 / 全库体检

```
python -m src.cli.mhi_tag <guid> [选项]
python -m src.cli.mhi_tag --audit [选项]        # 全库体检，不需要 guid
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `guid` | 增删查时必填 | 目标记录 id（`--audit` 下不需要） |
| `--add` | 空 | 添加 tag（逗号分割）；哨兵/保留字/非法字符一律报错 |
| `--remove` | 空 | 移除 tag |
| `--list` | off | 列出当前 tag |
| `--audit` | off | tag 体检（只读）：空 tags / 哨兵落库 / CSV 破损 / 保留字与非法字符 |
| `--sample` | 20 | 体检每类问题最多列出的样本行数 |
| `--category` | book（`--audit` 下 all） | book/chat/onenote；`all` = 三张主表全扫 |

无操作子命令时等价于 `--list`。输出 JSON。

**`--audit` 语义**：`healthy=false`（有任何一类问题）时**退出码 1**，stdout 仍是完整 JSON，
stderr 额外给一行摘要——便于脚本/巡检接入。判据与写路径校验同源（`src/core/tags.py`）。

```bash
mhi tag --audit                    # 全库（book + chat + onenote）
mhi tag --audit --category book    # 只查书
mhi tag --audit --sample 50        # 每类问题列 50 行样本
```

---

## mhi-remove-book — 删除书（⚠️）

```
python -m src.cli.mhi_remove_book [<path>] [--guid G1,G2] [选项]
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `path` | 与 `--guid` 互斥 | 文件=精确匹配该书；目录=前缀匹配删除该目录下全部书 |
| `--guid` | 与 `path` 互斥 | 按 guid 删除：逗号分割（`--guid g1,g2`），亦可重复给出（`--guid g1 --guid g2`）；主键精确匹配，**大小写不敏感**。必须给 `path` 或 `--guid` 之一 |
| `--yes` | off | 跳过确认 |
| `--dry-run` | off | 只列出匹配的书不删除 |

**guid 模式退出码与安全阀**：命中 0 个 → 打印"无匹配 guid"并 `exit 1`；部分未命中 → 列出未命中清单，**必须显式加 `--yes`** 才继续删除已命中项（防 guid 打错导致"以为删 3 本、实际删错范围"）。

**预览输出**：`guid + 书名 + chunks + source_path`——guid 对人零直觉，必须靠路径/书名二次核对。

删除语义：chunks 级联删除（FK `fk_chunk_book`）；page_footnotes 由 `fk_footnotes_book` 级联 **且** 代码里再显式删一遍（双保险，对未打 FK 的老库同样正确）；**images 保留**（全局去重表，重导时 MD5 命中复用）。前缀匹配用 LOCATE（避开 LIKE 反斜杠转义坑）。

两模式内部单轨：先解析成 guid 列表，再按 guid 删除。

### 示例

```bash
# 按 guid 删（guid 来自 mhi-search 结果或 MCP list_books）
python -m src.cli.mhi_remove_book --guid 082d08ca-e324-450b-b2b9-9cc1cddea274 --dry-run
python -m src.cli.mhi_remove_book --guid g1,g2,g3 --yes

# 按路径删（原有用法不变）
python -m src.cli.mhi_remove_book "<corpus-root>/废弃分类" --dry-run
```

---

## mhi-cleanup-images — 图片清理（⚠️）

```
python -m src.cli.mhi_cleanup_images [--yes]
```

- 列出 `ref_count = 0` 的图片（孤儿：无任何书引用），`--yes` 确认后删除物理文件 + 记录
- 无 --yes 时只预览

## mhi-cleanup-emb-cache — 嵌入缓存清理（⚠️）

```
python -m src.cli.mhi_cleanup_emb_cache [选项]
```

| 参数 | 说明 |
|------|------|
| （无） | 仅清理过期条目（expires_at < NOW()） |
| `--all` | 清空整张缓存表（需二次确认，所有向量将重新计算） |
| `--stats` | 只统计不删除（条目数/过期数/数据量 MB） |

---

## mhi-import-chat — 对话导入

```
python -m src.cli.mhi_import_chat <path> [选项]
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `path` | 必填 | 文件或目录 |
| `--bucket` | AI对话 | 目标桶 |
| `--tags` | 空 | 手动 tag |
| `--no-clean` | off | 跳过清洗（调试用） |
| `--summary` | 空 | 手动指定摘要 |

## mhi-import-onenote — OneNote 导入

```
python -m src.cli.mhi_import_onenote <path> [选项]
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `path` | 必填 | 文件或目录 |
| `--bucket` | OneNote笔记 | 目标桶 |
| `--tags` | 空 | 手动 tag |
| `--auto-tags/--no-auto-tags` | 开 | 从子目录名自动 tag |

## mhi-clean-chat — 对话文件清洗

```
python -m src.cli.mhi_clean_chat <input> [选项]
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `input` | 必填 | 待清洗文件（须存在） |
| `--output` | 覆盖输入 | 输出路径 |
| `--summary` | off | 额外输出摘要 |

---

## mhi-reset-db — 重建数据库（🔴 高危）

```
python -m src.cli.mhi_reset_db [--yes]
```

- 删除并重建整个数据库（`config database.name`，默认 memoria_del_hielo）
- DDL 来源：`scripts/init_db.sql`（缺失则拒绝执行）
- 嵌入缓存目录/表不受影响
- **先导出现有数据再执行**

---

## 环境与前置

- **venv（WSL）**：`<home>/ai-services/bin/python`（导入/搜索/CLI）
- **Windows 原生解释器（`mhi.cmd` 用）**：`<Python 安装目录>\python.exe`，
  依赖 `mariadb click regex jieba`（+ httpx/Pillow/numpy/rich 等见 `pyproject.toml`）。
  本项目**不依赖 WSL**：后端是 MariaDB/OCR/Everything 等 TCP 端点，
  TCP 之上的层与操作系统无关；路径两个方向都有兼容（`_to_windows_path` 对
  非 `/mnt/` 路径原样返回；`_find_image` 的 Windows 盘符分支在 Windows 上
  落空后由「直接访问」分支兜住）。
- **服务依赖**（导入时）：
  - OCR：`server_paddle.py`（仓库根目录）→ 127.0.0.1:**9008**，`GET /health` 返回 200
    启动：`python server_paddle.py --port 9008`（我本地另用一个 `server_paddle_wrap.sh`
    包装：激活 venv + 固定端口 + 线程数；属本地环境脚本，未随发布版提供）
    ⚠️ 9007 已被 **DSH web** 占用，别写 9007；端口须与 config.toml `[ocr].endpoint` 一致
  - 嵌入：`server_bge.py`（仓库根目录）→ 9005 本地优先 / 9006 远端优先
- **MariaDB**：Windows 侧 127.0.0.1:9002（mhi_user）
- **维护脚本**（`scripts/`）：`split_pdf.py`（拆卷）、`purge_orphan_footnotes.py`（清孤儿脚注）、`verify_chunk_clean.py`（chunk 校验）、`diag_*`/`diagnose_*`（诊断，不入库）；`migrate_*`（结构迁移，效果已并入 init_db.sql）与夜间补 OCR 等一次性脚本属本地历史，未随发布版提供
