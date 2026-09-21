# ░ 项目索引 ░

## 模块脉络
1. PROJECT_SPEC.md — 全架构规格书，唯一权威来源（796行/18节）。
   关键词: 架构, 数据库, API, 清洗规则, 图片管线
2. docs/OPERATIONS.md — 操作手册与检查单（前置依赖→建库→导入→搜索→MCP→冒烟测试）
   关键词: 操作, 检查单, 冒烟测试, 常见问题
3. config.toml — 项目配置（数据库连接、嵌入端点、OCR、Everything、路径）
   关键词: 配置, MariaDB, BGE, PaddleOCR
4. scripts/init_db.sql — 数据库建表脚本（fresh install 与迁移后 schema 一致）
   - documents_book（书目卡片：source_path_hash 幂等锚点 + 书目向量 + 原文档案；书级全文索引已退役）
   - documents_book_chunk（书桶检索主战场：双 FULLTEXT + VECTOR HNSW + FK CASCADE）
   - chat/onenote 主表 + images + 配置表；存量库走 scripts/migrate_001_chunk.sql
   关键词: DDL, VECTOR(1024), chunk, content_tokenized, source_path_hash, 断点恢复
5. pyproject.toml — 项目元数据、依赖项、CLI 入口点注册
   关键词: 依赖, CLI, hatchling
6. .gitignore — Python 项目忽略规则
7. src/core/ — 共享核心层
   · config.py — TOML 配置加载，get(key) 点号路径，MHI_DB_PASSWORD 环境变量
   · db.py — MariaDB 即弃短连接（无状态：execute_sql 每次新建连接用完即焚、
     transaction 每事务一连接；连接池/信号量/烂连接问题从架构上消失，
     max_allowed_packet 由服务端控制）
   · embedding.py — server_bge.py HTTP 客户端（嵌入 + 重排 + JSONL 缓存 + embed_batch 批量嵌入）
   · tokenizer.py — 共享分词工具（has_cjk + tokenize + tokenize_query）
   · unicode_utils.py — Unicode 数字面量工具（唯一权威实现）：可见码点口径、
     PUA→U+FFFD 等长净化、UAX#29 字素簇边界（regex \X）、可见/字节前缀和；
     将来换 PyICU 只改此文件
   · chunker.py — 结构感知切块器 v2（净化入口 + 可见口径 + 字素簇对齐 + 精确偏移；
     三偏移四长度 + has_pua 输出；max_chars 可见码点硬护栏 + 码点×2≤8192 双保险）
     + build_book_card 书目卡片
   · mineru_postprocess.py — MinerU 输出后处理（脚注提取 + 图片路径重映射）
   · search.py — v2 双路搜索：book 走 documents_book_chunk（chunk 级 + 书级聚合 +
     每书限流），has_pua 回查全文表；chat/onenote 整档粒度；CJK 向量0.8/全文0.2 不变。
     另含 find_books()：书级语义检索（书名+目录近邻，只排序不判存在性）；
     两处向量 SQL 都遵守「派生表解析一次 + 排序/payload 分两段」形状（见该文件注释）
   · tag_manager.py — Tag 增删改查（逗号分割 tags 字段；写入前逐元素校验）+ audit_tags 全库体检
   · tags.py — tag 语法权威层（唯一真相源）：哨兵 TAG_ALL=OMNIA（"搜全部数据"，永不落库）、
     读路径折叠（OMNIA=空串=不生成 tag 谓词）、写路径拦截（哨兵 / SQL 保留字元素级匹配 /
     结构性字符）；定位是输入卫生+早失败，不是注入防线（防线是绑定参数）
   关键词: 连接池已退役→即弃短连接, 事务, 嵌入缓存, 分词, 切块, Unicode政策, 字素簇, PUA净化, 双路搜索, 状态机, tag哨兵OMNIA, 找书find_books, 派生表形状
8. src/importers/ — 三类导入器（均写入 content_tokenized）
   · onenote_importer.py — YAML front matter → title/前50字 key_metadata，自动 tag
   · book_importer.py — v2 chunk 层：二段事务状态机（一段记账 status='processing'
     + expected_chunks → 二段 chunk 分批入库 + 转 done 同事务原子）；幂等 = done 且
     chunks 数相符才跳过，processing/破损强制重导；embed_batch 批量嵌入，
     目录模式只取 .md 避开 _origin.pdf，分类 tag = 扫描根目录名
   · chat_importer.py — 清洗 → 摘要 → 分词 → 嵌入 → 入库（⚠️ 整对话一个向量，
     超 8K token 会被 server_bge 静默截断——导入对话前需按轮次窗口改造）
   关键词: OneNote, 图书, AI对话, 幂等导入, 断点恢复, 8K截断
9. src/cleaners/chat_cleaner.py — AI对话清洗核心
   · 格式 A (**user:**) / 格式 B (## 🧑‍💻 User) 双格式支持
   · 移除 System Prompt / 工具调用 JSON / 搜索原文 / 推理铺垫
   · 规则摘要生成（≤200 字中文）
   关键词: 清洗规则, 轮次拆分, 摘要
10. src/cli/ — 11 个 CLI 工具 + 转发器
    · mhi_import_book.py（--parallel 默认3 / --no-dir-tag / --exclude-dirs /
      --tags 叠加；rich 进度表格；Ctrl+C 两级）
    · mhi_remove_book.py（删书：路径模式 文件=精确/目录=前缀 ｜ guid 模式
      --guid G1,G2；与 path 互斥，--dry-run/--yes）
    · mhi_dispatch.py（子命令转发器：mhi <子命令> → src.cli.mhi_<模块>；
      映射表 + 显示宽度对齐 + ASCII-only .cmd 的由来）
    · mhi_import_chat.py / mhi_import_onenote.py
    · mhi_clean_chat.py / mhi_search.py / mhi_tag.py（tag 增删查 + --audit 全库 tag 体检，退出码 1 表异常）
    · mhi_find_book.py（书级语义检索：找书；别名 `mhi find`）
    · mhi_cleanup_images.py / mhi_reset_db.py
    关键词: CLI, click, 删书, 并行导入, mhi 转发器, find-book
11. src/mcp_server.py — FastMCP 服务器，7 个只读工具（检索 + 发现 + 会话）
    · search_docs（找内容，chunk 级 + rerank 精排）/ find_books（找书，书级语义排序；
      **不判存在性**，理由见 docs/真实检索查询分布案例.md §五）/ read_book_chunk（S2B 钻取）
    · list_buckets / list_tags / list_books（元数据发现）
    · set_default_tags（本场会话默认 tag）
    · 管理操作（import/clean/tag）全部下沉 CLI（mhi-import-*/mhi-clean-*/mhi-tag）
    关键词: MCP Server, fastmcp, 钻取, 发现工具, 找书
12. tools/mcp_replay.py — MCP 请求重放工具（JSONL + @指令格式，stdin/stdout 调试）
    关键词: 重放, 测试, stdio
13. tests/ — 测试
    · test_chunker.py — 切块器 36 用例（章路径/打包/边界/overlap/巨段/目录跳过/
      物理上限 + Unicode 政策：PUA 净化四口径等长/has_pua/簇对齐/偏移连续性/度量自洽）
    · test_cleaner.py — 清洗器 + YAML 解析
    · test_config.py — 配置加载
    · test_tags.py — tag 语法层（哨兵同义/混用报错/保留字/结构字符/audit 分类）
    · test_find_books.py — 书级检索（SQL 形状护栏：派生表解析一次 + 全取算 signal；分数/边界）
    · replay/smoke.replay — MCP 冒烟测试重放（11步）
    · replay/e2e.replay — MCP 端到端工作流（13步 + @save 变量传递）
    关键词: pytest, 重放
14. docs/真实检索查询分布案例.md — <某创作项目>项目 190 条真实检索查询的分布台账
    （主题簇 / 空结果归因 / 书级存在性判据标定 0.859 AUROC 但阈值误伤 27% / 召回回归基线）
    关键词: 真实查询, 召回回归, 存在性判据, 标定, negative rejection, Magnitude Mirage

## 关键词速查表
| 关键词 | 模块 |
|--------|------|
| VECTOR, HNSW | 1, 4, 7 (search.py) |
| 全文搜索, CJK, jieba, content_tokenized | 1, 4, 7 (tokenizer.py+search.py) |
| server_bge, 嵌入, 重排 | 1, 3, 7 (embedding.py) |
| PaddleOCR, OCR | 1, 3, 7 (image_pipeline.py) |
| 图片去重, MD5, 硬链接 | 1, 4, 7 (image_pipeline.py) |
| AI对话清洗, System Prompt | 1, 9 (chat_cleaner.py) |
| OneNote, YAML front matter | 1, 8 (onenote_importer.py) |
| 五个桶, 三类别 | 1, 4 |
| 急停, 缓存, 断点续传 | 1, 7 (embedding.py) |
| config.toml, 配置 | 3 |
| 操作, 检查单, 常见问题 | 2 |
| MCP Server, FastMCP | 11 |
| MinerU, 脚注, page_footnotes | 7 (mineru_postprocess.py), 10 (CLI_USAGE.md §10) |
| chunk, 切块, 8K token 上限 | 3 ([chunking]), 7 (chunker.py) |
| 幂等导入, 断点恢复, source_path_hash | 4, 8 (book_importer.py) |
| 书级聚合, read_book_chunk, S2B | 7 (search.py), 11 |
| 即弃连接, 无状态, 状态机, 二段事务, expected_chunks, 幂等 | 7 (db.py, book_importer.py) |
| 删书, mhi-remove-book, LOCATE, --guid | 10 |
| rich 进度, 并行, Ctrl+C 两级, stop_check | 8 (book_importer.py) |
| 默认tag, set_default_tags, fallback链 | 3 ([search]), 11 |
| tag语义, OMNIA哨兵, 搜全部数据, 保留字拦截, 逗号切分 | 7 (tags.py), 3 ([search].default_tags), PROJECT_SPEC §4.6 |
| 找书, find_books, 书级语义检索, 书目卡片 | 7 (search.py), 10 (mhi_find_book.py), 11, 14 |
| 存在性判据, 召回回归, 真实查询, negative rejection, Magnitude Mirage | 14 (docs/真实检索查询分布案例.md), 7 (search.py 注释) |
| 服务端, 测试, pytest | 13 |
| 编码, 代码页, chcp, MSVCRT, PEP 528, PEP 540, -X utf8, isatty | 17 (OPERATIONS.md §17) |

## 近似入口点
- "想改数据库结构" → scripts/init_db.sql + PROJECT_SPEC.md §4 + docs/OPERATIONS.md §14（迁移记录）
- "想换嵌入模型" → config.toml [embedding] + PROJECT_SPEC.md §3.2
- "想改清洗规则" → PROJECT_SPEC.md §10 + src/cleaners/chat_cleaner.py
- "CJK 搜索不生效" → src/core/tokenizer.py + src/core/search.py（CJK 检测 + 权重 + content_tokenized）
- "MariaDB 中文搜不到" → docs/OPERATIONS.md §12 + 中文MariaDB全文搜索工程说明.md
- "想加新桶" → PROJECT_SPEC.md §4.4 + scripts/init_db.sql (INSERT bucket_config)
- "tag 语义 / OMNIA 是什么 / 为什么 tag 不能叫 select / 为什么目录名带逗号会报错" → src/core/tags.py（唯一真相源）+ PROJECT_SPEC.md §4.6
- "库里有没有讲 X 的书 / 先给我一批相关书目" → `mhi find-book` / MCP `find_books`（src/core/search.py::find_books）
- "为什么 find_books 不告诉我『库里没有』 / 存在性阈值怎么定" → docs/真实检索查询分布案例.md §五 + src/core/search.py 的 find_books docstring
- "改检索后怎么知道没弄坏 / 召回有没有退化" → scripts/diag_query_corpus.py（真实查询台账，基线：17/191 空手且全是 fulltext_only）
- "哪些书漏打 tag / tag 体检 / 为什么 tag 过滤搜不到某本书" → mhi tag --audit（src/core/tag_manager.py audit_tags）+ mhi-import-book 收尾提示
- "想调试 MCP Server" → tools/mcp_replay.py --replay tests/replay/smoke.replay
- "想写新重放" → tests/replay/e2e.replay（带 @save 变量传递的参考）
- "mhi-import-book 怎么用" → src/cli/mhi_import_book.py + docs/OPERATIONS.md §15.1
- "MCP 工具有哪些" → src/mcp_server.py（7 个 @mcp.tool，只读检索+发现）
- "测试怎么跑" → python -m pytest tests/（⚠️ 用装了依赖的解释器；系统 python3 缺 mariadb/regex 会直接 collect error）+ python tools/mcp_replay.py --replay tests/replay/smoke.replay
- \"MinerU 脚注丢失/图片引用\" → src/core/mineru_postprocess.py + docs/CLI_USAGE.md §10
- "想改切块参数（大小/重叠/每书限流）" → config.toml [chunking] + src/core/chunker.py
- "书搜不到具体内容 / 只返回整本书" → src/core/search.py _book_search（chunk 路径）
- "导入中断了怎么办" → 重跑同一条导入命令（状态机幂等：done+chunks 相符才跳过）
- "书桶 chunk 表结构" → scripts/init_db.sql + scripts/migrate_001_chunk.sql
- "删书/清目录/按 guid 删书" → src/cli/mhi_remove_book.py（--dry-run 预览，--guid 多值，chunks 级联）
- "删书的 SQL 长什么样" → src/cli/mhi_remove_book.py _build_delete_statements（测试只断言 SQL 文本）
- "进度显示/并行导入/Ctrl+C" → docs/OPERATIONS.md §15.1 + src/importers/book_importer.py
- "少打字/命令包装/在 Windows 直接调 CLI" → mhi.cmd / mhi.sh + src/cli/mhi_dispatch.py（子命令表只此一份）
- "mhi.cmd 为什么不用 chcp / 为什么纯 ASCII / 编码谁负责" → docs/OPERATIONS.md §17（三层责任链 + PEP 528/529/540 + 实测数据）+ mhi.cmd 头部注释 + tests 护栏
- "中文输出乱码/崩溃 / 控制台代码页 / MSVCRT 转码 / isatty" → docs/OPERATIONS.md §17
- "为什么不用 PYTHONIOENCODING" → docs/OPERATIONS.md §17.2/§17.3（命令行开关 vs 环境变量；全局 PYTHONIOENCODING 优先级更高）

15. docs/CLI_USAGE.md — CLI 快速参考（所有命令参数速查 + 密码配置优先级说明 + 搜索行为对照表 + MinerU 后处理 §10）
16. tools/publish.py（**内部工具，不进公开仓**）— 发布导出流水线（白名单 → `git archive HEAD` → 解包真复制 → 脱敏替换 → 下载 LICENSE（失败即失败退出）→ 硬链接自检 → 必备文件检查 → 敏感门禁 → 可选 git init/commit/remote）
    · 设计要点：只导出**已提交**内容（未跟踪文件天然排除）；默认导出目录取 `MHI_PUBLISH_DIR`
      （或仓库同级 memoria-del-hielo-publish）；脱敏只作用于导出目录，主工作区永远保留真实路径
    · 入口：`python tools/publish.py`（预演，什么都不写）/ `--apply`（真写）/ `--clean` / `--init-git` / `--remote <URL>`
    关键词: 发布, 开源, 脱敏, 白名单, git archive, 硬链接自检, publish
17. tools/sanitize_scan.py（**内部工具，不进公开仓**）— 敏感信息门禁（发布前必跑，退出码非 0 即失败）
    · ⚠️ 这个文件里存着**"哪些东西不许公开"的名单本身**（真实用户名 / 个人盘路径 / 语料目录名 /
      创作专名），而 `SELF_EXEMPT_SUFFIX` 让它豁免于自己的门禁——**它永远是免检的**。
      所以绝不能让白名单再写成整目录 `tools/`：漏出去那一次，脱敏规则把它的正则改成了占位符，
      公开仓里那份既暴露名单结构、又完全跑不起来。
    · 配套：`.publish-paths`（白名单：哪些文件进公开仓）/ `.publish-redactions`（脱敏规则<TAB>替换<TAB>说明）
      —— 两者同样**不进公开仓**；改脱敏规则时记得**同步门禁**，
      否则会出现"脱敏了但门禁还红"（门禁按短词查，规则只替换了长词——踩过这个坑）
    关键词: 门禁, 敏感信息, 泄露检查, sanitize, 发布前置检查
18. server_bge.py（仓库根）— 常驻嵌入 & 重排代理，双端口：9005 本地优先 / 9006 远端优先
    · 五模型 CPU/GPU 懒加载；远端走硅基流动（API Key 取环境变量 `SILICONFLOW_API_KEY`，配额集中在 `REMOTE_QUOTAS` 一处改）
    · streaming ndjson；与 src/core/embedding.py 的调用约定对齐
    · 想换嵌入后端 / 想调配额 / 服务起不来 → 就改这一个文件（端口与配额都是文件头常量）
    关键词: 嵌入服务, 重排, server_bge, 9005, 9006, 硅基流动, 懒加载, ndjson
19. server_paddle.py（仓库根）— PaddleOCR HTTP 服务（9008）：`GET /health`、`POST /ocr`（multipart）
    · 语言降级链 ch→en→la→el→ru，按置信度阈值选胜出语言（`PADDLE_LANG_CONFIDENCE` 可调）；
      协议对齐 src/core/image_pipeline.py::_ocr_image
    · ⚠️ `enable_mkldnn=False` 是 paddle 3.3.x oneDNN PIR 回归 bug 的官方实测解法——别"顺手优化"打开
    关键词: OCR, PaddleOCR, 9008, 语言降级链, oneDNN, 常驻服务
