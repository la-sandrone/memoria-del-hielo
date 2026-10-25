# 为什么不要试图一个月搞懂Unicode以及我们给MinerU擦了多少屁股

> **状态：素材台账，未定稿。** 公开发布前必须做两件事：
> 1. 跑 `tools/sanitize_scan.py` 过一遍（本文含个人盘路径与语料目录名）
> 2. 复核每条「归属」一栏 —— 见下面的公平性声明
>
> **公平性声明（重要）**：本文的标题是玩笑，但**内容必须公允**。下面 30+ 条里
> **相当一部分是我们自己的 bug**，不是上游的。读者一验就能发现，硬甩锅会反噬。
> 所以每条都标了归属：`Unicode标准` / `MinerU` / `DocVortex` / `源PDF` /
> `MariaDB` / `我们自己`。
>
> 所有判据都写成**可重跑的命令或可复算的数字**。凡是我们"读了觉得是"的，都不算证据。

> **归属统计（读者可自行核对，不劳听信）**
>
> | 归属 | 节 | 条数 |
> |---|---|---|
> | Unicode 标准本身的边界（不是 bug，是必须知道的约束） | §2 | 12 |
> | MinerU 4.0.7 | §3 | 10 |
> | DocVortex | §4 | 3 |
> | 源 PDF / 上游转换链路 | §6 | 4 |
> | **我们自己** | **§7** | **11** |
> | MariaDB / jieba / BGE-M3 的实测边界 | §8 | 9 |
> | 「裁判」自身的失败（**也全是我们**） | §1 | 6 次实例 |
>
> 上游侧合计 **17** 条；**我们自己 11 条 ＋ 6 次裁判自伤**。
> 标题里的「擦屁股」只覆盖上游那 17 条 —— 剩下那一半是自伤，也写在里面，别漏看 §7。

---

## 零、一句话

> **不要试图在一个月内弄懂Unicode，当前版本Unicode看得懂不代表也能看得懂以后的版本。**
> —— 引自一位搞 TeX 的博主（下称「七篇」，链接见文末）

它对我们不是座右铭，是**工程条款**。这一条下面有 30+ 条实例，其中一半的教训是
「我以为我懂了」。

---

## 一、先立规矩：判据必须是可重跑的

这一天里，我们的「裁判」自己出错 **6 次**，其中 **5 次静默**（正常退出、输出格式合法、
结论看起来合理）。这条比任何 Unicode 知识都值钱。

### 1.1 「裁判」四条

| # | 条 | 由来 |
|---|---|---|
| 1 | **断言必须限定作用域** | 全局字符串断言撞上同名无关代码 |
| 2 | **判据必须配正对照** | 判据本身没验过，就是"裁判收错答题卡" |
| 3 | **别丢测试的 stderr** | `set -u` 自杀 + stderr 进 `/dev/null` → 失败伪装成通过 |
| 4 | **解析外部数据文件时，「解析成功」不是证据；「解析了多少、解析成什么形状」才是** | 本次新增 |

**第 2 条的加强版（今天新学到的）**：**正对照本身必须覆盖判据声明的每条分支**。
实例：给「物化阶段不得自增 ref_count」写断言，只在**空库**上调了一次 —— 于是只走到
`INSERT` 分支，而缺陷恰恰在「命中已存在」分支里。把 `ref_count++` 注入命中分支，
**测试照样全绿**。判据看着在验不变式，其实没验。改成「两次调用，各走一条分支」后，
注入即红，报错还直接给出证据：`['SELECT', 'INSERT', 'SELECT', 'UPDATE images']`。
⟹ 写正对照时先问：**这条判据声称覆盖哪几条路径？我注入的那条落在哪一条上？**

第 4 条的操作形态：**形状断言 + 覆盖率报告 + 用真实文件里的字面量做单测**。
命名上它专治三种静默病：**静默截断 / 静默跳过 / 静默空值**。

### 1.2 实例：同一个探针，一天内栽四次

测「UCA 权重 14.0 → 16.0 有没有变」，四个版本各有 bug，**四次一次都没报错**：

| 版本 | bug | 产出的假结论 |
|---|---|---|
| v1 | 正则 `\[([^\]]*)\]` 只抓**第一个** `[..]` 组；而部首行是两段 `2F49 ; [.FB40.0020.0004][.E708.0000.0000]` | 「康熙部首 214 条权重全部相同」——拿所有部首**共有的首段**互比，**恒真** |
| v2 首版 | 正则要求首个 `]` 后紧跟 `#` | 「DUCET 没有部首条目」（`grep` 一眼推翻） |
| v2 次版 | `split(".")[0]` 遇前导 `.` 切出空串 | 主权重全是 `0000` |
| 内联脚本 | `s[:1] in ("*.",)` 是**元组包含**判断，`'.' != "*.",` | 同上，全空 |

**第五、六次出现在写这份文档的时候**（同一处、同一个正则错两遍）：解析 DocVortex 那张
20/24 条的折叠表，值模式只吃双引号串，而四条双引号项写的是 `'"'`（单引号包的双引号）
→ **两次都静默只拿到 20 条**，并据此写出 "20 条里 13 条非 NFKC" 的错数字。
改用 `ast.literal_eval` 才拿到真数 **24 条 / 17 条非 NFKC**。

> **「别拿正则去解析 Python 能直接解析的东西」** —— 这一天的第六次，仍然是同一种病：
> 解析器静默少读了东西，而输出格式完全合法。

**救回来的手段没有一次是"更努力地理解 Unicode"**：

```bash
grep -nE '^(2F49|2E80)' allkeys-14.0.0.txt      # 先看原始行长什么样
```

第三、四次的 bug 各需一个字面量单测就能当场抓住：`prim_of('.FB40.0020.0004') == 'FB40'`。

### 1.3 实例：夹具自证

给 v4「zip 解包目录」写的识别分支找的是 `<stem>.json`，而真 zip 的**成员名是固定名**
`middle_json.json`。测试却全绿 —— 因为**夹具是我自己改名过的**：我把
`middle_json.json` 拷成了 `Book.json`。

```
$ python -c "import zipfile;print(zipfile.ZipFile('Book.zip').namelist())"
['markdown.md', 'middle_json.json', 'structured_content.json']
```

补的自检是一条**维度**：

```python
assert {f.name for f in unpacked_dir.iterdir()} == {n for n in zip.namelist()
                                                   if not n.startswith("images/")}
```

判据原来的「夹具自检」只验了 JSON **内容**是切片，没验**文件名**。少一维就漏一维。
（正对照：把名字改回去 → 3 条红，其中两条是本轮新增。）

---

## 二、Unicode 标准本身的坑（我们撞到的）

| # | 事实 | 判据 |
|---|---|---|
| 2.1 | **Strong Normalization Stability（4.1+）是唯一能锁死的东西**：只含已分配字符的字符串，其 NFC/NFD/NFKC/NFKD 结果在所有后续版本中**完全相同** | 官方 stability policy 原文 |
| 2.2 | 但它的**边界**是"已分配字符"。新分配的字符不在保护内 —— 官方 Note 明说 | 同上 |
| 2.3 | **General_Category 不是不朽的**。Property Value Stability 只给 `Cc`(1.1.5+)、`Co`/`Cs`(2.0.0+) 不朽；**`Z*` 与 `Cf` 没有任何条款** | 官方 Property Value Stability 表 |
| 2.4 | 而 GC 变更是**实证存在**的：Unicode 6.3 一次性改了 4 个**既有**字符 —— `180E Zs→Cf`、`1A1B Mc→Mn`、`2308–230B Sm→Ps/Pe` | UnicodeData.txt 前后对照 |
| 2.5 | **同一件事有两套等价口径，覆盖面与稳定性承诺都不同**：NFKC 单例分解（康熙部首 214/224、CJK 部首补充 2/128）vs UCA 隐式权重映射（更广，含全部简体部首） | 见 2.6/2.7 |
| 2.6 | UCA 数据目录**止于 16.0.0，没有 17.0.0**（collation 数据已迁到 CLDR） | `unicode.org/Public/UCA/` 目录列表 |
| 2.7 | DUCET 14.0→16.0：共同码点 **91.08%** 权重序列变化（整表重编号），**全局排序确有重排**（首个分歧第 358 位：U+2018 vs U+05F3） | 下两版 allkeys.txt 逐码点比 |
| 2.8 | 但**康熙部首 214 条完整权重序列 0 变化**，块内相对次序也一致 | 同上 |
| 2.9 | `U+4E00–U+9FFF` 在 DUCET 里 **0 个条目** → 汉字全走 **UCA 隐式权重**（由码点算术推出）→ 压根没有"版本漂移面" | `allkeys.txt` 计数 |
| 2.10 | 康熙部首在 DUCET 里是**两段权重**，两段 primary 拼接 = 目标汉字的 Han-Core 隐式权重（**214/214 无例外**；目标 ≥ U+8000 时首段是 `FB41` 不是 `FB40`） | 逐条复算 |
| 2.11 | ICU 与 python `unicodedata` 的**数据版本不同**（ICU 78.3 = Unicode 17.0；unicodedata = 15/16）→ **不能拿 unicodedata 兜底** | `icu.UNICODE_VERSION` / `ud.unidata_version` |
| 2.12 | NFKD/NFKC 与 NFC/NFD 是**两个层**：`⽉`(U+2F49) 的 `isNormalized('NFC') == True` —— **常规 NFC 检查发现不了这类污染** | ICU `Normalizer2.isNormalized` |

**2.10 的推论**：`WEIGHT_STRING('⽉' COLLATE utf8mb4_uca1400_ai_ci)` 与 `'月'` 同为
`FB40E708`，因为在只比 primary 的 `_ai_ci` 下两边 primary 序列相同。这**完整解释**了
`'⽉' = '月'`。

---

## 三、MinerU 4.0.7 的坑

| # | 事实 | 判据 |
|---|---|---|
| 3.1 | **`parse_mode` 不由 tier 决定**。`--tier flash` 也能出 `parse_mode: "ocr"`；`--tier standard` 也能出 `parse_mode: "txt"` | `extensions.mineru` |
| 3.2 | 三档（flash/basic/standard）在**同一本 born-digital PDF** 上可以是**同一读数**：三档 `parse_mode` 全 `txt`，flash 与 basic 的**字符集完全相同** | 见第五节实测 |
| 3.3 | `-f middle_json` 对 PDF **剔除 `image_base64`** → 单出不完整，必须配 zip | 官方文档 + 实测（`image_body.content` 为空串） |
| 3.4 | **扁平 `<stem>.md` 丢掉全部元数据**：tier / parse_mode / 页号 / 脚注结构 / 图片类型，**一个都不剩** | 实测两份扁平产出 |
| 3.5 | **v4 内部有四种 bbox 口径**：v3 `_middle.json` 页内 pt 绝对（0…813）、v3 `_content_list{,_v2}.json` **0–1000 整数**、v3 `_model.json` 0–1、v4 三种 json **0–1 归一化** | 值域实测 |
| 3.6 | v4 `content` **可递归嵌套**，`span['content']` 可以还是列表 | 见 4.1 |
| 3.7 | zip 成员名是**固定名**（`markdown.md` / `middle_json.json` / `structured_content.json`），**不带 stem** | `namelist()` |
| 3.8 | zip 图片名 `images/page_{page_idx}_{parent_type}_{parent_index}.{ext}` | 官方文档 + 实测 |
| 3.9 | `-o` 用「末段有没有后缀」判文件还是目录 → 不先 `mkdir -p` 会把目录写成文件 | 前情（v6.3 反证） |
| 3.10 | `metadata.document` 里有 `creator_application` / `producer_application` / `page_count`，`is_full_document` 可当"这产出完不完整"的哨兵 | 实测 |

---

## 四、DocVortex 的坑（MinerU 4 的依赖）

### 4.1 静默改字：一张自己发明的标点折叠表

```python
# docvortex/analyzers/native/pdf/spatial_text.py:24（ast 解析，共 24 条）
_PUNCTUATION_TRANSLATION = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "′": "'",          # 单引号类 4
    "“": '"', "”": '"', "„": '"', "″": '"',          # 双引号类 4
    "‐": "-", "‑": "-", "‒": "-", "–": "-",          # 横线类 7
    "—": "-", "―": "-", "−": "-",
    "\u00a0": " ", "\u0002": "-",                     # 其他 2
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl",                 # 连字 7
    "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "ft", "ﬆ": "st",
})
```

**逐条问 ICU 的结果：24 条里只有 7 条是 NFKC 本来就给的**（NBSP + `ﬀﬁﬂﬃﬄﬆ`），
**17 条是这张表自己发明的**（全部引号、全部横线、`U+0002`，以及那条错的 `ﬅ`）。

⚠️ `U+2033 ″ DOUBLE PRIME` 折成 `'"'`，而 NFKC 给的是 `′′` —— 这一折**长度也变了**（1 字 → 2 字）。

> **这个数字我们改过两次。** 第一版用正则 `"([^"]*)"` 取值，遇到 `'"'`（单引号包的
> 双引号）整条不匹配，**静默只拿到 20 条**，于是写出"20 条里 13 条非 NFKC"。
> "修正"时换了值模式仍只吃双引号串，**又只拿到 20 条**。最后改成
> `ast.literal_eval` 才拿到真数 24 条 / 17 条非 NFKC。
> **教训：别拿正则去解析 Python 能直接解析的东西。**

⟹ 它**比 NFKC 更具破坏性**：NFKC 会放过 `“”–—`（它们本身就是 NFKC 规范形式），
这张表不放。

**适用面比表格广**（函数名叫 `_normalize_table_text`，但调用点不止表格）：

```
spatial_text.py:131  _normalize_table_text("".join(segment_parts))   ← 整行/整段
spatial_text.py:169  _normalize_table_text(raw_char)                 ← **逐字符**
spatial_text.py:223  _normalize_table_text(rec_result[0])            ← OCR 结果
_table_recovery/text.py:71,116                                        ← 表格
```

### 4.2 真错：`ﬅ` → `ft`

```
U+FB05 LATIN SMALL LIGATURE LONG S T 「ﬅ」   表里折成 'ft'   ← ❌
                             Unicode NFKC 标准分解是 'st'
```

「ﬅ」是 **long s + t** 的连字，历史上就是 `st`。折成 `ft` 是**静默改字**。
而本仓测试的语料里正好有一本**讨论连字的 Unicode 标准书**。

### 4.3 开着的问题（未定位，不编）

同一份文档、同样走 native-PDF 路径：**只有 `standard` 档吃这张表，`basic`/`flash` 不吃**。
两个次要观察：basic 与 standard 的**块结构完全相同**（50 块，逐类型一致），
唯一差别就是这几个引号。**哪个开关让 standard 吃它，我们还没定位。**

---

## 五、实测：一本书上三方对读（Unicode 14.0 标准书，前 20 页）

| 读数 | 字符数 | 不同码点 | parse_mode |
|---|---|---|---|
| flash | 84,379 | 83 | `txt` |
| basic | 23,075 | 83 | `txt` |
| standard | 24,948 | 82 | `txt` |
| **pdftotext** | 84,568 | 84 | — |

```
flash vs basic      ✅ 字符集完全相同
flash vs pdftotext  仅flash-0 / 仅pdftotext-1     ← 多的那 1 个是 U+000C 换页符
```

**结论（⚠️ 已被 §5.3 限定，别单独引用）**：这书的**拉丁部分**文本层是好的
（源 PDF = `FrameMaker 2019.0.7` + `Acrobat Distiller 21.0`），三档字符集互为子集，
**"三档一致"不构成交叉验证**（它们读的是同一个来源）。
字符数差异（84k vs 23k）来自**目录的点引线**：flash/standard 保留 `Preface . . . xxi`，
basic 折叠成 `Preface xxi`。

> **我们说错过一次（而且是"倒退"）**：由"前 20 页 + 787–796 页都对得上"推出了
> "这本属于文本层可信那一类"。**错了** —— 判据被限定在拉丁文范围，而这本在
> **CJK 页上文本层会静默毁字**。更要紧的是：**这条教训在六篇随笔里早就给过**
> （第六篇：「不加 ToUnicode 很大可能性是不对的，**但是加了就有一定可能性还是不对的**」），
> 我们却在同一批材料读过之后又倒回去信了文本层。详见 §5.3。
> 教训两条：**"文本层可信"必须按字体判、不能按文档判**；**读过的材料要留出处，否则会重复踩**。

⚠️ **本窗口（前 20 页）压不到"怪异字符"**：`GREEK 0`，只有 `LATIN 54` + `DIGIT 10`。
真正的罕见文种在**第 19 章文字系统描述（约 750–850 页）** —— 锚点从目录里读到：
`19.3 Tifinagh 787`、`19.4 N'Ko 790`、`19.5 Vai 795`。

### 5.2 第二轮：`--ocr-mode` 才是"有没有 OCR"的开关（物理页 787–796）

三条臂：`standard×txt`、`standard×ocr`、`flash×txt`，加 pdftotext 共四方。

| 读数 | 字符数 | 不同码点 | parse_mode |
|---|---|---|---|
| flash×txt | 24,486 | 83 | `txt` |
| standard×txt | 24,487 | 83 | `txt` |
| **standard×ocr** | 24,463 | **85** | **`ocr`** ✅ |
| pdftotext | 24,751 | 83 | — |

```
flash-txt    vs standard-txt   ✅ 完全相同
flash-txt    vs pdftotext      ✅ 完全相同
standard-txt vs pdftotext      ✅ 完全相同
standard-ocr vs standard-txt   仅ocr-3 / 仅txt-1
```

**三个文本层读数（flash / standard / pdftotext）的字符集完全一致，83 个码点；
唯一分歧源是 `standard-ocr`。** 块结构却不同：flash 114 块
（text 69 / paragraph_title 12 / doc_title 1 / page_number 10 /
**header 20**），standard 110 块（text 65 / paragraph_title 14 / page_number 9 /
header 20）—— **字符集相同、分块不同** ⟹ 说"三档一致"必须先讲清比的是哪个维度。

> **排除提取产物后的数字**（本表已是排除后的）：正文是我用 `"\n".join(块)` 拼的，
> pdftotext 用 `\x0c` 分页。不排除的话每个读数白多一个 `U+000A`、pdftotext 多一个
> `U+000C`。第一版就是把它当成了"四方共有的**无名**码点"、白查一趟 ——
> 结论：**字符集比对必须先剔掉提取方法自身的产物**，否则是在比提取器而不是比文档。

**结论一：`--ocr-mode ocr` 确实强制走了视觉路径**（`parse_mode: "ocr"`），
而 `--tier` 不是那个开关。这一轮才第一次拿到真正的视觉读数。

**结论二：OCR 与文本层在字符集上只差 4 个码点，但那 4 个说明了问题** ——

| 码点 | 只在 | 上下文 |
|---|---|---|
| `"` `'` | standard-ocr | `Standard Annex #11, "East Asian Width."` / `People's Republic` |
| `Z` | standard-ocr | `- U+200B ZERO WIDTH SPACE is recom` |
| `•` | flash-txt / standard-txt / pdftotext | `• U+200B zero width space` |

另：本窗口三档都有 `table: 2`，所以"折叠只作用于表格"这个假设**在本窗口无法检验**
（这些表格里大概不含那 24 个字符之一）—— 既不能说支持，也不能说推翻。
本批实测 `unicodedata 15.0.0` vs `ICU 17.0` **无版本错配码点**（正文里没有超出
本机数据版本的字符）；那 3 个 CJK 是 `三`/`書`/`朝`，三档一致（朝鲜语章节的 Hanja 例）。

即：**视觉读数把弯引号写成 ASCII 直引号、把项目符号 `•` 读成 `-`、把小写读成大写；
文本层读数则反而是小写。** 两方都不是真值 —— 这正是本文第一节那条纪律的实例。

**结论三（关键，纠正 5.1 的适用范围）**：**这一轮里 standard 没有折任何东西**
（standard×txt 字符集与 flash×txt **完全相同**，且三方都保留了 `•`）。
所以 5.1 里观察到的"standard 折引号"**只发生在那 20 页前言/目录上**，
没有在正文上复现。结合 §4.1 的调用点（`_table_recovery/text.py` 调
`_normalize_table_text`），最一致的解释是：**该折叠作用于表格/表格状区域**，
而 standard 把目录判成了表格、flash/basic 没有。
⟹ **§4.3 那个"哪个开关"的问题因此收窄到"哪一档把目录判成表格"**，仍未定位。

**结论四：物理页 ≠ 印刷页，探针窗口要按偏移换算。**
从 pdftotext 的分页符后读到：`East Asia⏎762⏎18.6` ——
**物理页 787 显示的是印刷页 762**，⟹ 偏移 **+25**。
目录里 `19.3 Tifinagh 787` 是**印刷页**，对应物理页约 **812**。
（本窗口实测 `GREEK 0` / `CJK 3` / `LATIN 52`，仍压不到 Tifinagh/N'Ko/Vai。）

### 5.3 磨刀石判分页：文本层把 CJK 字形读成 ASCII 标点（物理 763 / 印刷 737）

这一页（§18.1 Han，图 18-4）是全书最好的一道题，因为**它自带答案**。

**现象**：截图里是 `澤 (marsh)` 和 `機 (machine)`、`儿₁`、`机₂`，以及图 18-4 的
**六个几乎一模一样的 `劍`**。而 pdf 文本层给出的是：

```
Distinctions are made between entirely unrelated characters such as > (marsh)
and : (machine) as well as extensions or borrowings beyond the original semantic
cluster such as ;1 (a phonetic borrowing used as a simplified form of :) and ;2 …
```

**`澤`→`>`  `機`→`:`  `儿`→`;1`  `机`→`;2`，图 18-4 那六个 `劍` 直接从文本层消失**
（该页 CJK 码点统计 = **0 个**）。

`od -c` 确认 `>` 就是纯 ASCII `0x3E` —— **不是乱码，是"看起来像正常标点"的垃圾**。

#### ⚠️ 先把出处交代清楚：这一条**不是我们的新发现**，文章里早说过了

第六篇（<https://mp.weixin.qq.com/s/l9QJBVTmuI_NNZ9X87NHMA>）对 ToUnicode 的原话：

> **「说回来，MuPDF 生成的 ToUnicode 代码。很烂。把整个字体的 cmap 完整过一遍然后写进去了。」**
>
> **「这玩意无解的，我特么也看不到 Edge 处理 PDF 的代码……」**
>
> **「不加 ToUnicode 你复制出来的东西很大可能性是不对的。但是加了就有一定可能性还是不对的。」**
>
> **「现有的 ToUnicode 是不能够给一个 glyph 映射多个 code 的。所以这事很多时候无解。」**
>
> **「如果这个字体存在 glyph 不映射 code 的情况，或者 glyph list 和 code list 不是同一顺序的，
> 那么 ToUnicode 做了还不如不做。」**
>
> **「处理 ToUnicode，每个软件都不一样，以及每个字体都不一样。」**

同一篇还给了这类问题的两条出路：给 glyph list 加 `actual text` 属性，或**生成 Type 3 字体**；
并指出印／东南亚文字靠 `Encoding + ToUnicode` 是行不通的。

⟹ **下面这一节的性质是"文章预告过的情形，在我们语料上首次见到实例"，
而不是"我们发现了一个新问题"。** 我们在 §5 曾得出"这本属于文本层可信那一类"，
**那不只是判据被限定在拉丁文范围，是往一个已经确立的结论倒退了。**

两条已经写在我们自己记录里、但当时没把出处挂上去的：

| 文章 | 内容 | 我们在哪儿踩到 |
|---|---|---|
| 第六篇 | ToUnicode **不能给一个 glyph 映射多个 code** | 康熙部首 `⻣ U+2EE3 / ⾻ U+2FBB / 骨 U+9AA8` —— 同 glyph 三码点 |
| 第六篇 | 印／东南亚走不通；解法 = `actual text` 或 **Type 3** | 《月背征途》的 `NotoSansCJKsc` 正是 **Type 3** |

**根因**（`pdffonts` 单页级，一句话定案）：

```
AFEICP+Characters          TrueType  WinAnsi  emb yes sub yes uni **no**   ← CJK 装在这个子集字体里
AEPPDK+Minion3-Regular     Type 1C   Custom   emb yes sub yes uni yes     ← 拉丁字体，正常
```

**CJK 字形装在一个无 ToUnicode 的 TrueType 子集字体里**，编码 WinAnsi。任何信任
WinAnsi 字节值的提取器都会把字形码位当 ASCII 标点输出。同页拉丁字体 `uni=yes` 所以正文完好。

**真值（来自物理 764 / 印刷 738 的 Table 18-4，那部分是 Minion，读得完美）**：

```
U+5263  U+528D  U+5271  U+5294  U+5292  U+91FC
（对应 JIS J0-3775 / J0-5178 / J0-517B / J0-5179 / J0-517A / J0-6E5F）
```

⟹ 于是这一页可以**定量判分**：把 MinerU 的视觉读数与这六个码点比对。
判分脚本 `_scratch/check_sword_variants.py`；命令见下：

```bash
mkdir -p "$P/standard-ocr" "$P/standard-txt"
for M in ocr txt; do
  mineru-kit parse "$SRC" --pages 763-764 -o "$P/standard-$M" \
      --tier standard --ocr-mode "$M" --format middle_json
done
cd <project-root>
~/ai-services/bin/python _scratch/check_sword_variants.py "$P"
```

**这一页把"OCR 有没有必要"这个问题的答案反过来了**：在 5.1/5.2 那两处，
OCR 是"可选的交叉验证"；**在这一页，文本层已经把字符弄丢了，视觉路径是唯一来源。**

**给知识库的机械判据（可直接实现）**：导入前对 PDF 跑一遍
`pdffonts`，**任一嵌入字体 `uni = no` 即标记该书的文本层对该字体的字形范围不可信**。
这条是纯机械的、可自动化的，比"抽几页人工看看"可靠得多。
（`scripts/check_pdf_tools.py` 已有 `pdffonts` 调用，可挂在这里。）

### 5.4 「哪个读数可信」本身也是开放问题：Adobe 也不是权威

**出处**：<https://mp.weixin.qq.com/s/6ZR90gnFce7knZcYJ805Fg>（标题大意：Acrobat 不是 last resort）

它给了一条**方法论**，而这正是我们前几节一直缺的东西：

> **「我们无法知道 Adobe 是不是'掌握 PDF 细节的一切'。」**
> —— 成规模企业人员流动、知识流失，"发明者自然全懂"是**下意识的假设**，不是判据。

> **「通过这种例子，往往能够验证出 Adobe 的阅读器对于 PDF 处理的一些细节之处……
> 但又可能是 Adobe 对 PDF 理解在某个特定范围，它错了。而这个特定范围，
> 它既不在弹窗上告诉你，也不在 PDF Reference 中告诉你。」**

> **「大多数可以遵循最大公约数原则：如果几个阅读器的行为都差不多，
> 那么就可以认为某一个观察点大致上是对的。但这事并不绝对。」**

两条作者自己测多了之后的信念：

1. **PDF Reference 自发布以来，Adobe 阅读器的行为就没有完全写在 Reference 上**
2. **不同时期的 Acrobat 的行为，有一些甚至是相反的**

#### 对我们三条直接后果

**① 我们的"四方对读"不合格——四个读数不独立。**
最大公约数原则的前提是**独立实现**。flash / basic / standard 三档读的是**同一个文本层**
（§5.2 实测字符集逐码点相同），**它们不是四个证据，是一个证据的四份抄本。**
§5.1 那句"三档一致不构成交叉验证"因此不是我们的发明，而是这条原则的一个实例。

**② 我们缺的正是它说的那件事：独立第二提取器。**
落在本场景就是"换个实现再抽一次"：

```
poppler (pdftotext)  ─┐
MuPDF   (mutool)     ─┼→ 两边不一致的地方 = 真正可疑的地方
PyMuPDF (pip 可装)   ─┘
```

（实测 `which mutool` → **未安装**。装它是可实现的下一步。）

**③ §5.3 要再修一处措辞：「文本层坏了」≠「PDF 坏了」。**
该文说"唯一被保证的是 glyph → bitmap → 人眼"。而截图（用户在 Windows 查看器里打开的
同一页）显示 `澤 機` 与六个 `劍` **渲染全部正确**。
⟹ 坏掉的只是 **ToUnicode**——一张"为了让你能复制"而额外写进去的**派生表**，
不在渲染路径上。**PDF 没坏，坏的是它给外人看的那张对照表。**
这比"文本层坏了"精确得多，也正是"PDF 是某一种形式的电子照片"这条推论
在我们语料上的验证。

> **推论纪律（本节新增）**：拿到一个"文本层读数"时，先问三件事 ——
> **这个读数有几个独立实现？**（同一个库的多个档位算一个）
> **它的渲染路径与提取路径是否分离？**（分离时，渲染对而提取错是完全可能的）
> **我有没有构造最小例子？**（该文指出：大部分人没有这个能力，复杂例子要半个月；
> 而我们手上那页六个 `劍` 恰好就是一个自带真值的最小例子。）

#### 5.4.1 实测：最大公约数原则在这类错误上会给出**假阳性**（自我更正）

装了 `mutool`（1.28.5）与 PyMuPDF（1.28.2）之后，拿物理页 763 做**两个独立实现互证**：

```
poppler(pdftotext) 2354 字符 / MuPDF(mutool) 2374 字符
非空白字符集: poppler 68 / MuPDF 68 / 共有 68 / 差集 []
poppler  …such as > (marsh) and : (machine)…
MuPDF    …such as > (marsh) and : (machine)…      ← 完全一致
```

**两个独立实现给出同一个错读数。** 文章 A 的原则说"几个阅读器行为差不多，就可以认为
某一个观察点大致上是对的"——**在这里它是假阳性**（该文自己也写了"但这事并不绝对"）。

原因：poppler 与 MuPDF **读的是同一张坏表**（`AFEICP+Characters` 的 `WinAnsiEncoding`
＋无 ToUnicode），各自从 Encoding 反推出**同样的**错 Unicode。
**它们不是两个独立判断，是同一个错误来源的两次执行。**

#### 5.4.2 因此更正 §5.4 的推荐：独立第二提取器**不够**

| 能抓 | 抓不到 |
|---|---|
| **实现差异**类：两个库合规性不同的地方、ToUnicode 缺失导致的乱码 | **数据损坏**类：两边读同一张坏表 → **一致地错** |

这页属于后者。⟹ **真正能判对的只有渲染路径**（glyph → bitmap → 人眼）。
这也是"PDF 是某一种形式的电子照片"这条推论在工程上的落点：
**提取路径可以坏，渲染路径几乎不会**。

> **判据修正（以此为准）**：
> · 「两个独立实现一致」**只能排除实现差异，不能证明文本正确**；
> · 要判"文本层是否可信"，必须让**渲染路径**参与（视觉读数 / OCR / 人眼）；
> · `pdffonts` 的 `uni = no` **只是线索，不是判据** —— 同一页上
>   `AFABCD+Minion-Regular` / `AFABLD+Minion-Regular` 也是 `uni=no`，
>   但它们承载的 `“sword”`、`—` 全部读对（WinAnsi 对真正的文本字体成立）。

#### 5.4.3 PyMuPDF 的 span 级字体归属：把推理变成可复现的判据

```
[Characters] '>'   [Characters] ':'   [Characters] ';'
[Characters] ':'   [Characters] ';'
[Minion3-Regular] '•'            ← U+2022 BULLET，正常
[Minion-Regular] '“sword”'       ← 弯引号，正常
```

那 5 个符号**全部出自 `Characters`** 字体，与 §5.3 由 `pdffonts` 推出的结论一致 ——
现在它是**可复现的归属**，不是推理。

⚠️ **一个必踩的坑**：PyMuPDF 的 span `font` 是 **`Characters`（无子集前缀）**，
而 `get_fonts()[3]` 的 `basefont` 是 **`AFEICP+Characters`（带前缀）**。
**不剥前缀直接比对 → 0 命中**，而这个 0 看起来像"没有 span 归属到可疑字体"。
首版就把它当成了结论。（剥法按 PDF 约定：`^[A-Z]{6}\+`。）

> **接口纪律（本节新增）**：`mutool draw` 的 `-p` 是 **password**、`pages` 是**位置参数**；
> 首版写成 `-p 763` 把页码当密码传，**页范围压根没生效**，输出的是全本 1048 页
> （全本 2,565,015 字符 vs 单页 2,468）。于是当时那两条"验证"恒真、纯自证。
> ⟹ **接口形状查文档（man/官方文档），行为在真数据上探测** —— 两件事别混。
> 文档依据：<https://manpages.debian.org/bookworm/mupdf-tools/mutool.1.en.html>、
> <https://pymupdf.readthedocs.io/en/latest/app1.html>

### 5.5 把它跑成整本的数：这本书 27.1% 的页面上有「伪装成拉丁字母的槽位号」

§5.3 只看了一页。用 `mutool extract`（抠出全部嵌入字体）＋ `fontTools`（读字体自己的表）
把**整本 1048 页**跑了一遍。

**判据**（不依赖参考字体、不依赖渲染、纯 CPU）：

| 判据 | 内容 |
|---|---|
| **B** | 某个 **ASCII 码位**映射到**全宽字形**（advance ≥ 0.90em）—— 拉丁字形不可能是全宽 |
| **A** | 字体**没有 cmap 表**（且非 CID-keyed）→ Unicode 从根上不可恢复 |
| **C** | ASCII 码位对应的**字形名**与其码位矛盾（如 0x3E → `uni6FA4`） |

**结果**：

```
受审 249 个嵌入字体 → 可疑 50 个
  用到可疑字体的页: 284 / 1048 = 27.1%
  可疑字体承载的字符: 4,016 / 2,406,202 = 0.17%
```

**50 个可疑字体的名字一眼就说明它们是什么**：

```
Han01 Han03 Unihan500          Hangul01
Arab Syriac-Sup-u14 Serto      Aramaic-Julie60 ManichaeanPart2
Deva RickU60-Deva SiddhamEx    MarNepGlyphs MarNepGlyphs
Khmerone Khmerfour             Baliglyphsone Nkoglyphstwo MendeFig
EgyptianControls1 PHAGS-PAASCII-NEW  UniRoozbehMlym LiangUni13
UniEricOne..Eight  Ufourone..twelve  Ufiveone
```

⟹ **这本书里每一处文字系统样例，都装在一个"以 ASCII 码位为字形槽"的自制字体里。**
文本层在这些地方给出的是**槽位号**——看着像正常拉丁字母和标点，实际不是内容。

**怎么读这两个数**：

- **0.17% 很低** ⟹ 对整书嵌入/检索召回的损害有限；
- **27.1% 很高** ⟹ 但四分之一以上的页面上存在"像正常文字、其实不是"的片段，
  **对精确引用与回查是污染**（LLM 会照抄这个槽位号）。

两者不矛盾：**污染是稀疏的，但分布是弥漫的。**

#### 三条方法论收获（都是在踩坑之后）

1. **判据 B 有效，判据 C 无效。** 这些字体的字形名被改写成 ASCII 名
   （`comma` / `dollar` / `ampersand`），所以"名字与码位矛盾"根本不会触发；
   真正露出马脚的是**宽度**。⟹ 选判据要看**改写者改不掉什么**。
2. **`mutool extract <pdf> <objnum>` 单对象调用静默不产出**（rc=0、无文件），
   且它的文件名编号 **≠** `pdffonts` 的对象号。⟹ 只能全量抽取**读字体自己的
   `name` 表**来认身份。（实测全量抽取 249 个字体仅 2.2 秒、2.7MB。）
3. **同一个 bug 我犯了两次**：比较字体名时**只剥了一边**的子集前缀
   （`XXXXXX+Name`）——一次是 PyMuPDF 的 `font`（不带）vs `pdffonts`（带），
   一次是字体 `name` 表（带）vs `basefont`（我剥了）。两次都产出**假结论**
   （"0 命中"、"0 页受影响"）。⟹ **凡比较标识符，两侧都归一化，并且
   用"应该 >0 的正对照"兜底**（已知可疑字体必须能被查到）。

#### 还没堵的洞 —— 以及一次**未遂**的修补（结论见本节末：已按政策回退）

首轮 `TTFont` 打不开裸 CFF（`Not a TrueType or OpenType font (bad sfntVersion)`），
**127 个 `.cff`（Type 1C / CID Type 0C）未判**。补 `fontTools.cffLib` 分支后：

| 判据 | 补之前 | 补之后 |
|---|---|---|
| OK | 24 | 129 |
| B_WIDTH_FULLWIDTH | 50 | 56 |
| A_NO_CMAP | 47 | 47 |
| G_CID_KEYED_SKIP | — | 14（显式跳过） |
| E_PARSE_FAIL | **127** | **2** |

覆盖率 49% → **74%**。**但这次修补不算成功**，两条自检没过：

1. **宽度语义未验证。** 脚本内置的自检要求 `space ≈ 0.25~0.33em`、`M ≈ 0.8~0.9em`；
   实测 `space = M = 0.269`（`upem=1000 defaultWidthX=269 nominalWidthX=476`）
   —— **`M` 不可能是 0.269em**。⟹ CFF 的
   「charstring 宽度操作数是相对 `nominalWidthX` 的增量，缺省取 `defaultWidthX`」
   这套组合我用得不对。**那 6 个新命中（宽度 1.0 / 1.024 / 1.04 / 1.101）
   是未验证的数，不作为发现。**
2. **字体身份未映射。** CFF 没有 `name` 表，而 `font_identity()` 只在 TTFont 分支里调，
   所以那 6 个只有文件名（`font-10316.cff`）⟹ 无法映射回 PDF 的 basefont
   ⟹ **影响面重算时它们被静默排除**（所以仍是 50 个 / 27.1% / 0.17%）。

> **纪律（本节新增）**：**"覆盖率上升"不等于"结论变好"。**
> 覆盖率从 49% 提到 74% 看起来是进步，但新增覆盖里唯一的新命中**连自检都没过**。
> 判据的扩张必须带**同一条自检**；自检红了，新命中一律不作数。

### 5.7 真书 E2E：三次导入，v1 与 v2 在真数据上是 1 : 6

把兼容层接进导入链后，用**三本真书**走了一遍（每次都只取 2 页做小样）。

| 书 | 来源链 | 切法 | 形态 | provenance |
|---|---|---|---|---|
| 火的记忆II：面孔与面具 | `PDFium` | 复用**已有 v3 产出**切片 | `dir` | `3.1.6` / tier `''` / mode `''` |
| 查士丁尼法学总论 | `PScript5.dll` + `Distiller 7.0`（2006） | `pdfseparate`+`pdfunite` 切 2 页，**现场 v3 转** | `dir` | `3.4.0` / tier `''` / mode `''` |

**验到的**（三条断言全过）：

```
① provenance 落库        dir / 3.4.0 / tier='' / mode='' / 风险计数 0 / status=done
② 脚注入库 + judge_version='v2'
③ header/footer/page_number 进脚注表 = 0        ← v2 的类型优先明确拒
```

**bbox 四口径归一化也被端到端验到了**：切片时记下原始 pt（475/500/460/508，页高 571），
预先算出 y/h = 0.832/0.876/0.806/0.890，**导入后 DB 里的值逐位吻合**。

#### ★ 关键：第三本给出了 v1 的真书反证

火的记忆II 的脚注**一律贴页脚**（y/h 0.806–0.890），v1 也会全收 ⟹ **测不到 v2 的收益**。
查士丁尼那页是**长脚注从页面上部开始**，同一批数据上：

```
v2（类型优先）收 6 条
v1（y >= 0.70）收 1 条          ⟹ v1 丢掉 5 条真脚注 = 83%
```

六条 `page_footnote` 的 y0/h 分别是 **0.246 / 0.379 / 0.493 / 0.607 / 0.626 / 0.759** ——
前五条全在 0.70 以上被杀。而页眉/页码在 0.095，v2 用类型明确拒。

⟹ 至此 **v1 的两头错都有真书背书**：
· 原神剧情合集：页脚冒充（`footer` 占 24%）→ v2 拒
· 查士丁尼：真脚注被 y 门槛误杀（**83%**）→ v2 收

#### 两条操作发现（属于 PDF 准备阶段，记此备查）

1. **v3 的 `-b/--backend` 取值是 `hybrid-engine`**，不是产出目录名那个 `hybrid_auto`
   （写成后者会直接报 `Invalid backend`）。
2. **v3 的 CLI 没有页范围开关**（`--help` 里无 start/end/pages），所以"现场转几页"
   必须**先切页**。本次用 `pdfseparate -f 24 -l 25` + `pdfunite`（poppler-utils，
   项目已在用）切出 2 页再转，2 页 OCR 约 45 页/秒的批处理速度。
3. 该书是**扫描件**（`pdffonts` 该页零字体、`pdfimages` 1 张图、9.8MB/257 页），
   所以 `_ocr_enable: True`，正文全部来自 OCR —— 正是我们想测的那条路。

#### 5.7.1 v4 那一轮（同一个 2 页切片，扫描件）

v4 跑了两条臂（`--format zip` 与 `--format markdown`），zip 解包后导入。

**provenance 四列第一次全部非空**：

```
mineru_form        = 'full'        ← zip / 解包形态
mineru_version     = '4.0.7'
mineru_tier        = 'standard'    ← ★ v3 恒为 ''，这是第一次有值
mineru_parse_mode  = 'ocr'         ← ★ 第一次非空，且是扫描件该有的值
```

（导入的 md 名是 `markdown.md` —— 也就是 §5.3 修的那个"解包成员名是**固定名**不是
`<stem>.json`"的分支，这次在真跑。）

**v4 也发 `page_footnote`** —— 这原本是个开口的疑问。实测 v4 在同样的两页上给出
**与 v3 几乎逐位相同的六个 y0/h**（0.380 vs v3 的 0.379；0.627 vs 0.626）：
**两次独立解析在版面上吻合。**

```
v2 收 7 条 / v1 收 2 条   ⟹  v1 丢掉 5 条 = 71%
```

#### ★★ 回退分支救了 v4 误标的一条真脚注（原以为是假阳性）

v4 的 `page_footnote` 只有 6 条，但库里进了 **7** 条。多出来那条是
`p1 [text] y0/h=0.820` —— 由 v2 的**回退分支**（"其余类型 → 底部区域"）收的。

**判定：它是真脚注，v4 的版面模型把它误标成了 `text`。**

```
① curules aediles，古罗马掌管公共建筑物警务、交通、市场、戏院和公共娱乐等事的官员。
```
三条证据：
1. **以 `①` 开头**（脚注标记）；
2. **同页正文 y0/h=0.133 处有 `市政官①`** —— 正文正在引用它；
3. 内容是注释体（词条 + 释义），且落在页面底部（0.820）。

⟹ **这条把回退分支的性质说清了**：它不是假阳性来源，是**"生产者误标"的安全网**。
若无回退分支，v2 在这页只能拿到 6/7（丢掉这条真脚注）。

| 口径 | 本页拿到 | 说明 |
|---|---|---|
| **v2（带回退）** | **7 / 7** ✅ | 含救回被误标的那条 |
| v2（**去掉**回退，纯类型判据） | 6 / 7 | 会丢被误标那条 |
| v1（无类型筛选 + y≥0.70） | 2 / 7 | 两头都错 |

⚠️ 但回退分支**理论上**也可能收普通底部正文（假阳性）。本页没有出现，
**样本量 1 页不足以定论**；对"真吃脚注"的语料，这个分支的净收益需要更多页来标定。

#### 5.7.2 v4 `flat` 形态：内联 base64 物化第一次真跑（`gehling1996.pdf` 第 3–4 页）

前面两轮都没能触到物化分支，原因查明：**扫描件不吐内容图**（zip 里连 `images/` 都没有）。
这次换一篇**带插图**的论文（古网 Paleodictyon / gehling1996，页 3–4 有 Figure 2/3/4），
flat md **713,516 字符 / 2 张内联 jpeg** —— 终于把那条路跑通了。

**provenance**：

```
mineru_form        = 'flat'        ← 靠 base64 签名检出（_looks_like_v4_flat）
mineru_version     = ''            ← flat 无元数据
mineru_tier        = ''
mineru_parse_mode  = 'unknown'     ← ★ 第三态第一次真跑，与 v3 的 '' 语义不同
kangxi_radical_count = 0    pua_count = 0    status='done'  expected_chunks=6
```

**物化分支的证据**（三条互相独立）：

| 证据 | 值 |
|---|---|
| `images` 表 | **39,521 → 39,523（+2）** —— 正好是 flat md 里的 2 张内联图 |
| 落盘路径 | `/mnt/<drive>/…/memoria-del-hielo/images/<md5>`（KB 存储目录，不是用户产出目录） |
| **两张图的 `ocr_text` 长度** | **689 / 494 字符** ⟹ **真的 OCR 了** |

最后一条是本轮**最重要的顺带验证**：`parse_mode='unknown'` ⟹ `skip_ocr_for_images=False`
⟹ **D1 那条「unknown → 保守走 OCR，不静默丢信息」的规则第一次在真书上生效。**
（此前两轮 v4 都是 `parse_mode='txt'`，那条规则连触发条件都没出现过。）

**顺带一条小发现（未查证，不敢下结论）**：两张物化图的 `ref_count = 2`，
而按原设计"物化阶段不自增、只由 `process_image_refs` 增一次"应当为 1。
疑似 `INSERT` 的列默认值就是 1、管线再 +1。
**没有查 `images.ref_count` 的 DDL，所以这里只报现象不报原因。**

**没验到的一处（也是我自己的探针缺陷）**：我本想确认"改写后的引用在 chunk 正文里长什么样"，
但正则写成 `\(([^)]{0,64})\)`，而存储路径有 ~72 字符 ⟹ **没匹上**，于是那一步输出为空。
能确认的只有**反向**：6 个 chunk 里 `data:image` 均**不存在** ⟹ base64 确实被换掉了。
**引用改写的具体形态这一条，本轮没验，不当结论。**

**仍未触发**：`skip_ocr_all`（flat 的 `parse_mode='unknown'` → `skip=False`，走的正是 D1 的保守分支；
要触发它需要 `parse_mode='ocr'` **且** 该产出里有内容图 —— 本轮扫描件那两页有 `ocr` 却没图，
这篇有图却是 `flat`/`unknown`）。

#### ★ `flat` 形态**带着脚注类型** —— 推翻了 D5 的前提

当初 D5 裁定"扁平 md 显式声明无脚注"，依据是"扁平 md 零元数据"。
实测**推翻了这个前提的一半**：扁平 md 的行内确实没有元数据，但它**用 HTML 内联块
携带了块类型**：

```html
<small><span class="docvortex-page-footnote" data-block-type="page_footnote" style="color:#6b7280">
```

**`data-block-type="page_footnote"` 就在 markdown 里。** 也就是说脚注类型**是能捞出来的**，
只是我们（按 D5）选择了不捞。

⟹ 两条路：要么改适配器（flat 也从 md 里捞 `data-block-type`），
要么在文档里写明"我们知道它在，但选择不捞"及理由。**这是设计决定，不该顺手改。**

#### 仍未覆盖（诚实列出）

（v4 `full` 形态已补上，见 §5.7.1。）仍缺三条，且**缺的原因都已查明**：

| 仍缺 | 为什么缺 |
|---|---|
| `materialize_inline_images` 的真 base64 | **扫描件根本不吐内容图**（zip 里连 `images/` 目录都没有），flat md 里 `data:image/` 出现 **0 次** ⟹ 这条路只有"带插图的电子书"才测得到 |
| `skip_ocr_all` 的可观测效果 | 本轮 `parse_mode='ocr'` 已把 `skip_ocr_all=True` **传下去**了，但那 2 页**没有内容图可跳** ⟹ 传了、无处生效 |
| v4 `flat` 形态的 provenance | flat 无元数据 ⟹ `parse_mode='unknown'`（**与 v3 的 `''` 语义不同**）；本轮只导入了 zip 那条臂 |

#### 结论：按既定政策**回退**，不用百分比粉饰

我们自己的 `mineru_batch.py` 早就定过一条政策（`split_oversized` 的 `dropped`）：

```python
except Exception as e:
    eprint(f"  !! 跳过（MuPDF 解析失败）: {fp.name} ({str(e)[:80]})")
    dropped += 1
```
```
dropped 是"被丢弃、本轮不会出现在任何任务里"的输入文件数。这个数必须往上报——
否则一本打不开的 PDF 只留下滚动日志里的一行警告，最后的 OK/FAIL/SKIP 结算根本
不认账，跑几千本时你并不知道少了几本。
```

⟹ **政策 = 不修、不猜，但必须单独计数上报**（`DROP` 独立结算，不混进 OK/FAIL/SKIP）。

**这条判了那个 CFF 分支死刑**：它把 105 个 CFF 字体重判成 `OK`，
而它的宽度语义是错的（自检 `space = M = 0.269` 未过）⟹ **系统性地不可能判出可疑**
⟹ **那是把"判不出来"混进了"没问题"，比不判更坏。** 已回退：CFF 不给判据，
标 `G_UNJUDGED_NON_SFNT` 单独计数；那个会生产假安慰的 helper 已删除。

**回退后的诚实输出**：

```
G_UNJUDGED_NON_SFNT  127     ← CFF（Type 1C / CID Type 0C），判不了，独立计数
B_WIDTH_FULLWIDTH     50     ← 已验判据
A_NO_CMAP             47
OK                    24
D_NO_ASCII_IN_CMAP     1
覆盖率 74 / 249 = 30%        不可判 175 = 70%
```

**读法（不粉饰）**：

- 已判 74 个里 **50 个可疑 = 命中率 68%** ⟹ 未判的那 70% **大概率为同构**，
  保守处置不是疑心病；
- 所以这本书的结论**不是一个百分比**，而是一句话：
  **「30% 已判、其中 68% 可疑；70% 判不了」⟹ 本书文本层不单独作判据。**
- 之前那个「27.1% 页 / 0.17% 字符」只在**已判的那 30%** 上成立，
  **不能当全书的数**。

> **设计原则（用户自述）**：
> **PDF 就是这么一坨玩意，Adobe 自己都不能解决 100%，我们干嘛呢。**
> 落实形态不是"努力修"，而是**检测 → 计数上报 → 绕过去**（路由到视觉读数）。
> 推论：**体检的产物是一个"路由决定"，不是一个测量值。**
> 所以精度不重要 —— 它只决定"走文本层还是走视觉"，而走视觉的成本可接受
> （我们本来就有 MinerU）。

> **⚠️ 本节的一处越界（已更正）**：我们曾把上面这条与本项目里另一支工具
> `mineru_batch.py` 的 `DROP` 政策（"不修不猜，但必须上报"）当成同一件事，
> 甚至把它的引用写进了本项目的脚本与 README。**那是越界**：
> `mineru_batch.py` 是**批处理编排器**，它关心的是"这批任务里少了几个文件"；
> 本项目关心的是"读出来的字能不能信"。**关切不同，规矩不能互相移植。**
> 本项目自己的理由其实更直白：**读数要进 chunk、进向量、进 LLM 上下文，
> 所以「我没查」与「我查了没问题」必须在输出里可区分。**

> **实际采用的口径（用户裁决）**：**默认宽松**（只有判据命中才算坏），
> 另给 `--strict`（未判也算坏）。依据是实测：arXiv 语料 3/3 篇可疑 = 0、
> 判定全由"未判"驱动（它们用 Type 1C，而裸 CFF 判不了）——
> 严格口径会让整批论文返回同一个常数、工具失效；
> 而宽松口径在已知坏书上仍判走视觉（可疑 50 > 0）。

剩余 2 个真·解析失败：`AttributeError: charset`（`font-10456.cff`、`font-4827.cff`）。
另有 47 个 `A_NO_CMAP`（TrueType 无 cmap）与 14 个 CID-keyed CFF 仍不可判。

#### 还没堵的洞（数字是**下界**）

`TTFont` 打不开裸 CFF（`Not a TrueType or OpenType font (bad sfntVersion)`），
**127 个 `.cff`（Type1C）字体未判**。所以 27.1% / 0.17% 是**下界**，
真实值可能更高。（已知坏的那个 `AFEICP+Characters` 是 `.ttf`，已判对。）

### 5.6 机理补完：**CMap 是程序，不是表** —— 所以"全局一致"从来不存在

出处：`https://mp.weixin.qq.com/s/3SyTLJsn8OE6KlBQ7OLRMA`（作者谈 PDF 内部结构的札记）

这一篇补上了 §5.3–5.5 一直缺的**机理层**：

> **「CMap 和 cmap 还不太一样。CMap 是 PostScript 引入的概念，cmap 是
> OpenType/TrueType 里面的概念。但是 CMap 很厉害，因为它是一种**程序**。
> 它定义了编码的本身（集合），又定义了表现形式（byte 序列的定义），
> 又定义了编码到 glyph 映射。所以 PostScript 本身是不使用 ICU 这类 Unicode
> 处理库的，因为 CMap 就满足了这一点。」**

⟹ **这就是为什么"ToUnicode 做了什么"没有全局一致性保证**：它是一个**程序**的输出，
而程序由各自的生产者写。第六篇那句"每个软件都不一样，以及每个字体都不一样"，
到这里有了根：**不是实现懒，是规范把这件事留给了程序。**

同篇另两条直接对上我们的现象：

> **「PDF 中的 TrueType，定义是 simple font。这是没问题的，因为当年确实是
> simple font，连 CJK 支持都没有。后来有了，但是 PDF 把这类 TrueType 字体
> 定义为 CIDFontType2。」**

我们那 50 个可疑字体用的是 **simple TrueType 承载 CJK 字形 —— 越界了**。
（`pdffonts` 的 type 列区分 `TrueType` 与 `CID TrueType`，所以这是一条
**廉价的结构性判据**，与 5.5 的宽度判据互为旁证。）

> **「PDF 能不能够保证在所有平台上看到的都是一样的，就很难说。」**
> **「macOS 以及以前的 Mac OS X 中对于 PDF 的支持其实并不是特别好……
> 但是因为读写都有 bug，反倒是在 macOS 上能够在某些时候表现一致。」**
> **「macOS 的 Preview 在显示图片的时候，尤其是宽高都比较小的时候，
> 会使用模糊策略，似乎是高斯模糊。甚至打印到纸上都不能幸免。」**

⟹ 又两条纪律：
· **"一致 ≠ 对"** —— 这次的一致来源是**读写都错**（与 §5.4.1 的"读同一张坏表"同构）；
· **渲染路径也不是绝对可信的**（§5.4.2 说"渲染几乎不会坏"，这里给出了反例的边界：
  小尺寸位图会被渲染器**模糊**）。§5.4.2 的措辞据此收紧为：
  **渲染路径可信的是"字形身份"，不保证"像素还原"。**

#### 因果链补完：生产者**为什么**用 ASCII 槽位

出处：`https://mp.weixin.qq.com/s/f9VgvuxkHQ6NGu0WuFJCkw`（测 Office 365 的字体支持）

> **「还有打印测试，使用的是微软的虚拟打印机 Microsoft Print to PDF：
> `218KB` / `52876KB`」**

同一份文档，**整字体嵌入是子集化的 242 倍**。作者同篇的结语：

> **「字体处理尤其是子集化是世界级难题。你要说是技术难题么，给了足够的时间
> 总能搞出来。问题是时间怎么来。这些东西可不是三五个月就能解决的问题，
> 几乎按年计算。」**

⟹ **50 个可疑字体不是"缺德"，是工程妥协**：要在一本书里放几十种稀有文种的字形样例，
嵌入完整字体代价不可接受，于是**用 ASCII 码位当槽位、自制小字体**。
副作用是文本层变成槽位号。
**把"缺陷"还原成"在约束下的选择"，是判断"要不要绕过它"的前提。**

另：同篇实测 **编辑器里显示正确 ≠ 另存 PDF 正确**（Cascadia `gvar` 另存后处理失败；
思源宋体 VF 被转成图片）—— 又一个层间分裂，与我们的**渲染对/提取错**同构。

#### 一个最好的类比：QR 汉字字体

出处：`https://mp.weixin.qq.com/s/yKqUWAA2I58VwxxSsdAOeg`

作者用 `fontforge` 造了一个字体：**每个汉字码位（U+4E00 起）画成该汉字的 QR 码**。
于是 `\setmainfont{qrcode-han.ttf}` 之后，正文写「我能吞下玻璃而不伤身体」，
**印出来是一排 QR 码**。

这是"码位与字形之间没有任何必然联系"的**善意的极端演示**：
Unicode 规定**语义**，字体规定**外观**，两者解耦。
我们那 50 个字体做的是同一件事，只是**动机是省空间、后果是文本层说谎**。
⟹ 向读者解释"为什么 `>` 可以是 `澤`"时，这个例子比任何抽象说明都好用。

#### 作者自述（可引）

> **「我还是抱有一点理想主义倾向……我觉得就是：just for fun。有意思就行了，
> 挑战自我认知也是一个有意思的事。我很期待的是，我造出来的东西，
> 能够全量的彻底地控制。」**

另有一条与我们语料直接相关的提醒：

> **「我是很怕用户拿来一批国产软件搞出来的 PDF，有些软件（不点名）生成的
> PDF 文件都快有十几年的历史了，产生了大量的不良数据。」**

**对照组**：另一本《帝国行政论》（君士坦丁七世）是**全页扫描 + 隐形 OCR 层** +
Base-14 `Times/Helvetica`，文本层里 `(53382 't&ii)` 而正确是 `(53 382 τῶι)`。
两本正好是"文本层可信"与"文本层撒谎"的两个极端。

---

## 六、源 PDF / 上游链路的坑

| # | 事实 | 判据 |
|---|---|---|
| 6.1 | 一本 CJK 书里有 **11,018（v3）/ 11,013（v4）个康熙部首码点**，v3 与 v4 **一样脏** → 是 **PDF 的属性**，不是某一代 MinerU 的 | 两代产出字符统计 |
| 6.2 | 该 PDF 的 `Creator/Producer` = **calibre 4.18.0**，字体 = `NotoSansCJKsc` **Type 3** / Custom / `uni=yes`（**不是扫描件**）→ 坏编码是 EPUB 源沿转换链带下来的 | `pdfinfo` / `pdffonts` |
| 6.3 | 思源/Noto 里 **U+2F49 与 U+6708 共用一个 glyph**，所以「⽉背征途」显示就是「月背征途」→ 归一是**把 glyph 层面的同一性还原到 code 层面**，不是猜语义 | 七篇之一原例 |
| 6.4 | 扫描 PDF 的"文本层"是**上一个 OCR 引擎的转录**，不是原文 | 七篇之四 |

---

## 七、我们自己的坑（先列自己的，免得写成"全是别人的锅"）

| # | 坑 | 静默机制 |
|---|---|---|
| 7.1 | `_v4_block_text` 只抽一层，遇嵌套就把内层列表 `str()` 成 **repr** 塞进正文 | 静默截断；产出"看起来合理"的垃圾 |
| 7.2 | `_load_v4_dir` 找 `<stem>.json`，真名是 `middle_json.json` | 静默跳过整条分支 |
| 7.3 | 夹具被我改名 → 测试在**假夹具**上自证通过 | 判据少一维 |
| 7.4 | `census_report` 按 raw 名计数、白名单却写规范名 → `paragraph_title` 被误报"未识别" | 两张表各写各的 |
| 7.5 | 探针四连 bug（见 1.2） | 四次全静默 |
| 7.6 | 测试里写死 `page_idx == {2, 17}`，夹具选页一变就漂 | 写死数据而非形状 |
| 7.7 | 字符串里用 ASCII 双引号 → 语法错 | 不静默（这次红了） |
| 7.8 | 以为"全文 NFKC 可行" | 实测会动 **3,571 个「，」**、长度 +46、**全部偏移失效** |
| 7.9 | 以为"三档一致就是互证" | 见第五节，三方同源 |
| 7.10 | 以为"探针窗随便取" | 前 20 页纯拉丁，压不到罕见文种 |
| 7.11 | 把上轮"bbox ×1000"的结论误归给 v4 并宣称"推翻" | 原结论出自 v3 `_content_list*.json`，**出处没错，是我误归** |

---

## 八、MariaDB / jieba / BGE-M3 的实测边界

| # | 事实 | 判据 |
|---|---|---|
| 8.1 | `'⽉' = '月'` **为真**（`WEIGHT_STRING` 同为 `FB40E708`），但 **`LIKE` 不等**（`'⽉球' LIKE '%月球%'` → 0） | 实测 |
| 8.2 | 推论纪律：**做字面量探测一律加 `BINARY`**，否则 collation 等价会被当成命中 | 同上 |
| 8.3 | `innodb_ft_min_token_size = 3`，而 `tokenize_query` 产出**全 AND**（`+月球* +背面* +着陆*`）→ **含 2 字词的 CJK 查询必 0 命中**。硬证据：`'月球'` **在被索引的列里确实存在，`MATCH` 却 = 0**；`'国家队'`(3字)=8、`'北京航天'`(4字)=5 正常 | 实测 |
| 8.4 | ⟹ 中文查询**实际全靠向量路** | 8.3 的推论 |
| 8.5 | BGE-M3 tokenizer 的 normalizer = `Sequence[Precompiled(nmt_nfkc charsmap 316720B), Replace]` → **分词前先做 NFKC** → 康熙部首**在进模型前就被折成汉字** | `tokenizer.json` |
| 8.6 | 实测印证：同一 chunk 污染/归一两版 **cos = 0.999904 ~ 1.000000**（11 个里 8 个恰为 1.000000），三查询 top1 全不翻转 | 真模型真数据 |
| 8.7 | 但 **CJK 部首补充的简体部首**（`⻜⻋⻓`，988 个实例）Unicode 不给等价、NFKC 折不动 → **向量路对这部分不免疫** | 实测 |
| 8.8 | jieba 把部首切成**单字** token（`'⽉球'` → `'⽉ 球'`），而 1–2 字 token 根本不进 FTS 索引 → **部首对全文路的影响被 `min_token_size` 完全吞掉** | 8.3 |
| 8.9 | `ORDER BY` 全项目只有一处按文本列排序 → 断锁只在标点/拉丁子集上有理论影响，且实测这些子集相对次序一致 | `grep` + 下两版 DUCET 比 |

---

## 九、待补（开着的问题）

1. **DocVortex 4.3**：哪个开关让 `standard` 吃 `_PUNCTUATION_TRANSLATION`。
2. **第 19 章（750–850 页）三方对读**：`standard×{txt,ocr}` + `flash×txt`，
   才能回答"这本上 OCR 到底准不准"。
3. **`ﬅ → ft` 的影响面**：这书有没有真命中；上游是否已修。
4. **`visible_*` 的版本烙印**：`is_visible` 依赖 `Z*`/`Cc`/`Cf`/`Cs`，而只有 `Cc`/`Co`/`Cs`
   有稳定性条款。存量 169,858 个 chunk 的 `visible_*` 带着"某个 Unicode 版本的烙印"。
   （缓：它不参与回查切片，回查走 `char_start`/`char_len` 码点偏移。）
5. **NFD 文件名**：导入侧未对文件名做 NFC 归一，而幂等键是 `md5(source_path)`
   → 从 macOS（HFS+ 强制 NFD）拷来的同名书会算出不同 md5、导入两次。现状 686 本 0 例。

---

## 十、输入端三改与一条旧裁定的作废（2026-09-29）

官方口径（`docs/reference/output_files`，非推断）：`mineru-kit parse --format` 只有
**markdown / middle_json / zip** 三个取值；`ParseResult.save()` 写
`markdown.md` + `middle_json.json` + `structured_content.json` + `images/`（有原始模型
输出时再加 `model_output.json`）。官方同时明确指出：**standalone structural JSON
"is not a complete result package"** —— 那句就是"`--format middle_json` 的产出不能入库"的权威依据。
（归档名 `<stem>.zip` **不写进官方文档**（"ZIP 文件名由各入口决定"），故本库实测确认，
单输入与「多变参 + 单 `-o`」两种形态都验过。）

| # | 事实 / 决定 | 依据 |
|---|---|---|
| 10.1 | v4 的 `--format` 缺省由 `markdown` 改为 **`zip`** | flat `.md` 把 tier / parse_mode / 页号 / 脚注结构 / 图片类型**全丢**；zip 与它同一次 parse、只换序列化 |
| 10.2 | 实测：77 页论文 flat = 6,500,893 B，其中 **6,382,676 B 是 base64**（正文实际只有 118,502 B）；zip 的 `markdown.md` = 119,977 B，**0 个 base64**、57 条 `![](images/page_*.jpg)` 且全部命中 | 真样本 `R:\ermakov2017.{md,zip}` 对读 |
| 10.3 | 两种形态的正文**几乎逐字相同**（118,502 vs 119,977）⟹ 差别**只在图片引用的写法** | 同上；这条是"分析总结在 md 上做、两代都成立"的反证 |
| 10.4 | **zip 只解需要的成员**：跳过 `model_output.json`（该样本 8.4 MB，本项目从不消费） | `zip_input.SKIPPED_MEMBERS` |
| 10.5 | **★ D1 的「parse_mode 未知 ⟹ 保守走 OCR」在**内联图**这一档作废**（用户裁定） | flat 是唯一 unknown 的形态，而它恰好没有 `structured_content`、拿不到图片类型映射 ⟹ 照片/插图/公式全会被当文字读，OCR 文本进正文即噪声。**没有信息被丢**：图片字节仍落盘（`images/{md5}`）、引用仍在正文，丢的只是"自动把 OCR 文本灌进正文" |
| 10.6 | 新的 zip 形态**不受** 10.5 影响：图片是独立文件 + 有类型映射，照常走 `process_image_refs` 的既有判据 | `parse_mode='ocr'` 或类型命中才跳过 |
| 10.7 | **身份 vs 工作路径**必须分开：zip 的幂等键 `md5(source_path)` 与书名挂在 `.zip` 上，工作路径才是解包正文 | 混用则"清一次 cache 就换了本书"，且每本书都叫 `markdown` |
| 10.8 | zip 的接受判据在**枚举里**（归档含 `markdown.md`），不在导入时 | 否则被拒的 zip 算进总数 N 却永远导不进来，N 与 X 就散了（"总说 13、实际跑了 12"） |
| 10.9 | `(".md", ".txt")` 此前在 **4 处**各写一份 → 收拢成 `SRC_EXTS` 唯一真相源 | 加一个扩展名就必然漏一处的经典面 |
| 10.10 | zip-slip 与总量上限一律**整包拒绝**，不做"清洗后继续" | 静默改路径比拒绝更危险：调用方以为拿到了完整产物，实际少了东西 |
| 10.11 | **同主干撞车**：`<stem>.md` 与 `<stem>.zip` 并存时 **zip 优先**，md 跳过并明说 | 两者是同一次解析的两种序列化。真样本实测：两个都导 = 67 块 + 66 块、正文逐字相同（幂等键不同 ⟹ 入库两次），且 flat 那份会被 zip 的侧车认领成 `form='full'` |

**我们自己的坑（第十节）**

| # | 坑 | 静默机制 |
|---|---|---|
| 10.a | `zip_input._root_prefix` 落空返回值写成 `''`，而 `''` 是"正文在归档根"这一**合法结论** ⟹ `is_mineru_zip()` 对**任何** zip 都返回 True | 把"没找到"混进"找到了、只是位置不同"。抓法是把非 MinerU zip / 裸 json 包 / 坏 zip 各跑一条**负对照** |
| 10.b | 伪 DB 替身只认一种 SELECT 形状（单列名），于是"复用已有记录"这条最该验的路径被整个绕过 | 替身比被测代码窄 ⟹ 测试在假路径上自证通过 |

---

## 关键词速查表

| 关键词 | 节 |
|---|---|
| 裁判收错答题卡 / 四条纪律 | 1.1 |
| 静默截断 / 静默跳过 / 静默空值 | 1.1, 1.2 |
| 夹具自证 | 1.3 |
| Strong Normalization Stability | 2.1 |
| General_Category 不朽 / U+180E | 2.3, 2.4 |
| UCA / DUCET / 隐式权重 / FB40E708 | 2.7–2.10 |
| parse_mode 不由 tier 决定 | 3.1 |
| bbox 四种口径 | 3.5 |
| 扁平 md 丢元数据 | 3.4 |
| `_PUNCTUATION_TRANSLATION` / standard 折引号 | 4.1 |
| `ﬅ` → `ft` | 4.2 |
| 三方同源 / 不构成互证 | 五 |
| Type 3 字体 / calibre / 康熙部首 | 6.1–6.3 |
| `min_token_size=3` / 2 字词必 0 | 8.3 |
| `nmt_nfkc` / 向量路免疫 | 8.5, 8.6 |
| LIKE vs `=` 分裂 / 必须 BINARY | 8.1, 8.2 |

## 近似入口点

- 「这条结论靠什么支撑」 → 每条都有「判据」列，全是可重跑命令或可复算数字
- 「哪些是上游的锅」 → 二/三/四/六节
- 「哪些是我们自己的锅」 → 第七节（**先看这节**）
- 「为什么不能全文 NFKC」 → 7.8 + 2.12
- 「为什么向量路不在乎部首」 → 8.5–8.7
- 「全文路为什么对中文失效」 → 8.3, 8.4, 8.8

## 引用

- 七篇（搞 TeX 的博主，微信）：
  1. `https://mp.weixin.qq.com/s/W6Q3BI-Sg_IimxFtmZvbCg` — TeX accent 组合范式 vs Unicode NFD
  2. `https://mp.weixin.qq.com/s/d10B2CPaYe4gD25l1lphug` — 老 PDF 的 ToUnicode 修复
  3. `https://mp.weixin.qq.com/s/BmSnandaBQ3krtqL0--iLA` — 一个 glyph 对多个 code（`⻣` 原例）
  4. `https://mp.weixin.qq.com/s/D6uzuBk61FFAgeIxxlsbBw` — PDF 是电子照片；隐形 OCR 层
  5. `https://mp.weixin.qq.com/s/hblfkv47Fa8Mks533hHCMQ` — 可复制性"看制作者心情"
  6. `https://mp.weixin.qq.com/s/l9QJBVTmuI_NNZ9X87NHMA` — MuPDF / ToUnicode 的烂账
  7. `https://mp.weixin.qq.com/s/1IvdQc4VcvihL3m4RERNuw` — HFS+ 强制 NFD；本文标题那句
- 该系列另几篇（本文引用了其技术内容）：
  `https://mp.weixin.qq.com/s/3SyTLJsn8OE6KlBQ7OLRMA`（CMap 是程序；simple font vs CIDFontType2）、
  `https://mp.weixin.qq.com/s/f9VgvuxkHQ6NGu0WuFJCkw`（子集化 218KB→52876KB）、
  `https://mp.weixin.qq.com/s/yKqUWAA2I58VwxxSsdAOeg`（QR 汉字字体）、
  `https://mp.weixin.qq.com/s/6ZR90gnFce7knZcYJ805Fg`（Acrobat 不是 last resort；最大公约数原则）、
  `https://mp.weixin.qq.com/s/hblfkv47Fa8Mks533hHCMQ`（GB2312/SJIS 都是「天地玄黄」；ToUnicode 可写可不写）
- Unicode 官方：[Stability Policy](https://www.unicode.org/policies/stability_policy.html)、
  [UTS #10 UCA](https://www.unicode.org/reports/tr10/)、
  [MinerU 输出契约](https://github.com/opendatalab/MinerU/blob/master/docs/en/reference/output_files.md)
