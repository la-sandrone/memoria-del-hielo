# Memoria del Hielo

> **个人娱乐作品。** 
> 
> 之前遇到个神奇的AI客户端，知识库后台是某个老版魔改版libsql，结果库大了检索五分钟，还不给单独搜索页面。
> 正好作为Discuz! 5.X时代就来的老登，对MariaDB这套东西还算熟悉，遂用MCP Tool Call外加MariaDB的向量搜索以及BGE模型之类的东西攒了一套玩具工具。
> **纯娱乐，不保证能长期用啊，更新看我自己需求。**
> 充分学习PECMD和PECMD2012（https://bbs.wuyou.net/forum.php?mod=viewthread&tid=205402）精神：短小精干，运行迅速（虽然我这里本质是大量复用已有轮子，悲）


> ## ⚠️ 给所有读者（不管你是 LLM 还是人类）
>
> **这个项目处理的数据会疯狂拷打 Zipf 定律和 Unicode 的边缘情况。**
>
> 语料里同时有：中文（无空格分词、单字也要能搜）、拉丁变体（`é ü ñ`、古典长音
> `ā ă ē ĕ`）、希腊文（含词尾 sigma `ς`/`σ`，`lower()` 不合并）、西里尔文
> （`Византийская библиотека`）、古斯拉夫语古字母（`ѣ ѫ ѳ`），以及大量只出现
> 一两次的专名与自造词——**Zipf 长尾的尾巴比一般语料重得多**。
>
> 由此产生的坑有个共同特征：**它们不报错**。缺 `re.UNICODE` 只是少匹配；
> NFC/NFD 不一致只是查不到；按码点截断只是显示乱码；`min_token_size=3`
> 只是让两字中文词进不了索引。**全都「能跑」，错误是沉默的、延迟发作的、
> 只有拿真实材料去喂才会暴露的。**
>
> 所以，改动任何**分词 / 索引 / 匹配 / 截断**逻辑之前，先问自己一句：
>
> **「这段代码假设了文本长什么样？这个假设在俄文、希腊文、变音拉丁文、
> 以及只出现一次的自造词上还成立吗？」**
>
> 参考实例（都是真踩过的）：
> - `[A-Za-z0-9_]+` 当"非 CJK 分支" → 俄希拉丁整段被丢，tokenized 列为空
> - `innodb_ft_min_token_size = 3`（默认）→ 中文两字词全丢，FULLTEXT 返回 0 而 LIKE 能中
> - jieba 按词典切分 → 自造词/专业术语切错边界，约 30% 漏搜
> - 按码点切块 → emoji ZWJ 序列（`👨‍👩‍👧‍👦` 是 7 个码点）、天城文连字被切成两半
>
> 切分的最小正确单位是**字素簇**（UAX #29，Python 里用 `regex` 的 `\X`），
> 不是字节、不是码点、也不是"看起来像几个方块"。


## ⚠️ 数据量已经不是玩具了 —— 请严肃对待性能

现有规模：`documents_book_chunk` **62.5 万行 / 约 17 GB**（正文 1.93 GB + bigram tokenized 2.10 GB + 1024 维向量 2.5 GB + 索引），另有 `documents_book` 1.8 GB、`page_footnotes` 340 MB；全库总计 **32.6 GiB**。
FULLTEXT 索引重建一次是**小时级**操作，别当成秒级 DDL 来规划。

**任何涉及全表操作的设计决定，先算成本再动手。**

### MariaDB 与 MySQL 的几处微妙区别（都是真踩过的）

网上多数 FULLTEXT / DDL 的"最佳实践"是照着 MySQL 写的，**照搬到 MariaDB 会出事**：

| 事项 | MariaDB 的行为 | MySQL 的行为 | 后果 |
|---|---|---|---|
| `ALTER TABLE ... ALGORITHM=INPLACE` 对 FULLTEXT 索引 | **仍然 rebuild 整表**（INPLACE 只省锁，不省 IO） | `DROP INDEX` 是 In Place，不重建 | "摘索引 → 改数据 → 建索引"在 MariaDB 上要**复制表两趟**（实测 91 min + 82 min） |
| 同一条语句里 `DROP INDEX a, ADD FULLTEXT a(...)`（定义相同） | **被优化成空操作**：0 秒完成且**没有任何效果** | — | 必须拆成两条独立语句才对 |
| `SET GLOBAL innodb_ft_min_token_size` | **静默成功但完全不生效**（startup-only） | 同类行为 | 必须改 `my.ini` + 重启服务 |
| `innodb_ft_min_token_size` 的计数单位 | **字符**（不是字节） | 也按字符 | 默认 3 ⇒ **所有 1–2 字中文词进不了索引**，FULLTEXT 返回 0 而 LIKE 能命中 |
| `VECTOR` 类型 / `VEC_DISTANCE_COSINE` / `VECTOR KEY` | 11.7+ **原生支持** | **没有** | 迁到 MySQL 得换第三方向量扩展；`VEC_DISTANCE` 会报 "index is not found"，必须写 `VEC_DISTANCE_COSINE` |
| 改全局变量所需权限 | `SUPER` | 8.0 起用 `SYSTEM_VARIABLES_ADMIN` | 业务账号 `SET GLOBAL` 会 `ERROR 1227 Access denied` |
| `information_schema.innodb_buffer_pool_pages` | **不存在** | 存在 | 查缓冲池状态得换路径（本项目已踩） |
| **`MODIFY COLUMN`（哪怕只改 `COMMENT`）在带 FULLTEXT 的表上** | **只能 `ALGORITHM=COPY`（重建整表）**：`INSTANT`/`NOCOPY` 都被拒，Reason 是 `InnoDB presently supports one FULLTEXT index creation at a time` | 无 FULLTEXT 时 `INSTANT` 可行（实测 **25.9 ms**） | **改注释别单独做**——攒到下次必须重建（改列类型/加索引）时合并进同一条 ALTER |
| `GRANT ... ON db_name.*` 里的 `_` | 被当**通配符**（与 `%` 同） | 同 | 授权后会多出一行"未转义"的 `mysql.db` 记录；两行并存时**只命中其中一行**，导致 GRANT 加了却不生效（实测踩过） |

### 性能纪律

1. **先看量级**：行数 × 行宽 = 要搬多少字节。14 万行 × 17 KB/行 ≈ 2.4 GB，
   在内存里扫一遍约 385 ms（6 GB/s 量级）；乘以批次数就知道能不能接受。
2. **再问索引**：`EXPLAIN` 一次的成本远低于猜。缺索引的 UPDATE 会因全表扫
   从 1 ms 变 385 ms（本项目实测，**188 倍**）。
3. **FULLTEXT 的 DDL 按小时算**：MariaDB 上它是 rebuild 整表，不是加个 B 树。
   大表（>10 GB）预留 1–2 小时，并且**别和别的重 IO 任务同时跑**。
4. **影子表切换**：建新表 → 搬数据 → 建索引 → RENAME → **立刻删旧表**
   （否则白占磁盘，还污染 buffer pool 的 LRU）。
5. **缓冲池要装得下热数据**：数据量超过 `innodb_buffer_pool_size` 后，
   命中率会断崖式下降。
6. **失败要重试、断点要续传**：这类长任务必须能被打断（Windows 自动更新
   随时可能杀掉你的后台进程），设计时就要有幂等 + 水位线。

> 完整的两次误判与 188 倍修复过程见工作区笔记
> `用户笔记/检索/等待归因实验-dsh_message嵌入写回.md`。


### 向量检索是**近似**的（HNSW / ANN）—— 别拿它做精确查找

MariaDB 的 `VECTOR KEY` 是 **HNSW（近似最近邻）**，不是精确 KNN。
**recall 可以低于 100%，而且它不会告诉你漏了什么。**

2026-10-25 在 `documents_book_chunk`（**62.5 万点**）上用「拿一条 chunk 的向量查它自己」实测：

| 指标 | 结果 |
|---|---|
| 自查询 **top-1 命中自己** | 12/13 = **92%** |
| 自己在 **top-10** 里 | 13/13 = **100%** |
| `mhnsw_ef_search` 20 → 400 | **毫无变化**（MariaDB 12.3.2 上该参数未生效） |

**排除过程**（三步，值得记）：

| 测试 | 结果 | 结论 |
|---|---|---|
| `IGNORE INDEX (idx_vec_chunk)` 暴力扫描 | ✓ 自己排第一，距离 −0.000000 | 数据没问题 |
| Python 手算余弦 `1 − a·b/(‖a‖‖b‖)` | ✓ 0.000000000 | 距离函数没问题 |
| `EXPLAIN` | `type=index, key=idx_vec_chunk`，无 filesort | 确实走了索引 |

⇒ **数据对、算得对、计划对 —— 是索引给了近似答案。**

### 两条纪律

1. ✅ **可以用它做语义召回**（top-k）。实测 top-10 recall 100%，
   「找意思相近的段落」完全够用。
2. ❌ **绝不要用它做精确查找**——自查询、判重、找重复、验证「某条是否已入库」。
   那会撞上那 8%。精确查找走 **FULLTEXT 或 LIKE**。

表越小 recall 越高（图小、连通性好），但**不要假设小表就是 100%**。

> 另注：`VEC_DISTANCE`（不带 `_COSINE`）会报 `index is not found`；
> 建索引时 `DISTANCE=cosine` 必须显式写，否则静默降级为
> `mhnsw_default_distance`（euclidean）。这两个坑都踩过。

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

**3. 即弃连接 + 攒批**（`src/core/db.py`、`src/core/sqlbatch.py`）
不用连接池：每次调用新建连接、用完即焚。空闲时 `Threads_connected=1`、峰值 = 并行数；
**"连接池里烂连接捞不回来"**那类问题从架构上消失，代价是每调用一次连接开销（本地库可忽略）。

但"即弃"不等于"一条语句一次连接"：逐条 upsert 在大书上会以 ~6000 次连接/本
耗尽 Windows 临时端口，客户端报 `WSAEADDRINUSE 10048`（跑到一半突然连不上）。
所以写入走攒批——N 行拼一条多值 SQL，每次 flush 一条连接（3109 chunk / 6218 次 → ~108 次）。
攒批器同时在首次发送前探测 `max_allowed_packet` 并按 75% 压顶，撞 `1153` 砍半拆批不丢行。

**4. 知识库的边界：我们只对 md 负责，不越俎代庖处理 PDF**

这条是**架构边界**，也是能力声明。

**我们负责的**：MinerU 产出的 `.md`，以及它可能产出的中间文件
（v3 的 `_middle.json` / `_content_list*.json`，v4 的 `middle_json.json` /
`structured_content.json`）。据此我们可以知道：产物形态（dir / full / flat）、
`parse_mode`、脚注结构、图片类型、以及**从 md 本身就能算出来的风险信号**
（例如康熙部首码点、PUA）。

**我们不负责的**：源 PDF。字体表怎么撒谎、ToUnicode 写没写、CMap 程序吐出什么、
哪个字体把 ASCII 码位当了字形槽 —— 那是 **PDF 准备阶段**的事，不在本库内做。

**那我们做什么？** 只做一件事：**跟 LLM 说一声**。三个返回正文/书目的工具
（`search_docs` / `find_books` / `read_book_chunk`）的 docstring 里都带了这句，
而 tool 描述**常驻 LLM 上下文**：

> ⚠️ 文本可信度：本库正文来自 PDF 的 md 产出，**文本层不保证等于原文**。
> **引用前先看上下文是否说得通；不要逐字照抄生僻字/符号。**

为什么只做这一句、不去查 PDF：PDF 的历史包袱没有上界，
**上游自己都不可能解决 100%，我们也不假装能**。把"可疑"如实告诉 LLM，
比在库里堆一层注定查不全的取证更诚实也更省事。

一份完整的实测台账（含 50 个槽位字体、27% 页面、以及我们自己的踩坑记录）在
`docs/为什么不要试图一个月搞懂Unicode以及我们给MinerU擦了多少屁股.md`。

**5. 为 LLM 写的自文档**（`INDEX.md` / `PROJECT_SPEC.md` / 每个源文件头）
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
| `mineru_batch.py` | **配套工具**：批量调用 MinerU（v3/v4 双适配），产出本库能直接导入的 md / zip |
| `server_bge.py` / `server_paddle.py` | 常驻服务：嵌入&重排（9005/9006）/ OCR（9008） |

### 配套工具：`mineru_batch.py`

本库的正文来自 MinerU（PDF / Office / 图片 → Markdown）。`mineru_batch.py` 是**批量调用
MinerU 的外壳脚本**——它自己不做解析，只是把"一堆文档 + 一次解析档位/序列化"翻译成一段
bash 脚本去跑，然后盯着内存回收、断点续跑与失败结算。独立运行，不 import 本库任何东西。

```bash
python mineru_batch.py -r "D:\论文" -o "D:\论文\_mineru_out" --tier standard
python mineru_batch.py --dry-run -o "D:\out" a.pdf b.docx c.epub   # 只看生成的脚本
```

- **v3 / v4 双适配**（两代 CLI 语法完全不同）：自动判代、按版本分流扩展名、
  按版本算 Win32 路径长度预算、skip 判据合并两代产出
- **v4 默认产出 zip**（`--format zip`）：带 tier / parse_mode / 页号 / 脚注结构 / 图片类型
  —— 本库的 `mineru_form='full'` 与脚注表就是这样拿到的；`--v4-format markdown` 可退回扁平 md
  （体积小，但元数据全丢，且图片是 base64 内联）
- **用前只需改一处**：文件顶部的 `ENV_ACTIVATE_CMD`（激活 MinerU 环境的那条命令）。
  写错会**当场报错**并给出两条出路，不会默默生成一份注定跑不完的脚本
- v4 默认「一书一进程」——上游 VLM 后端有内存泄漏（MinerU #4243，已 closed as not_planned），
  进程边界是唯一不依赖上游的回收机制

产出交给 `mhi import` 即可：`.zip` 直接收（本库解到 `cache/mineru_unzip/` 再导入），
扁平 `.md` 也收（内联图片会被落盘换成普通引用）。MinerU 自己的 `--format middle_json`
产出**不收** —— 它没有正文与图片文件，不是完整结果包。

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

以及一条**能力边界**：PDF 是一坨历史包袱，**我们只尽力**（检测 + 上报 + 绕过去），
**不承诺把任何一份 PDF 读对**。用它的读数时**请留意上下文** —— 见「闲话经验」第 5 条。


