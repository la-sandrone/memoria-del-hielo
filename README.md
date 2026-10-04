# Memoria del Hielo

> **个人娱乐作品。** 
> 
> 之前遇到个神奇的AI客户端，知识库后台是某个老版魔改版libsql，结果库大了检索五分钟，还不给单独搜索页面。
> 正好作为Discuz! 5.X时代就来的老登，对MariaDB这套东西还算熟悉，遂用MCP Tool Call外加MariaDB的向量搜索以及BGE模型之类的东西攒了一套玩具工具。
> **纯娱乐，不保证能长期用啊，更新看我自己需求。**
> 充分学习PECMD和PECMD2012（ https://bbs.wuyou.net/forum.php?mod=viewthread&tid=205402 ）精神：短小精干，运行迅速（虽然我这里本质是大量复用已有轮子，悲）

## 具体发生了什么

同一份知识库（约16万chunks），检索一次：

| 方案 | 耗时 |
|---|---|
| 某神人客户端（锁死 libsql 0.5.22 老核心） | 5分53秒 |
| 同一份库，上正经MariaDB | 0.88秒 |

说白了干大活还是用MariaDB这种正经东西吧，要不然塞了若干本书以后体积膨胀到50+GB也太逆天了。

## 闲话经验

> 纯粹笔记性质。

**1. MariaDB 向量检索的两个实测坑**（63s → 1.2s 的完整考古，`src/core/search.py` 有注释）

- `VEC_DISTANCE_COSINE(col, VEC_FromText(?))` 里的向量会被**逐行重新解析**——
  21KB 文本 × 16 万行 ≈ **33 秒**。把它塞进派生表 `(SELECT VEC_FromText(?) AS qv) q`
  就只解析一次，同一查询 **0.29 秒**。
- 把 `content`(TEXT) 放进 `ORDER BY` 的 SELECT 列表会**强制临时表落盘**（6.06s vs 0.39s）——
  排序只带 `chunk_id + distance`，payload 留到外层按 id 回填。

**2. 查询分析总结**（`docs/真实检索查询分布案例.md`）
190 条真实检索查询（13 个主题簇）、空结果 100% 归因，以及一个**阴性结论**：
文献主推的 Score Gap 在本场景反而更差（AUROC 0.687 vs 原始相似度 0.859）——
场景不同（书级主题型 vs chunk 级推理型），**结论不能照搬**。

**3. 即弃连接**（`src/core/db.py`）
不用连接池：每次调用新建连接、用完即焚。空闲时 `Threads_connected=1`、峰值 = 并行数；
**"连接池里烂连接捞不回来"**那类问题从架构上消失，代价是每调用一次连接开销（本地库可忽略）。

**4. 为 LLM 写的自文档**（`INDEX.md` / `PROJECT_SPEC.md` / 每个源文件头）
模块脉络 + 关键词速查 + "近似入口点"（"想改 X → 去哪个文件"），
目的是让 AI 能**定向读取**而不是每次全仓扫描。另有一套 `mhi tag --audit` 之类的自检工具。

## 目录与工具（一句话版）

| 东西 | 说明 |
|---|---|
| `src/core/` | 检索（双路 + 书级）、切块、Unicode 政策、嵌入客户端、即弃连接 |
| `src/importers/` | 图书（含 MinerU 产物）/ 对话 / OneNote 三类导入器 |
| `src/cli/` | `mhi` 子命令：`import` `search` `find-book` `tag` `rm` `images` `emb` … |
| `src/mcp_server.py` | MCP 只读工具：`search_docs`（找内容）/ `find_books`（找书）/ `read_book_chunk`（钻取）/ `list_*` |
| `scripts/` | 建表/迁移 SQL、去重、诊断 |
| `docs/` | 操作手册、CLI速查、查询分析 |

## 跑起来？（大概率跑不起来）

依赖：MariaDB 12.x（向量索引）、嵌入&重排服务（看`server_bge.py`），OCR服务（看`server_paddle.py`），配置模板见`config.toml.example`

Python 包（细节见`pyproject.toml`；装核心那组就够跑 CLI／检索／MCP）：

- **核心**：`mariadb` `httpx` `requests` `numpy` `Pillow` `jieba` `chardet` `regex` `click` `rich` `mcp` `fastmcp`
  ⚠️ `mcp` 和 `fastmcp` 是**两个**包，都得装——少一个 MCP 服务就起不来（这里有前科）
- **可选**：`PyMuPDF`（PDF 提取）、`EbookLib`（EPUB 提取）——都是懒加载，不装也能跑其它格式
- **开发**：`pytest` `pytest-asyncio`
- **两个常驻服务各自的**（核心用不着）：`server_bge.py` → `fastapi` `uvicorn` `pydantic` `sentence-transformers` `torch`；`server_paddle.py` → `paddleocr` `paddlepaddle`（要 GPU 就换 `paddlepaddle-gpu`）

安装：看`scripts/init_db.sql`建库，然后`mhi import`导入书就成，记得把`server_bge.py`和`server_paddle.py`这俩挂在后台。

应用：要么CLI，要么MCP stdio方式挂`python3 -m src.mcp_server`就行。

## 许可

- **代码：AGPL-3.0-or-later**（与依赖的 PyMuPDF / EbookLib 同为 AGPL 保持一致）
- **文档（`docs/`、根目录 `*.md`）：CC-BY-4.0**

```text
SPDX-License-Identifier: AGPL-3.0-or-later
```

## 免责

个人娱乐作品，**按原样提供**，不承诺任何东西：不承诺能跑通、不承诺数据安全、不承诺维护或答疑。有空没空看一眼。


