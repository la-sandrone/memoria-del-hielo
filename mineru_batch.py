#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["natsort", "pathvalidate"]
# ///
#
# ============================================================================
#   ★★★  使用前请先改一处：搜  ENV_ACTIVATE_CMD  ★★★
#
#   它在下面这段说明文字结束后的**第一个区块**，写的是「激活 MinerU 环境的命令」。
#   把它改成你机器上真正能用的写法即可 —— 其余参数都有可用的缺省值。
# ============================================================================
r"""
MinerU 批量处理脚本 v6.5 -- bash 生成器架构（MinerU v3 / v4 双适配 + 原生 TTY + 原生 Ctrl+C）

本文件是 memoria-del-hielo 的**配套工具**：把一批 PDF / Office / 图片文档
批量喂给 MinerU，产出知识库能直接导入的 md 与 zip。
用之前只需改一处 —— 文件顶部的 ENV_ACTIVATE_CMD（见上）。

用法:
  python mineru_batch.py [选项] 文件/目录 [文件/目录...]
    -o D:\\解析素材2\\tmp           输出根目录（缺省=各书所在目录，各归其位）
    --mineru-version auto         auto|3|4（缺省 auto：用 ENV_ACTIVATE_CMD 激活后问环境，
                                  有 mineru-kit 即 v4，优先 4；显式 3/4 可跳过探测）
    --venv ~/venv-mineru-v4       覆盖环境：激活命令改为 source "<X>/bin/activate"，
                                  探测也走它（缺省用顶部的 ENV_ACTIVATE_CMD[_V3]）
    --tier standard               [仅 v4] 解析档位 flash|basic|standard|advanced
    --v4-format zip               [仅 v4] 产出序列化 zip|markdown（缺省 zip）
                                  zip=markdown.md + middle_json.json + structured_content.json
                                     + model_output.json + images/（保留 tier/parse_mode/
                                     页号/脚注结构/图片类型；图片是独立文件而非 base64）
                                  markdown=单个 <stem>.md，图片 base64 内联，全部元数据丢失
    --backend hybrid-engine       [仅 v3] 解析后端 pipeline|vlm-engine|hybrid-engine|*-http-client
    --v4-recycle book             [仅 v4] book=每书一进程（缺省，进程退出即整块回收内存）
                                  batch=全部书共用一个进程（省掉每书一次引擎启动，内存只在本批结束时回收）
    --lang ch                     语言（缺省不指定。v3 走 --lang；v4 无 --lang 选项，改走 MINERU_LANG 环境变量）
    --no-skip                     强制重处理已有结果的书
    -r                            递归扫描子目录
    --split-limit N               PDF 页数超限自动拆卷（默认 400；=0 不拆；pipeline 后端不拆）
    --dry-run                     只打印生成的 bash 脚本，不执行（拆卷也只预演、不写盘）
    --extra-args ...              透传给 mineru / mineru-kit CLI 的额外参数（放最后）
    --max-path N                  Win32 路径长度上限（默认 260；=0 不检查长度，只查名字合法性）
    --skip-bad                    跳过名字非法/路径超长的书，只跑其余的
                                  （默认：列出全部问题后退出、不做任何改动，交你手工改名）
    --images                      目录扫描时也收图片（默认只在显式点名文件时收）
    --include-mineru-out          目录递归时不排除 MinerU 产出目录
                                  （默认排除 auto/txt/ocr/hybrid_*/images）

也支持 @文件列表（列表内相对路径 -> 相对列表文件所在目录）：
  python mineru_batch.py @D:\\book_list.txt

格式矩阵（v6.3：4.0 新收了一批扩展名，按版本分流）:
                      v3（mineru 3.4.0）              v4（mineru-kit 4.0.7）
  mineru 系          13 个                          28 个
                     pdf/xlsx/pptx + 图片            = 左列 + doc/ppt/xls/rtf/
                                                     odt/ods/odp/ofd/html/htm/
                                                     shtml/mhtml/mht/csv/tsv/epub
  pandoc             epub, docx                     docx
  --tier             不适用（v3 用 --backend）      pdf 与图片生效；其余上游强制 flash
  --format           ✗ 无此选项                     zip（缺省）/ markdown（见 v6.4 一节）
  * docx 两代都留 pandoc：文本类文档它比 DocVortex/VLM 快得多、不占 GPU，产出也干净。
  * epub 在 v3 没有原生支持（只能 pandoc），在 v4 有 —— 所以 v4 交回 mineru。
  * 图片（png/jpg/jpeg/webp/gif/bmp/tiff/jp2）走 tier：standard 档下会过 VLM。
  * txt/md/tex/rst 之类上游只收进文档库、不参与解析，本脚本不收。
  * 默认输出目录 = 各书所在目录（各归其位）；
    -o 显式指定时所有书集中输出到该目录。

扫描纪律（v6.3；两条都有强制开关，默认都是有理由的保守值）:
  1) **图片默认不扫**。只有显式点名文件（命令行参数或 @列表行）才收图片；目录展开
     出来的一律不收，除非加 --images。
     依据：<corpus-root> 下有 142,709 个 jpg，几乎全是 v3 产出里的
     hybrid_auto/images/<sha256>.jpg —— 递归扫进去就是 14 万个垃圾任务。
  2) **目录递归默认剪掉 MinerU 产出目录**（auto/txt/ocr/hybrid_auto/hybrid_txt/
     hybrid_ocr/images）。不剪的话 -r 扫书库会把既有产出里的 <stem>_origin.pdf
     当成新书反复解析（<corpus-root> 下实测 245 个）。要一起扫加 --include-mineru-out。
  剪枝用 os.walk 改 dirnames 实现——不是"走进去再过滤"，是压根不进去。
  显式点名的文件不受这两条限制：你指了它，就是要它。

=================================================================
v6.4 v4 默认改 zip 序列化（本次新增）
=================================================================
官方口径（docs/reference/output_files，非推断）：
  · `mineru-kit parse --format` 取值域 = markdown | middle_json | zip（缺省 markdown）
  · `ParseResult.save()` 写 markdown.md + middle_json.json + structured_content.json
    + images/，有原始模型输出时再加 model_output.json
  · ZIP 内图片名 = images/page_{page_idx}_{parent_type}_{parent_index}.{ext}
    （独立公式用 images/page_{idx}_equation_{n}.jpg）
  · 归档名（<stem>.zip）**不写进官方文档**（"ZIP 文件名由各入口决定"），
    故本脚本实测确认 —— 见下方实测段。

为什么默认 zip：
  1) flat 的 <stem>.md **丢全部元数据**：tier / parse_mode / 页号 / 脚注结构 /
     图片类型一个都不剩。下游知识库因此无法回答"这段文字是读来的还是看来的"。
  2) 官方明确指出：standalone structural JSON（= --format middle_json 的产出）
     "is not a complete result package" —— 它连 markdown 正文和图片文件都没有，
     只剩结构。所以 middle_json 不是一个可入库的产出形态。
  3) 同一份 parse 只换序列化，改动本身便宜。
  4) 代价：zip 体积更大（实测 77 页论文：md 6.5 MB -> zip 11.4 MB；增量来自
     独立图片与 model_output.json）。

实测（2026-09-29，venv-mineru-v4 / mineru 4.0.7，--tier flash --format zip）：
  · 单输入 + -o <dir>              -> <dir>/<stem>.zip
  · 多变参 + 单 -o <dir>（batch）  -> <dir>/<输入1>.zip  <dir>/<输入2>.zip
  ⟹ <stem> 仍**只出现一次**，长度预算模板只多 1 个字符（.md -> .zip）。
  · zip 根成员 = markdown.md / middle_json.json / structured_content.json
    （+ model_output.json）；无图时**不建** images/ 目录。
  · 归档名与成员布局**与扩展名无关**：另用 csv 走 flash 路径复验，同样是
    `<out>/<stem>.zip` + 上面四个成员。⟹ `--format zip` 对所有 v4 扩展名都成立，
    不需要"office/html/epub 要特殊处理"这类补丁。

⚠ 不想吃 zip 的体积时用 --v4-format markdown（回到 v6.3 的扁平行为）。
  zip 内的 middle_json.json 是**侧车**，与知识库那边「拒绝把 middle_json 当输入」
  是两回事：前者是完整结果包的一个成员，后者是只有一个裸 json 的残缺产出。

=================================================================
v6.2 MinerU v3 / v4 双适配（本次新增）
=================================================================
上游在 4.0 把 CLI 整个换代了，两代没有共同语法。实测对照（v3=3.4.0，v4=4.0.7）：

  动作         v3                                    v4
  ---------------------------------------------------------------------------
  单书解析     mineru -p SRC -o OUT --backend B      mineru-kit parse SRC -o OUT --tier T --format F
  批量         （无批量子命令，只能自己循环）        mineru-kit parse S1 S2 ... -o OUT
  质量旋钮     --backend（解析管线）                 --tier（flash|basic|standard|advanced）
  语言         -l / --lang                           ✗ 无此选项 -> 用 MINERU_LANG 环境变量
  落盘         OUT/<stem>/<method>/<stem>_*.md       OUT/<stem>.zip（v6.4 起缺省）
  落盘(旧)                                           OUT/<stem>.md（扁平单文件，图片 base64 内联）
  环境         由顶部 ENV_ACTIVATE_CMD_V3 指定        由顶部 ENV_ACTIVATE_CMD 指定

【为什么 v4 默认「一书一回收」】
MinerU 的 VLM 后端有 ModelSingleton 级的内存泄漏（upstream #4243 已 closed as not_planned，
vllm / lmdeploy 两条后端都中招；lmdeploy 侧另有 KV cache block 逐页不归还）。上游既不修，
唯一不依赖上游的清理机制就是**进程边界**——页表拆解是硬件级保证，不需要信任任何库。
所以 v4 缺省每本书起一个 mineru-kit 进程，进程退出即整块回收。

代价是每本书都要重启一次引擎（实测冷启约 43 s，AOT 缓存命中后 init 约 11.5 s）。
一本书动辄几分钟到半小时，这个开销可接受；书多而小的时候才值得换模式。

  --v4-recycle book   （缺省）每书一进程：内存曲线是锯齿，涨一本、掉一本。
  --v4-recycle batch  全部书共用一个进程（= mineru-kit 原生批量形态）。
                      省掉每书一次引擎启动，但内存只在本批跑完时才回收——等于把 #4243
                      的泄漏量乘以本批书数。只在确认本批总量放得下时才用。

⚠ batch 模式的三个语义差异（是上游设计，不是 bug，但必须知道）：
  1) **首错即停**：mineru-kit 里任意一本书抛错都直接走 exit_with_message，该组剩余的书
     **一本都不会跑**（kit/commands/parse.py 的 for 循环里没有 try 续跑）。
  2) 因此逐书成败无法分辨：统计里单列 BATCHFAIL（=本组整体失败，组内哪几本跑完了未知），
     不虚报成 FAIL——宁可说得含糊，也不给假数。
  3) 没有逐书独立的失败域：一本坏书能拖垮整批。要逐书隔离就回 --v4-recycle book。

【-o 的目录语义（v4 特有的坑）】
mineru-kit 用「末段有没有后缀」判 -o 是文件还是目录（kit/common.py 的
is_output_path_file_like / resolve_single_output_path）。若 <out_root> **不存在**且末段
带点（如 D:\解析素材4\论文.v2），它会被当成文件路径，于是单书模式把整篇 markdown 写进
那个"文件"而不建目录。本脚本在每条 v4 命令前先 mkdir -p，把这个判断钉死成"目录"。

【长度预算按版本算】
v3 的产出路径里 <stem> 出现两次（OUT/<stem>/<method>/<stem>_*.md），v4 只出现一次
（OUT/<stem>.zip，或 --v4-format markdown 时的 OUT/<stem>.md）。同一本书在 v4 下产出路径
短得多——实测 122 字符主干的英文书在 v3 下
预测 288~290 字符（爆 MAX_PATH），在 v4 下完全放得下。所以长度预算是**按版本**算的，
否则会拿 v3 的尺子误杀 v4 能干的书。
名字合法性判据则两代共用一套（见下节）：保留设备名即使写成 CON.md 在 Win32 下依然打不开，
而且同一个书库不该有两套命名策略。

【skip 判据：两代都认，但 v4 一侧按 --format 认（v6.4 起）】
  已有 <out>/<stem>.zip       -> v4（--format zip）产出，跳过
  或 <out>/<stem>.md          -> v4（--format markdown）产出，跳过
  或 <out>/<stem>/ 内有 .md   -> v3 产出，跳过
换引擎重跑时不会白干；要强制重解析用 --no-skip。

⚠ v6.4 起，旧 flat 产出（<out>/<stem>.md）**不再满足** --format zip 的 skip 判据。
这不是疏忽，是必须的：zip 的全部价值就是那批元数据（tier / parse_mode / 页号 /
脚注结构 / 图片类型），把 flat 产出当"已有产出"就等于永远拿不到 zip —— 那正是
「改完 MinerU 管线重跑会被全部跳过」那个坑。代价是历史上按 flat 解析过的书会被
重新解析一遍（要吃 GPU），所以本轮计划行会把这些书**明确列出来**，不悄悄跑掉。
v3 的目录产出仍照收（换引擎不白干）。

=================================================================
v6.1 名字与路径长度策略（本次新增）
=================================================================
MinerU 的输出子目录名 = 输入文件名主干，而且内部产物名也复用主干：
  <out_root>/<stem>/hybrid_auto/<stem>_content_list_v2.json
于是 <stem> 在最终路径里出现两次。由此产生两类问题：

  1) 名字非法（NTFS/DrvFs 允许，Win32 不允许）：尾部点、尾部空格、首部空格、
     保留设备名（CON/PRN/AUX/NUL/CLOCK$/COM0-9/LPT0-9 及上标变体）、非法字符。
     例：国际关系史 第七卷（1945—1949）..pdf 的主干是 "…（1945—1949）."，
     尾点 -> 输出目录建得出来，但 Win32 API 会把尾部点归一化掉，
     所以 Windows 侧任何程序（资源管理器/编辑器/RAG 入库）都打不开它。
  2) 路径超长：MAX_PATH 260 字符上限（未开长路径支持时）。上面那个 <stem>
     出现两次的乘法结构，让长文件名特别容易顶爆预算。
     （实测：122 字符主干的英文书 -> 预测产出路径 288~290 字符，而 MinerU
      自带的截断只截到 200 UTF-8 字节，管不到 MAX_PATH。）

处理原则：只去不携带语义的部分，其余一律报给人（不搞启发式）。
  * 尾部点、首尾空白 -> 自动改名：源文件 + 同名旧输出目录成对改。
    只改源文件会留下孤儿目录，且下次重跑会重复解析，所以必须成对。
    旧输出目录名以点/空格结尾，Win32 打不开，故改名放在生成的 bash 里做
    （WSL 侧无此限制；实测 DrvFs 上 mv 一个尾点目录 rc=0）。
  * 超长 / 保留设备名 / 非法字符 / 清洗后撞名 -> 一个都不动，逐条打印原因后
    exit 1，交你（或 LLM）手工改名。加 --skip-bad 可跳过这些书先跑其余的，
    此时仍逐条打印被跳过的文件与原因。
  * 路径组件含单引号（Windows 其实允许）会让生成的 bash 脚本无法安全引用
    （本脚本用单引号包裹路径）-> 同样报失败并指出是哪个组件，请手工改名。
    注意引号也可能落在父目录上，那种情况改文件名没用。
  * 不做猜测性截断、不做字符替换：截断会砍掉恰好携带区分信息的尾部（年份、
    出版社、卷号常落在尾部）并且会引入撞名；字符替换则改变了你书库的命名。
    那是 LLM 的活，不是脚本的活。

名字合法性的权威判定用 pathvalidate（platform="windows"，依据 MS KB2829981）。
它管不到的两个坑由本脚本兜底：
  * pathvalidate 把 "." 和 ".." 判为合法（源码对该值显式 early-return），
    但作为输出目录名会变成 <out_root>/../hybrid_auto 这种路径穿越 -> 单独拦。
  * 它只管单个名字（255 字节），不管 MAX_PATH（路径级）-> 单独算。

判名字一律按【文档/契约】，不按【本机实测行为】：实测行为会随 Windows 版本和
卷类型漂移（NTVDM 没了 -> DOS 设备名半死但 NUL 还活着；8.3 短名在本机 NTFS 上
其实也不存在 -> 那条“经典救援通道”根本借不到…）。
完整实测矩阵与理由见 win_name_reason() 上方的注释块——有人（或 LLM）想放松
RESERVED_NAME 时，请先读那段。

=================================================================
v6.0 架构说明（为什么删掉 v4.0~v4.7 的全部 PTY/信号代码）
=================================================================
v4.x 的病根：Python 用 PIPE 捕获 mineru 输出 -> wsl.exe 检测 stdout 是管道
就不分配 PTY -> mineru 认为自己是非 TTY -> tqdm 整体禁用、颜色丢失
（MinerU 内部按 isatty 决定）-> 于是引入 `script -qefc` 伪造 PTY -> PTY
又切断/扰乱信号路径 -> 再补 PID 文件 + kill -INT -PGID 三级链路 + QuickEdit
禁用 + msvcrt 键盘轮询……全部是"自造问题的连锁反应"。

v6.0 的做法：Python 只做 发现 -> 分流 -> 生成 .sh 脚本，然后退出；
bash 脚本由 wsl.exe 直接执行（stdout 继承 Windows 控制台，绝不 PIPE）：
  - wsl.exe 检测 stdout 是控制台 -> 自动分配 PTY -> mineru 看到真 TTY
    -> 彩色 / tqdm 进度条 / 擦行 原生保留，零中间层。
  - Ctrl+C：conhost 把 CTRL_C_EVENT 发给整个前台组（wsl.exe + bash +
    mineru 同组），wsl 转发为 WSL 内 SIGINT（microsoft/WSL#7300 实证），
    mineru 收到 SIGINT 自行退出，bash 脚本随之终止——原生信号链。
  - 控制台模式只做一件必要的事：输出模式若尚未置位 VIRTUAL_TERMINAL_PROCESSING
    就 OR 进去，**已置位（如 Windows Terminal）则一个字都不写**。
    v6.1 起连 ENABLE_QUICK_EDIT_MODE 也不再动——那是 v4.x 信号链断裂时代的补丁，
    实测裸 cmd 下 wsl 的彩色与 Ctrl+C 都正常，没有理由改用户的输入模式。
  - 每本书一个独立 mineru 进程：退出后 OS 回收全部内存（规避 MinerU
    ModelSingleton 内存泄漏，Issue #4243）。v6.2 起这是 v4 的缺省行为
    （--v4-recycle book）；可用 --v4-recycle batch 换回上游的「整批一进程」。
    代价与语义差异见上面 v6.2 一节。
  - 单文件超时不做（--timeout 已删）：超大文件场景由用户外部挂 timeout，
    脚本内不设限，避免误杀长任务。

执行链（Windows 侧，wsl.exe 三步）：
  1) wsl bash -c "cat > /tmp/mineru_batch_<pid>.sh"   <- stdin 灌入脚本文本
     （Windows Python 写不了 WSL 的 /tmp，故借 wsl 的 stdin 管道落盘）
  2) wsl bash -lc "chmod +x /tmp/mineru_batch_<pid>.sh"
  3) wsl bash -l /tmp/mineru_batch_<pid>.sh           <- 执行（无 -c！
     直接以脚本文件为参数，避免 -lc "bash xxx.sh" 字符串嵌套经 wsl.exe
     传参链被重写——v4.4 的引号破坏教训。login shell 读 /etc/profile。）
  Python 等待并透传退出码（subprocess 不捕获 stdout，继承控制台）。
  ※ 若实测 Ctrl+C 时 Python 与 wsl 抢信号导致 wsl 变孤儿：切换为
    "Python 启动 wsl 后立即 os._exit(0)"（CreateProcess 后跑路）——
    改 launch() 内 WINDOWS 分支注释标记的 [PLAN-B] 处即可（一处）。

Linux 侧：直接写 /tmp/xxx.sh -> bash -l /tmp/xxx.sh（同构，无 wsl 层）。

临时脚本保留在 /tmp（可复查、可手动重跑）；/tmp 系统重启自动清理。
"""

# ══════════════════════════════════════════════════════════════════════════════
#                         ★★★  使 用 前 请 先 改 下 面 这 一 处  ★★★
#
#   ENV_ACTIVATE_CMD —— 激活 MinerU 所在环境的命令。
#
#   本脚本自身不装任何东西，它只是**生成一段 bash 脚本**去批量调用 MinerU。
#   那段 bash 的开头会原样插入下面这一行，所以请换成本机上真正能用的写法：
#
#     WSL / Linux（venv）     'source "$HOME/venv-mineru-v4/bin/activate"'
#     Linux / macOS（conda）  'source "$(conda info --base)/etc/profile.d/conda.sh" && conda activate mineru'
#     Windows（Git Bash）     'source "/c/Users/你/venv-mineru-v4/Scripts/activate"'
#
#   三条硬要求（前两条脚本会替你检查，不满足就**当场报错**，不会静默跑错东西）：
#     1) 能在 `bash -l`（登录 shell）里执行；
#     2) 执行后 `mineru`（v3）或 `mineru-kit`（v4）出现在 PATH 上 ——
#        自动判代正是靠这一点去问你配置的这个环境；
#     3) 不要写 `cd`：工作目录由生成的脚本自己管。
#
#   留空（'' ）= 不插入任何激活命令，直接用当前环境。
#   适合：MinerU 已装在系统 PATH，或你本来就打算自己先 source 好再跑本脚本。
#
#   为什么判代探测也走这条命令（而不是去认一个写死的 venv 路径）：
#   激活命令是**你可以改的**，若探测还盯着某个硬编码路径，两者就会"说两套话"
#   —— 明明会激活 v4，却报"没找到 v4"。探测与激活共用同一个来源 ⟹ 不可能漂移。
# ══════════════════════════════════════════════════════════════════════════════
ENV_ACTIVATE_CMD = 'source "$HOME/venv-mineru-v4/bin/activate"'

# 同上，但**只在 `--mineru-version 3` 时**用它（v3 与 v4 通常装在不同环境里）。
# 你只用 v4 的话，这一行不用管；完全不用 v3 就留空 ''。
ENV_ACTIVATE_CMD_V3 = 'source "$HOME/venv-mineru-v3/bin/activate"'

# ══════════════════════════════════════════════════════════════════════════════
#  以下都是可用的缺省值，一般不用改。
# ══════════════════════════════════════════════════════════════════════════════

import os
import re
import sys
import atexit
import subprocess
import argparse
from collections import namedtuple
from pathlib import Path

try:
    import natsort
except ImportError:
    natsort = None

# 名字合法性判定用 pathvalidate（platform="windows"）——其依据是 MS KB2829981
# 与 "Naming Files, Paths, and Namespaces"。缺它就直接失败，不静默降级。
try:
    from pathvalidate import (
        ErrorReason,
        ValidationError,
        is_valid_filename,
        validate_filename,
    )
except ImportError:
    sys.stderr.write(
        "X 需要 pathvalidate：pip install pathvalidate\n"
        "  （WSL 侧: source ~/venv-mineru-v3/bin/activate 后 pip install pathvalidate）\n"
    )
    sys.exit(1)


IS_WINDOWS = sys.platform == "win32"
# ---- 格式矩阵（v6.3：按版本分流；4.0 新收了一批扩展名）----
# 图片：默认**不扫**（目录递归时不收集）。只有显式点名文件（命令行参数或 @列表）
# 或加 --images 才收。理由实测：<corpus-root> 下有 142,709 个 jpg，全是 v3 产出里的
# images/<sha256>.jpg —— 递归扫进去就是 14 万个 sha256 命名的垃圾任务。
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".jp2"}

# 需要模型的那批（= mineru 4.0.7 filetypes.TIERED_PARSE_EXTENSIONS）。
# 两个用途：① 只有这些能收 v4 的 --tier；② 排任务顺序时它们最慢（走 VLM），放最后。
TIERED_EXTS = {".pdf"} | IMAGE_EXTS

# v3（mineru 3.4.0）的 --help 明说支持：pdf / image / docx / pptx / xlsx
V3_MINERU_EXTS = {".pdf", ".xlsx", ".pptx"} | IMAGE_EXTS
# v4（mineru-kit parse）收 mineru/filetypes.PARSEABLE_EXTENSIONS，除掉 docx（见下）
V4_MINERU_EXTS = ({".pdf"} | IMAGE_EXTS | {
    ".doc", ".ppt", ".pptx", ".xls", ".xlsx", ".rtf",
    ".odt", ".ods", ".odp", ".ofd",
    ".html", ".htm", ".shtml", ".mhtml", ".mht",
    ".csv", ".tsv", ".epub",
})

# pandoc 只留 docx：文本类文档它比 DocVortex/VLM 快得多、不占 GPU，产出也干净。
# 两代的差异：epub 在 v3 没有原生支持（只能 pandoc），在 v4 有，故 v4 交回 mineru。
V3_PANDOC_EXTS = {".epub", ".docx"}
V4_PANDOC_EXTS = {".docx"}

EXTS_BY_VERSION = {"3": (V3_MINERU_EXTS, V3_PANDOC_EXTS),
                   "4": (V4_MINERU_EXTS, V4_PANDOC_EXTS)}

# MinerU v3 的产出目录名：<out>/<stem>/<method>/（method 见 v6.1 的 MINERU_SUBDIR_MAX
# 注释；images/ 是它内部的图片目录）。目录递归时默认剪掉这些子树：否则 -r 扫书库会
# 把既有产出里的 <stem>_origin.pdf 当成新任务反复解析。-o 或 --include-mineru-out
# 可以强制扫。
MINERU_OUTPUT_DIRS = {"auto", "txt", "ocr",
                      "hybrid_auto", "hybrid_txt", "hybrid_ocr", "images"}


def mineru_exts(version):
    """该版本交给 mineru/mineru-kit 的扩展名集合。"""
    return EXTS_BY_VERSION[version][0]


def pandoc_exts(version):
    """该版本交给 pandoc 的扩展名集合。"""
    return EXTS_BY_VERSION[version][1]


def supported_exts(version):
    """该版本能处理的全部扩展名。"""
    return mineru_exts(version) | pandoc_exts(version)
MINERU_MAX_CONCURRENT = 1
MINERU_WINDOW_SIZE = 48  # 非pipeline后端内存控制（官方:内存≈并发×窗口×页成本；本机23.5G用48×1）
# ---- MinerU 版本（v6.2 双适配）----
# v3 CLI: mineru -p SRC -o OUT --backend B       -> OUT/<stem>/<method>/<stem>_*.md
# v4 CLI: mineru-kit parse SRC -o OUT --tier T --format F
#         F=zip      -> OUT/<stem>.zip（markdown.md + 三个 json + images/；v6.4 起缺省）
#         F=markdown -> OUT/<stem>.md（扁平单文件，图片 base64 内联）
MINERU_VERSIONS = ("3", "4")
VENV_OVERRIDE = os.environ.get("MINERU_WSL_VENV")  # 覆盖环境（缺省用顶部的两个字符串）
# 判代的入口名：自动探测靠 `command -v` 问环境，而不是去看某个 venv 目录里有没有文件
# （激活命令是可改的，盯着写死的路径会与真正被激活的环境说两套话）。
MINERU_V4_ENTRY = "mineru-kit"   # v4 独有；v3 没有这个入口
MINERU_V3_ENTRY = "mineru"       # 两代都有，故只在没有 v4 时才算 v3

DEFAULT_MINERU_VERSION = os.environ.get("MINERU_VERSION", "auto")     # auto|3|4
DEFAULT_BACKEND = os.environ.get("MINERU_BACKEND", "hybrid-engine")   # 仅 v3
DEFAULT_TIER = os.environ.get("MINERU_TIER", "standard")              # 仅 v4
DEFAULT_LANG = os.environ.get("MINERU_LANG")  # None=不指定（MinerU 自动检测）
V4_RECYCLE_CHOICES = ("book", "batch")
DEFAULT_V4_RECYCLE = "book"   # 每书一进程：退出即整块回收内存（规避 upstream #4243）
# 产出序列化。[仅 v4] 默认 zip —— 扁平 .md 把 tier / parse_mode / 页号 / 脚注结构 /
# 图片类型全丢了，而 zip 与它**同一次 parse、只换序列化**。markdown 留给
# 「只要一个单文件、不在乎元数据」的场景。
V4_FORMAT_CHOICES = ("zip", "markdown")
DEFAULT_V4_FORMAT = os.environ.get("MINERU_V4_FORMAT", "zip")


def _v4_out_ext(v4_format):
    """--v4-format 取值 -> 产出文件后缀。**唯一映射点**，别在别处再写一份。

    取值名（zip|markdown）与后缀（.zip|.md）故意不同名：--format 的值是"序列化形态"，
    磁盘上的后缀只是它的投影。两处各写一份映射，日后加取值必漂移。
    """
    return ".zip" if v4_format == "zip" else ".md"

# 已拆卷文件名模式（<stem>_卷NN_pAAAA-BBBB.pdf）：避免对卷文件再拆
RE_SPLIT_VOL = re.compile(r"_卷\d+_p\d+-\d+\.pdf$")


def volume_plan(page_count, limit):
    """返回 [(卷序号, 起始页0基, 结束页0基), ...]；空列表表示不用拆。

    与 volume_name() 一起构成拆卷命名的**唯一来源**——预演与实拆共用，
    免得两处各写一份命名规则，日后改一处忘一处。
    """
    if limit <= 0 or page_count <= limit:
        return []
    return [(i, start, min(start + limit, page_count))
            for i, start in enumerate(range(0, page_count, limit), 1)]


def volume_name(base_stem, i, start, end):
    """<stem>_卷NN_pAAAA-BBBB.pdf（页码 1 基，与 scripts/split_pdf.py 一致）。"""
    return f"{base_stem}_卷{i:02d}_p{start + 1:04d}-{end:04d}.pdf"

# ---- Win32 名字与路径长度（v6.1；v6.2 起长度模型按版本分流）----
# v3 的输出子目录名 = 输入文件名主干，内部产物名也复用主干：
#   <out_root>/<stem>/<parse_method>/<stem>_content_list_v2.json
# 故 <stem> 在最终路径里出现两次 —— 长度预算就紧在这里。
# v4 是扁平单文件 <out_root>/<stem>.md，<stem> 只出现一次，预算宽松得多。
# 两代共用一个 max_path 上限，但喂给预算函数的模板不同（见 _output_patterns）。
MINERU_SUBDIR_MAX = "hybrid_auto"                 # [v3] 最长形态（hybrid_{auto,txt,ocr}；pipeline 为 auto）
MINERU_LONGEST_SUFFIX = "_content_list_v2.json"   # [v3] <stem>_content_list_v2.json
SHA256_HEX_LEN = 64                               # 图像名 images/<sha256>.jpg
DEFAULT_MAX_PATH = 260                            # Win32 MAX_PATH（未开长路径支持时）
PATH_SAFETY = 10                                  # 安全余量
PANDOC_MEDIA_RESERVE = 60                         # pandoc --extract-media 内文件名不可预知，保守预留
STEM_LEN_CAP = 512                                # stem 长度预算的搜索上界（远超 255 字节上限）

RE_PV_INVALIDS = re.compile(r"invalids=\((.*?)\)")

# jobs 元素：用具名字段，替代位置索引 j[3]
Job = namedtuple("Job", ["out_root_wsl", "src_wsl", "stem", "kind"])

# 单个文件的体检结果：status in {OK, FIXABLE, FATAL}
Rec = namedtuple("Rec", ["path", "status", "stem", "new_name", "reason", "detail"])

# 生成 bash 脚本所需的全部旋钮（替代原来的一长串位置参数——加一个版本维度后
# 位置参数会多到读不懂调用点）
ScriptOpts = namedtuple("ScriptOpts", [
    "version",      # "3" | "4"
    "backend",      # [v3] --backend
    "tier",         # [v4] --tier
    "lang",         # 语言（v3 走 --lang；v4 走 MINERU_LANG）
    "activate_cmd", # 插进 bash 开头的那条环境激活命令（顶部 ENV_ACTIVATE_CMD）
    "extra_args",   # 透传参数 list 或 None
    "no_skip",      # True=不生成 skip 判断
    "recycle",      # [v4] "book" | "batch"
    "v4_format",    # [v4] "zip" | "markdown"（产出序列化）
])


def eprint(*a, **kw):
    sep = kw.pop("sep", " ")
    end = kw.pop("end", "\n")
    text = sep.join(str(x) for x in a) + end
    sys.stderr.buffer.write(text.encode("utf-8", "replace"))
    sys.stderr.buffer.flush()


def out(text="", end="\n"):
    sys.stdout.buffer.write((text + end).encode("utf-8", "replace"))
    sys.stdout.buffer.flush()


# 输出模式原值备份：None = 从未改动过（已置位就不改）；否则为原值供 atexit 恢复。
# 只记录"真被改动过"的，所以 restore 天然幂等，也不会去动没碰过的输入模式。
_CONSOLE_MODE_BACKUP = None


def enable_console():
    """Windows 控制台配置——只做一件必要的事，且只在必要时做。

    输出模式：ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004) 若**尚未置位**就 OR 进去
    （mineru 的 ANSI 走 wsl 继承的 CONOUT$ 直写路径时靠它渲染；若走 ConPTY 则内建
    解析、置位无害）。**已经置位就一个字都不写**——例如 Windows Terminal 默认就开着
    VT，那就原样不动：既幂等，也少一次对用户终端状态的扰动。**绝不整值覆盖**，否则
    会清掉用户原有的其他输出标志。只备份真被我们改动过的原值，atexit 恢复。

    v6.1 起不再做的事（都是 v4.x 的残留）：
    * 不再清 ENABLE_QUICK_EDIT_MODE (0x0040)，不再碰输入句柄，不再键盘轮询。
      那一套是 v4.x「PIPE 捕获 -> wsl 拿不到 PTY -> 信号链断」时代的补丁；
      v6.0 起 stdout 直接继承控制台、wsl 原生分配 PTY，**实测裸 cmd 下彩色与
      Ctrl+C 都正常**，所以没有任何理由再去改用户的输入模式。
    """
    global _CONSOLE_MODE_BACKUP
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h_out = k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h_out, ctypes.byref(mode)):
            return                     # 没有真控制台（stdout 被重定向等）-> 不动
        if mode.value & 0x0004:
            return                     # 已有 VT 位 -> 保持不变，一个字都不写
        _CONSOLE_MODE_BACKUP = mode.value
        k32.SetConsoleMode(h_out, mode.value | 0x0004)
    except Exception:
        pass


def restore_console():
    """恢复被我们改动过的控制台模式（atexit 注册；仅强杀进程无法恢复）。

    只恢复**真的动过**的项：没动过就没有备份，也就无需恢复（天然幂等）。
    """
    if not IS_WINDOWS or _CONSOLE_MODE_BACKUP is None:
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetConsoleMode(k32.GetStdHandle(-11), _CONSOLE_MODE_BACKUP)
    except Exception:
        pass


# ================================================================
# 路径
# ================================================================

def looks_like_wsl_path(p):
    """Windows 侧判断：/mnt/...、/home/... 等 WSL 原生路径（排除 // 开头）。"""
    s = str(p).replace("\\", "/")
    return s.startswith("/") and not s.startswith("//")


def windows_to_wsl(path):
    """Windows 绝对路径 -> WSL /mnt/x/...；WSL 原生路径原样返回。

    注意：Windows 上 Path('/mnt/<drive>/x') 的 str() 是反斜杠形式 '\\mnt\\e\\x'
    （WindowsPath 按 os.sep 规范化），故 looks_like_wsl_path 命中后必须
    统一转正斜杠再返回——否则反斜杠路径会污染 worker 侧 bash 脚本。"""
    s = str(path)
    if looks_like_wsl_path(s):
        return s.replace("\\", "/")
    if s.startswith("\\\\wsl$\\") or s.startswith("//wsl$/"):
        parts = s.replace("\\", "/").split("/")
        try:
            idx = parts.index("wsl$")
            return "/" + "/".join(parts[idx + 2:])
        except (ValueError, IndexError):
            pass
    if len(s) >= 2 and s[1] == ":":
        drive = s[0].lower()
        rest = s[2:].replace("\\", "/")
        while "//" in rest:
            rest = rest.replace("//", "/")
        return f"/mnt/{drive}{rest}"
    return s.replace("\\", "/") if "/" in s else s


def to_worker_path(p):
    """Path -> worker 侧绝对路径（Windows 转 /mnt/...，Linux 原样）。"""
    if IS_WINDOWS:
        return windows_to_wsl(str(p))
    return str(p)


# ================================================================
# Win32 名字合法性与路径长度（v6.1）
# ================================================================
#
# 病根：MinerU 的输出子目录名 = 输入文件名主干，且内部产物名也复用主干
#   <out_root>/<stem>/<parse_method>/<stem>_content_list_v2.json
# 证据：mineru/cli/common.py 的 prepare_env() = os.path.join(output_dir,
#       pdf_file_name, parse_method)；pdf_file_name 取自 cli/client.py 的
#       path.stem（未 resolve，故取自传入路径的字面名）；CLI 无覆盖选项。
# 于是 <stem> 在最终路径里出现两次，引发两类病：
#   1) 名字非法：NTFS/DrvFs 允许而 Win32 不允许（尾部点、尾部空格、首部空格、
#      保留设备名、非法字符）。危害是 Win32 API 会把尾部点/空格归一化掉，
#      于是 Windows 侧任何程序（资源管理器/编辑器/RAG 入库）都打不开该目录。
#   2) 路径超长：MAX_PATH 260 字符上限（未开长路径支持时）。
#
# 处理策略（不搞启发式——猜测性改名/截断是 LLM 的活，不是脚本的活）：
#   * 只去「尾部点、首尾空白」这类不携带语义的部分 -> 自动改名。
#     源文件与同名旧输出目录成对改名（只改源文件会留孤儿目录 + 下次重复解析）。
#   * 其余（超长 / 保留设备名 / 非法字符）-> 一律不动，逐条打印原因后 exit 1，
#     交用户手工改名；--skip-bad 可跳过这些书先跑其余的（仍逐条报告原因）。
#
# 名字合法性权威判定交给 pathvalidate。两个它管不到的坑由本脚本兜底：
#   * pathvalidate 把 "." 和 ".." 判为合法（源码对该值显式 early-return），
#     但作为输出目录名会变成 <out_root>/../hybrid_auto 这种路径穿越 -> 单独拦。
#   * 它只管单个名字（255 字节），不管 MAX_PATH（路径级）-> 单独算。
#
# 不去做「把长名截断/替换字符」的理由：截断会砍掉恰好携带区分信息的尾部
# （年份、出版社、卷号常落在尾部），且截断后还会引入撞名；字符替换则改变了
# 用户书库的命名。两者都是在替用户猜语义——交给用户/LLM 更合适。

def split_name(name):
    """按 pathlib 的规则拆 (stem, suffix)，但**不做盘符/根目录解释**。

    不能用 PureWindowsPath：它把 "A:B.pdf" 里的 "A:" 当盘符，于是 stem 变成
    "B"、suffix 变成 ".pdf"——等于静默改名（实测踩到）。而 MinerU 在 WSL 侧用
    PosixPath 取 stem，故这里也按 PosixPath 规则手工复刻，最忠实。
    """
    i = name.rfind(".")
    if 0 < i < len(name) - 1:
        return name[:i], name[i:]
    return name, ""


def fix_filename(name):
    """只做「去整名首尾空白 + 去主干尾部点/空格」的机械修复。

    不替换任何字符、不截断。返回 (new_name, new_stem)；
    new_name 为空表示修完不可用（交给 FATAL 分支）。
    尾部点/空格必然落在主干上（<stem>.pdf 这类组件以扩展名结尾），故只修 stem。
    """
    n = name.strip()
    stem, suffix = split_name(n)
    stem = stem.strip().rstrip(" .")
    if stem in (".", ".."):
        return "", ""
    return stem + suffix, stem


def win_name_ok(name):
    """Win32 单个名字是否合法（pathvalidate 权威判定）。"""
    return is_valid_filename(name, platform="windows")


# ---------------------------------------------------------------------------
# 判名字的硬规矩：按【文档/契约】，不按【当前这台机器实测的行为】
# ---------------------------------------------------------------------------
# 起因：实测（Win11 x64 + 9p 卷，cmd.exe 操作 + POSIX 侧核对）发现当前 Windows
# 对保留设备名几乎全放行了，与 MS 文档、与 XP 时代行为都不符：
#
#   CON PRN AUX COM0 COM1 LPT1 LPT9  -> rc=0，真目录建成了，还能往里写文件
#   NUL                              -> md 报 rc=0 但静默什么都没建；
#                                       echo X > ...\NUL 数据进黑洞、文件不生成
#   abc.....（尾点）                  -> rc=0 但被静默改写成 abc
#   （CLOCK$ 未测：我误测成了 CLOCK，而 CLOCK 本来就不是保留名——别引用它）
#
# 用 \\?\ 前缀一测，两条规则当场分家：
#   md      R:\...\NUL        -> 静默失败        （设备名映射生效）
#   md \\?\R:\...\NUL        -> NUL 目录建成     （映射被绕过）
#   md      R:\...\abc.....   -> 建出 abc        （归一化生效）
#   md \\?\R:\...\abc.....   -> 建出 abc.....   （归一化被绕过）
# 即：「尾点/尾空格归一化」住在 RtlDosPathNameToNtPathName，**每次调用都过**；
#     「设备名映射」是 Win32 另一套只在对得上的形态上才触发的特殊匹配。
#
# 为什么会漂移：规则不是被“废除”的，是**随依赖它的代码一起死掉的**——
#   * XP 有 NTVDM（真能跑 16 位），DOS 的 INT 21h 文件 API 是活契约
#     -> 设备名映射必须生效，于是当年那个“autorun.inf 免疫目录”技巧成立
#   * Win11 x64 没有 NTVDM，DOS 层没人跑了 -> 只剩还有活代码依赖的映射：
#       NUL 活着（> NUL 写在全人类的批处理里，cmd 与 WOW64 下的 32 位程序都在用）
#       CON/LPT1 半死（没人再往并口/串口设备名写数据，物理端口都没了）
#   * 尾点归一化与 MAX_PATH 永远活着：它们服务的是 WOW64 下仍在跑的 32 位程序，
#     那些程序天天传脏路径，CreateFile("...\foo ") 必须继续被修好
#   一句话：WOW64 就是新的 NTVDM。DOS 那半截成了疤，Win32 那半截还在续命。
#   （因果是推的，矩阵是实测的——两者别混。）
#
# 所以：**别因为“我在本机实测 CON 能用”就把 RESERVED_NAME 降级。** 名字是长寿
# 数据——今天在 Win11 上建好的 CON.pdf，明天会被复制到 FAT32 U 盘、XP 老机器
# （NTVDM 活着）、NAS（Linux），而名字里没法记录“我是在哪个 build 上合法的”。
# 保守拦下的代价只是“极罕见的 CON.xxx 书请手工改名”，比让一个换个盘就炸的名字
# 进入书库便宜得多。确要放行，必须加显式开关，并把上面的代价写进开关的 help。
#
# 另：关于“用 8.3 短名绕过”这条经典通道——**本机实测它不可用，别拿它当方案**：
#   * R:（9p 内存盘）：fsutil 报“整卷禁用 8dot3”，短名列空
#   * F:（真 NTFS）：fsutil 报“已启用”，但
#       for %I in (F:\书籍\历史\*) do @echo %~sI
#     对现存每个书文件返回的短路径**与长路径完全相同、一个 ~N 标记都没有**
#     -> 这些文件根本没有短名。很可能它们是 WSL/DrvFs 侧创建的，而那条创建路径
#        不触发短名生成；也可能生成时该卷禁用、之后才启用。两种都未证实。
#   * 正对照（证明判据本身有效）：dir /x C:\Windows 能列出 CONTAI~1 / MICROS~1 等
#   结论：短名是「每卷配置 × 每个文件的创建时机」共同决定的，不是 NTFS 的固有属性。
#   （教训：我先在 9p 上测一次就写下“这条通道只在 NTFS 上有”，随即在真 NTFS 上被
#     自己的实测推翻。这正是本段开头那条原则要防的事——而且判据要配正对照：
#     没有正对照时，“没找到”和“工具看不见”长得一模一样。）
#   顺带：9p 卷上这轮也确认了 Win32 层结论可靠（9p 服务端是 Windows 进程）；
#   但 NTFS 驱动层的其他行为（比如设备名在 FSD 侧的处理）本轮仍未走到。
# ---------------------------------------------------------------------------
def win_name_reason(name):
    """把 pathvalidate 的报错翻译成中文原因。"""
    try:
        validate_filename(name, platform="windows")
        return "未知原因（pathvalidate 未报错）"
    except ValidationError as e:
        reason = getattr(e, "reason", None)
        if reason is ErrorReason.RESERVED_NAME:
            rn = getattr(e, "reserved_name", "") or name
            return ("Windows 保留设备名 %r"
                    "（CON/PRN/AUX/NUL/CLOCK$/COM0-9/LPT0-9 及上标变体）" % rn)
        if reason is ErrorReason.INVALID_LENGTH:
            return "名字过长（%s 字节 > 255 字节上限）" % getattr(e, "byte_count", "?")
        if reason is ErrorReason.INVALID_CHARACTER:
            m = RE_PV_INVALIDS.search(str(e))
            return "含 Win32 非法字符 %s" % (m.group(1) if m else "?")
        return "%s" % (reason,)


def analyze_name(name):
    """判定一个文件名（其主干将成为输出目录名，故两者都要合法）。

    返回 (status, new_name, new_stem, reason)：
      OK      -> 无需改动
      FIXABLE -> 只去尾部点/首尾空白即可合法
      FATAL   -> 必须人工改名（脚本不做任何猜测性改动）
    """
    stem, _suffix = split_name(name)
    if stem in (".", ".."):
        return ("FATAL", name, stem,
                "主干是 %r：作为输出目录名会变成 <out_root>/%s/... 指向目录自身或"
                "其父目录（路径穿越）" % (stem, stem))
    if win_name_ok(name) and win_name_ok(stem):
        return "OK", name, stem, ""
    new_name, new_stem = fix_filename(name)
    if new_name and win_name_ok(new_name) and win_name_ok(new_stem):
        return "FIXABLE", new_name, new_stem, "尾部点/首尾空白"
    if not new_name:
        return "FATAL", name, stem, "去掉尾部点/空格后名字为空或只剩点"
    for label, val in (("文件名", name), ("输出目录名", stem)):
        if not win_name_ok(val):
            return "FATAL", name, stem, "%s %r：%s" % (label, val, win_name_reason(val))
    return "FATAL", name, stem, "未知原因"


def u16_len(s):
    """Win32 MAX_PATH 计的是 UTF-16 code unit 数，不是 Python 字符数。"""
    return len(s.encode("utf-16-le")) // 2


RE_WSL_DRV = re.compile(r"^/mnt/([A-Za-z])(/.*)?$")


def to_win_form(worker_path):
    """worker 侧路径 -> 对应的 Win32 形式（正斜杠，长度与反斜杠等价）。

    返回 None 表示「不是 DrvFs 路径」，即 MAX_PATH 根本不适用（例如 WSL 原生
    ext4 上的书）。这点很关键：MAX_PATH 是 Windows 侧约束，对 /mnt/<盘>/...
    才有意义（/mnt/f 对应 F:，长度差 4），对 ext4 路径硬套 260 是错的。
    """
    p = str(worker_path).replace("\\", "/")
    m = RE_WSL_DRV.match(p)
    if m:
        return m.group(1).upper() + ":" + (m.group(2) or "")
    if len(p) >= 2 and p[1] == ":":
        return p
    return None


_STEM = object()   # 路径模板里的 stem 占位（用哨兵对象而非字符串替换，避免误伤含 "{S}" 的真名）


def _output_patterns(kind, version, v4_format=DEFAULT_V4_FORMAT):
    """产出路径模板（相对 out_root）的分段形式，_STEM 处填主干。

    逐条取自 MinerU / pandoc 实际写盘位置：
      [v3/mineru]  <out>/<stem>/<pm>/<stem><longest_suffix>
                   <out>/<stem>/<pm>/images/<sha256>.jpg
      [v4/mineru]  <out>/<stem>.zip     （--format zip；实测，见 v6.4 一节）
                   或 <out>/<stem>.md  （--format markdown）
                   ⚠ 两种形态 <stem> 都只出现一次；zip 把图片与侧车都装进同一个归档，
                     磁盘上不另落 images/ 目录 ⟹ 模板仍只有一条。
      [两代/pandoc] <out>/<stem>/<stem>.md
                   <out>/<stem>/media/<不可预知名>
    """
    if kind in mineru_exts(version):
        if version == "4":
            return [["/", _STEM, _v4_out_ext(v4_format)]]
        return [
            ["/", _STEM, "/", MINERU_SUBDIR_MAX, "/", _STEM, MINERU_LONGEST_SUFFIX],
            ["/", _STEM, "/", MINERU_SUBDIR_MAX, "/images/",
             "0" * SHA256_HEX_LEN + ".jpg"],
        ]
    return [
        ["/", _STEM, "/", _STEM, ".md"],
        ["/", _STEM, "/media/", "0" * PANDOC_MEDIA_RESERVE],
    ]


def _render(parts, stem):
    return "".join(stem if p is _STEM else p for p in parts)


def predicted_output_len(out_root_win, stem, kind, version,
                         v4_format=DEFAULT_V4_FORMAT):
    """预测该任务产出的最长路径长度（UTF-16 unit，out_root 用 Win32 形式）。

    真实拼接后测量，不手算分隔符——避免算术错误。
    """
    return max(u16_len(out_root_win + _render(p, stem))
               for p in _output_patterns(kind, version, v4_format))


def stem_budget(out_root_win, kind, max_path, version,
                v4_format=DEFAULT_V4_FORMAT):
    """反推 stem 允许的最大长度（UTF-16 unit），使预测路径 <= max_path - 余量。

    注意 out_root_win 必须是 Win32 形式（见 to_win_form）：Win32 的 F:\\x
    比 WSL 的 /mnt/f/x 短 4 个字符，用 WSL 形式量会把预算算小。

    二分搜索而非手推公式：预测长度对 stem 长度单调，二分不会算错。
    """
    limit = max_path - PATH_SAFETY
    if predicted_output_len(out_root_win, "", kind, version, v4_format) > limit:
        return 0
    lo, hi = 0, STEM_LEN_CAP
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if predicted_output_len(out_root_win, "x" * mid, kind, version,
                                v4_format) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return lo


# ================================================================
# MinerU 版本解析（v6.2）
# ================================================================

def _tilde_to_home(p):
    """把前导 ~ 换成 $HOME —— bash 双引号内 ~ 不展开，$HOME 会展开。"""
    return "$HOME" + p[1:] if p.startswith("~") else p


class EngineProbeError(RuntimeError):
    """auto 判代失败：配置的激活命令跑不通，或环境里没有 MinerU 的可执行入口。"""


_PROBE_MARK = "__MINERU_ENGINE__"


def _worker_bash(script, timeout=90):
    """在 worker 侧跑一段 bash（Windows 走 wsl，其余直接 bash）。

    返回 CompletedProcess；连 bash/wsl 都起不来则返回 None。
    ⚠ stdout/stderr 收 bytes 再自行以 UTF-8 + replace 解码：worker 的 locale
    不保证是 UTF-8，用 text=True 会让一次成功探测抛出与真实故障无关的
    UnicodeDecodeError。
    """
    cmd = (["wsl", "bash", "-lc", script] if IS_WINDOWS
           else ["bash", "-lc", script])
    try:
        return subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _decode(b):
    return (b or b"").decode("utf-8", "replace")


def _probe_once(activate_cmd):
    """跑一次探测，返回 val ∈ {'4','3','ACTIVATE_FAILED','NONE',''} 与诊断文本。"""
    act = (activate_cmd or "").strip() or ":"
    # { act ; } 用花括号包住，好让 `a && b` 这种激活命令被当成一个整体；
    # 激活阶段的噪音丢进 /dev/null，只让下面几个 echo 上到 stdout。
    script = (
        "{ " + act + " ; } >/dev/null 2>&1 || "
        "{ echo '" + _PROBE_MARK + "=ACTIVATE_FAILED'; exit 0; }\n"
        "if command -v " + MINERU_V4_ENTRY + " >/dev/null 2>&1; then "
        "echo '" + _PROBE_MARK + "=4'\n"
        "elif command -v " + MINERU_V3_ENTRY + " >/dev/null 2>&1; then "
        "echo '" + _PROBE_MARK + "=3'\n"
        "else echo '" + _PROBE_MARK + "=NONE'; fi\n"
    )
    r = _worker_bash(script)
    if r is None:
        return "", "无法在 worker 侧执行 bash（Windows 需要 wsl；或 bash 不可用）"
    val = ""
    for line in _decode(r.stdout).splitlines():
        line = line.strip()
        if line.startswith(_PROBE_MARK + "="):
            val = line.split("=", 1)[1].strip()
    if not val and r.returncode != 0:
        tail = [x for x in _decode(r.stderr).strip().splitlines() if x.strip()]
        return "", ("探测用的 bash 退出码 %d%s"
                    % (r.returncode, ("：" + tail[-1]) if tail else ""))
    return val, ""


def probe_engine(activate_cmd):
    """用**配置的那条激活命令**去问环境：装的是哪一代？

    返回 (major, why)。major ∈ {"3","4",None}；None = 问不出来。

    ⚠ 「激活命令跑不通」与「激活成功了但环境里没有入口」是两回事，必须分开报 ——
    混成一句话会让人去查错的地方（去改 PATH，而问题在激活命令本身）。
    """
    val, diag = _probe_once(activate_cmd)
    if val == "4":
        return "4", "环境里有 %s" % MINERU_V4_ENTRY
    if val == "3":
        return "3", "环境里只有 %s（没有 %s）" % (MINERU_V3_ENTRY, MINERU_V4_ENTRY)
    if val == "ACTIVATE_FAILED":
        return None, "激活命令执行失败（环境没被激活）"
    if val == "NONE":
        return None, ("激活成功，但 PATH 上既没有 %s 也没有 %s（MinerU 装在别处？）"
                      % (MINERU_V4_ENTRY, MINERU_V3_ENTRY))
    return None, (diag or "探测没有返回可识别的结果")


def resolve_engine(version_arg, venv_arg):
    """决定用哪一代 MinerU、以及要插入哪条激活命令。返回 (major, activate_cmd, why)。

    · `--mineru-version 3|4`  → 不探测，直接用顶部对应的那条激活命令
    · `auto`（缺省）          → 用 ENV_ACTIVATE_CMD 去问环境（见 probe_engine）
    · `--venv X` / `MINERU_WSL_VENV` → 整体覆盖：激活命令改成 source "X/bin/activate"，
      探测也走它（"我指定了这个环境"就该以它为准）

    auto 为什么优先 v4：v4 已转正，且产出路径更短（不受 v3 那套 <stem> 出现两次的
    乘法折磨）。判据用它独有的入口名 mineru-kit，而不是解析 `mineru --version`
    —— 后者要起一个 Python 进程，还怕版本号格式变。
    """
    override = (venv_arg or VENV_OVERRIDE or "").strip()
    if override:
        act = 'source "%s/bin/activate"' % _tilde_to_home(override.rstrip("/"))
        if version_arg in MINERU_VERSIONS:
            return version_arg, act, "环境覆盖 + 显式 --mineru-version %s" % version_arg
        major, why = probe_engine(act)
        if major is None:
            raise EngineProbeError(
                "--venv %s 指定的环境用不了：%s\n"
                "  → 要么换一个能用的环境，要么显式指定代次："
                "--mineru-version 4 （或 3）" % (override, why))
        return major, act, "环境覆盖；auto 探测：%s" % why

    if version_arg in MINERU_VERSIONS:
        act = ENV_ACTIVATE_CMD_V3 if version_arg == "3" else ENV_ACTIVATE_CMD
        return version_arg, act, "显式 --mineru-version %s" % version_arg

    major, why = probe_engine(ENV_ACTIVATE_CMD)
    if major is None:
        raise EngineProbeError(
            "自动判代失败：%s\n"
            "  → 两条出路（任选一条）：\n"
            "     1) 改本文件顶部的 ENV_ACTIVATE_CMD，"
            "写成你机器上真正能跑通的激活命令；\n"
            "     2) 或跳过探测，显式指定代次： --mineru-version 4   （或 3）" % why)
    if major == "3":
        return "3", ENV_ACTIVATE_CMD_V3, "auto：%s" % why
    return "4", ENV_ACTIVATE_CMD, "auto：%s" % why


# ================================================================
# 文件发现
# ================================================================

def discover_files(paths, recursive, version, images=False, include_out=False,
                   stats=None):
    """扫描路径。Windows 侧：普通路径相对 cwd resolve + 存在性检查；
    WSL 原生路径：原样透传（存在性交给 worker 侧），仅支持文件。

    两条默认纪律（v6.3 新增，都有强制开关）：

    1) **显式点名的文件收图片，目录展开的不收**。一条命令行参数或 @列表里的
       「文件名」= 你就是在指着它说"处理它"；从目录里翻出来的不算。目录要连图片
       一起收，加 --images。
    2) **目录递归时剪掉 MinerU 产出目录**（hybrid_auto/auto/txt/ocr/images）。
       不剪的话 -r 扫一遍书库会把既有产出里的 <stem>_origin.pdf 当成新书反复解析
       （实测<corpus-root> 下有 245 个 *_origin.pdf、142,709 个 jpg）。要连它们一起扫，
       加 --include-mineru-out。

    剪枝用 os.walk(topdown) 改 dirnames 实现——不是"走进去再过滤"，是压根不进去，
    所以那 14 万个 jpg 连名字都不会被读到。只剪目录展开出来的，显式点名的文件不受
    这两条限制。
    """
    mm = mineru_exts(version)
    pp = pandoc_exts(version)
    supported = mm | pp
    stats = stats if stats is not None else {}
    for k in ("excluded_dirs", "skipped_images"):
        stats.setdefault(k, 0)
    stats.setdefault("unsupported", {})     # ext -> 计数（目录扫描时按扩展名汇总）
    collected, seen = [], set()

    def take(f):
        if f not in seen:
            seen.add(f)
            collected.append(f)

    for p_str in paths:
        # ---- 显式给出：WSL 原生路径字符串 ----
        if IS_WINDOWS and looks_like_wsl_path(p_str):
            p = Path(p_str)
            if p.suffix.lower() in supported:
                take(p)
            else:
                eprint(f"  !! 跳过 <不支持>: {p_str}（{_ext_hint(p.suffix.lower(), version)}）")
            continue

        p = Path(p_str).resolve()
        if not p.exists():
            eprint(f"  !! 跳过 <不存在>: {p_str}")
            continue

        # ---- 显式给出：文件（图片也算"点名"，照收）----
        if p.is_file():
            if p.suffix.lower() in supported:
                take(p)
            else:
                eprint(f"  !! 跳过 <不支持>: {p.name}（{_ext_hint(p.suffix.lower(), version)}）")
            continue

        if not p.is_dir():
            continue

        # ---- 目录展开 ----
        for root, dirnames, filenames in os.walk(p):
            if not recursive:
                dirnames[:] = []
            elif not include_out:
                keep = []
                for d in dirnames:
                    if d.lower() in MINERU_OUTPUT_DIRS:
                        stats["excluded_dirs"] += 1
                    else:
                        keep.append(d)
                dirnames[:] = keep
            for fn in filenames:
                suf = os.path.splitext(fn)[1].lower()
                if suf not in supported:
                    # 目录扫描不逐条报（一场 -r 可能几万条）；只按扩展名汇总，
                    # 由 main 汇总成一行——既不静默，也不刷屏。
                    stats["unsupported"][suf] = stats["unsupported"].get(suf, 0) + 1
                    continue
                if suf in IMAGE_EXTS and not images:
                    # 图片默认不扫：只在显式点名或 --images 时收
                    stats["skipped_images"] += 1
                    continue
                take(Path(root) / fn)

    if natsort:
        return natsort.natsorted(collected, key=str)
    return sorted(collected, key=str)


def _ext_hint(suf, version):
    """扩展名被跳过时的提示：区分「另一代支持」与「两代都不支持」。"""
    for other in MINERU_VERSIONS:
        if other != version and suf in supported_exts(other):
            return f"该扩展名需要 MinerU v{other}（当前 v{version}）"
    if suf in IMAGE_EXTS:
        return "图片需显式点名文件，或加 --images"
    return "不在支持列表内"


def read_file_list(list_path):
    """读取 @列表。相对行 -> 相对列表文件所在目录；#/; 注释；
    Windows 路径检查存在性，WSL 路径透传（worker 侧检查）。"""
    lp = Path(list_path)
    if IS_WINDOWS and looks_like_wsl_path(lp):
        base = str(lp).rsplit("/", 1)[0] or "/"   # WSL dirname（字符串级）
    else:
        lp = lp.resolve()
        if not lp.exists():
            eprint(f"X 文件列表不存在: {list_path}")
            sys.exit(1)
        base = lp.parent

    raw_bytes = lp.read_bytes()
    enc = "utf-8"
    try:
        import chardet
        enc = chardet.detect(raw_bytes).get("encoding", "utf-8") or "utf-8"
    except ImportError:
        pass
    text = raw_bytes.decode(enc, "replace")
    if "\r\n" in text:
        lines = text.split("\r\n")
    elif "\r" in text:
        lines = text.split("\r")
    else:
        lines = text.split("\n")

    paths = []
    for ln, line in enumerate(lines, 1):
        line = line.strip().strip("\"'")
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if IS_WINDOWS and looks_like_wsl_path(line):
            if not line.startswith("/"):
                line = base + "/" + line
            p = Path(line)
        else:
            if IS_WINDOWS:
                line = line.replace("/", "\\")
            else:
                line = line.replace("\\", "/")
            p = Path(line)
            if not p.is_absolute():
                p = base / p
            p = p.resolve()
            if not p.exists():
                eprint(f"X 文件列表 第{ln}行 文件不存在: {line}")
                sys.exit(1)
        paths.append(p)
    if not paths:
        eprint(f"X 文件列表为空: {list_path}")
        sys.exit(1)
    return paths


def collect_all_paths(paths, recursive, version, images=False, include_out=False):
    """处理 @ 文件列表 + 普通路径，合并返回 (Path 列表, 统计)。

    @列表里的每一行都算「显式点名」，故图片照收（不受 --images 约束）；但扩展名
    仍要过该版本的支持表 —— 否则一行 .txt 会掉进 pandoc 分支，生成
    `pandoc x.txt --from docx` 这种注定失败的命令（旧版本有这个洞）。
    """
    supported = supported_exts(version)
    stats = {}
    raw, scan_args = [], []
    for p_str in paths:
        if p_str.startswith("@"):
            for f in read_file_list(p_str[1:]):
                if f.suffix.lower() in supported:
                    raw.append(f)
                else:
                    suf = f.suffix.lower()
                    stats.setdefault("unsupported", {})
                    stats["unsupported"][suf] = stats["unsupported"].get(suf, 0) + 1
                    eprint("  !! 跳过 <不支持>: %s（%s）"
                           % (f.name, _ext_hint(suf, version)))
        else:
            scan_args.append(p_str)
    if scan_args:
        raw.extend(discover_files(scan_args, recursive, version,
                                  images=images, include_out=include_out,
                                  stats=stats))
    out_list, seen = [], set()
    for f in raw:
        if IS_WINDOWS and looks_like_wsl_path(f):
            k = ("wsl", str(f))
        else:
            k = (f.name.lower(), f.stat().st_size)
        if k not in seen:
            seen.add(k)
            out_list.append(f)
    return out_list, stats


# ================================================================
# PDF 拆卷（v4.8 保留）：页数超限拆到原始目录（缓解 VLM 峰值内存）
# ================================================================

def split_oversized(pdfs, limit, fixed_stems=None, dry_run=False):
    """页数 > limit 的 PDF 自动拆卷（PyMuPDF，跨平台 pip 可装），拆件放原始目录。

    - 拆件命名: <stem>_卷NN_pAAAA-BBBB.pdf（与 scripts/split_pdf.py 一致）
    - fixed_stems: {Path: 清洗后主干}。用清洗名给拆件命名，避免拆件继承源文件
      主干里的尾部点/空格（否则脏名会顺着拆件扩散，还得再改一遍）
    - dry_run=True：只预演，**一个字节都不写**。打印将拆成哪些卷，并把卷路径
      照常交给后续流程（这样 dry-run 的任务清单与实跑一致，卷名过长也能提前
      报出来），但卷文件此刻并不存在。
    - 已拆卷的文件（文件名匹配 _卷NN_pAAAA-BBBB）不再拆
    - 打开失败 / 拆卷失败的书直接跳过并警告
    - Ctrl+C 中断：已拆卷保留，原 PDF 本轮不处理（重跑自动继续）

    返回 (out_list, dropped)：out_list 为待处理文件（拆过卷的是卷，没拆的是原文件）；
    dropped 是"被丢弃、本轮不会出现在任何任务里"的输入文件数。这个数必须往上报——
    否则一本打不开的 PDF 只留下滚动日志里的一行警告，最后的 OK/FAIL/SKIP 结算根本
    不认账，跑几千本时你并不知道少了几本。
    """
    try:
        import pymupdf  # PyMuPDF（新 API 名，import fitz 已 deprecated）
    except ImportError:
        eprint("  !! 需要 PyMuPDF 拆卷: pip install pymupdf（本次跳过拆卷，大 PDF 可能 OOM）")
        return pdfs, 0          # 没拆卷，但也没丢书：原文件照常进任务

    out_list = []
    dropped = 0
    for fp in pdfs:
        if RE_SPLIT_VOL.search(fp.name):
            out_list.append(fp)
            continue
        try:
            doc = pymupdf.open(str(fp))
        except Exception as e:
            eprint(f"  !! 跳过（MuPDF 解析失败）: {fp.name} ({str(e)[:80]})")
            dropped += 1
            continue
        pages = doc.page_count
        base_stem = (fixed_stems or {}).get(fp, fp.stem)
        plan = volume_plan(pages, limit)
        if not plan:
            doc.close()
            out_list.append(fp)
            continue
        if dry_run:
            # 只预演，绝不落盘；卷路径照常交给后续流程，好让 dry-run 的任务
            # 清单与实跑一致（卷名过长这类问题才能提前报出来）。
            eprint(f"  [dry-run] {fp.name}: {pages}页 > {limit}页 -> 将拆为 "
                   f"{len(plan)} 卷（只提示，不写盘；卷文件此刻并不存在）:")
            for i, start, end in plan:
                eprint(f"              {volume_name(base_stem, i, start, end)}")
            doc.close()
            for i, start, end in plan:
                out_list.append(fp.parent / volume_name(base_stem, i, start, end))
            continue
        eprint(f"  !! {fp.name}: {pages}页 > {limit}页 -> 拆为 {len(plan)} 卷（{fp.parent}）")
        try:
            for i, start, end in plan:
                vol_name = volume_name(base_stem, i, start, end)
                out_doc = pymupdf.open()
                out_doc.insert_pdf(doc, from_page=start, to_page=end - 1)
                out_doc.save(str(fp.parent / vol_name))
                out_doc.close()
                out(f"  [{i}/{len(plan)}] {vol_name} ({end - start}页)")
                out_list.append(fp.parent / vol_name)
        except KeyboardInterrupt:
            eprint("  !! 拆卷中断：已拆卷保留，原 PDF 本轮不处理（重跑自动继续）")
            doc.close()
            return out_list, dropped
        except Exception as e:
            eprint(f"  !! 拆卷失败 -> 跳过本书: {fp.name} ({str(e)[:80]})")
            doc.close()
            dropped += 1
            continue
        doc.close()
    return out_list, dropped


# ================================================================
# bash 脚本生成（v6.0 核心）
# ================================================================

def _sq(s):
    """bash 单引号包裹。路径含单引号/双引号（Windows 文件名几乎不可能，
    WSL 罕见）无法安全嵌入，显式报错——不静默降级。"""
    if "'" in s or '"' in s:
        raise ValueError(f"路径含引号，无法安全生成 bash 脚本: {s}")
    return "'" + s + "'"


def _bash_quote_ok(s):
    """bash 脚本中双引号上下文安全检查（echo/source 等）：拒绝引号字符。"""
    if "'" in s or '"' in s:
        raise ValueError(f"内容含引号，无法安全生成 bash 脚本: {s}")
    return s


def _skip_test(out_dir_q, out_file_q):
    """skip 判据（bash 表达式，直接喂 if）：两代任一已有产出即跳过。

    out_file_q = **本条命令实际会写的那个文件**，由 --v4-format 决定：
        zip      -> <out_root>/<stem>.zip
        markdown -> <out_root>/<stem>.md
    v3 产出     = <out_root>/<stem>/ 下的任意 .md（跨引擎，换引擎重跑不白干）

    ⚠ v6.4：v4 一侧**只认本格式的产出**——旧 flat .md 不再满足 zip 的判据，
    否则 zip 的元数据永远拿不到（见文件头「skip 判据」一节的论证）。
    格式判断留给调用方；本函数不做版本/格式推断，拿到什么文件就测什么文件。
    """
    return (f"[ -f {out_file_q} ] || "
            f"find {out_dir_q} -name '*.md' 2>/dev/null | grep -q .")


def _mineru_cmd(job, opts):
    """单书 mineru 命令。两代语法完全不同，故分两支。"""
    out_root_wsl, src_wsl, stem, kind = job
    out_q = _sq(str(out_root_wsl))
    src_q = _sq(src_wsl)
    if opts.version == "4":
        # v4: mineru-kit parse SRC -o OUT [--tier T] --format F
        cmd = f"mineru-kit parse {src_q} -o {out_q}"
        # --format 缺省 zip（<out>/<stem>.zip，带全部侧车与独立图片）
        cmd += " --format " + _bash_quote_ok(opts.v4_format)
        # --tier 只对 pdf 和图片传（= 上游 TIERED_PARSE_EXTENSIONS）：其余扩展名在 v4
        # 里是 flash-only，单书模式下显式传一个不支持的档位会当场报错
        # （kit/commands/parse.py 里 ensure_tier_supported_for_parse_extension
        # 只在「非 batch 且 tier 显式」时调用）。
        if kind in TIERED_EXTS:
            cmd += " --tier " + _bash_quote_ok(opts.tier)
    else:
        # v3: mineru -p SRC -o OUT --backend B [--lang L]
        cmd = f"mineru -p {src_q} -o {out_q} --backend {_bash_quote_ok(opts.backend)}"
        if opts.lang:
            cmd += " --lang " + _bash_quote_ok(opts.lang)
    if opts.extra_args:
        cmd += " " + " ".join(_bash_quote_ok(a) for a in opts.extra_args)
    return cmd


def _batch_cmd(gjobs, opts):
    """整组一条 mineru-kit 命令（v4 batch 模式）。

    --tier 在 batch 下是安全的：mineru-kit 的 is_batch 分支会跳过「档位是否支持该
    扩展名」的检查，并把 office/html/epub 等强制成 flash。但组内没有 pdf/图片时仍
    不传，免得依赖那条隐含路径。
    """
    cmd = 'mineru-kit parse "${__batch[@]}" -o ' + _sq(str(gjobs[0].out_root_wsl))
    cmd += " --format " + _bash_quote_ok(opts.v4_format)
    if any(j.kind in TIERED_EXTS for j in gjobs):
        cmd += " --tier " + _bash_quote_ok(opts.tier)
    if opts.extra_args:
        cmd += " " + " ".join(_bash_quote_ok(a) for a in opts.extra_args)
    return cmd


def _pandoc_cmd(job):
    """epub/docx -> pandoc。两代 MinerU 都用这一支。"""
    out_root_wsl, src_wsl, stem, kind = job
    fmt = "epub" if kind == ".epub" else "docx"
    md_out = _sq(f"{out_root_wsl}/{stem}/{stem}.md")
    media_out = _sq(f"{out_root_wsl}/{stem}/media")
    return (f'"$PANDOC" {_sq(src_wsl)} --from {fmt} --to gfm '
            f"--output {md_out} --extract-media {media_out} "
            f"--markdown-headings=atx")


def build_bash_script(jobs, opts, renames=None):
    """生成执行脚本。jobs: [Job(...)] 已按执行序排好；opts: ScriptOpts。

    renames: [(旧路径, 新路径)] 名字修复清单（去尾部点/首尾空白），以幂等的 __ren
    在脚本最前面执行。源文件与**同名旧产出**成对给出 —— v3 的旧产出是目录，
    v4 的旧产出是 <stem>.md 文件，两代都要改：只改源文件会留孤儿，且下次重复解析。

    引号纪律：所有路径用 _sq 单引号包裹（含空格/中文/括号/$/反引号整体安全；
    含引号字符的路径已在体检阶段被拒）；echo 显示文本一律经 _bash_quote_ok。

    v4 的两种回收粒度，差别只在这一个维度上：
      book  -> 每本一条命令、各自独立进程，进程退出即整块回收内存（缺省）
      batch -> 按 out_root 分组，每组一条命令带多本，一个进程跑完整组才回收
    pandoc 的 epub/docx 不参与分组，永远逐书跑（不占 GPU，也没有泄漏问题）。
    """
    if not jobs:
        return None
    total = len(jobs)
    v = opts.version
    batch_mode = (v == "4" and opts.recycle == "batch")
    mm = mineru_exts(v)
    mineru_jobs = [j for j in jobs if j.kind in mm]
    pandoc_jobs = [j for j in jobs if j.kind not in mm]

    L = []
    L.append("#!/usr/bin/env bash")
    L.append("# mineru_batch v6.5 自动生成（可手动重跑；/tmp 重启自动清理）")
    if v == "4":
        L.append(f"# MinerU v4 / mineru-kit  reclaim={opts.recycle}  tier={opts.tier}"
                 f"  format={opts.v4_format}")
    else:
        L.append(f"# MinerU v3 / mineru  backend={opts.backend}")
    L.append(f"# jobs={total} (mineru {len(mineru_jobs)}, pandoc {len(pandoc_jobs)})")
    L.append("")

    if renames:
        # 名字修复段：必须幂等（旧名不存在＝已改过），且绝不覆盖已存在的目标名。
        # 改的是源文件名；产出名由源文件主干决定，故随后自动一致。
        L.append("# ---- 名字修复（去尾部点/首尾空白；源文件与同名旧产出成对）----")
        L.append("__ren() {")
        L.append('  [ -e "$1" ] || return 0        # 旧名不存在 => 已改过，静默跳过')
        L.append('  if [ -e "$2" ]; then echo "  [改名跳过·目标已存在] $2" >&2; return 0; fi')
        L.append('  if mv -n -- "$1" "$2" 2>/dev/null; then echo "  [改名] $2"; '
                 'else echo "  !! 改名失败: $1" >&2; fi')
        L.append("}")
        for old_p, new_p in renames:
            L.append(f"__ren {_sq(old_p)} {_sq(new_p)}")
        L.append("")

    # ---- 环境变量：按版本挑真正生效的那些 ----
    if v == "3":
        if opts.backend != "pipeline":
            L.append(f"export MINERU_API_MAX_CONCURRENT_REQUESTS={MINERU_MAX_CONCURRENT}")
            L.append(f"export MINERU_PROCESSING_WINDOW_SIZE={MINERU_WINDOW_SIZE} "
                     "MINERU_PDF_RENDER_TIMEOUT=2000 MINERU_PDF_RENDER_THREADS=5")
    else:
        # v4 包里根本没有 MINERU_API_MAX_CONCURRENT_REQUESTS 这个变量
        # （实测 grep 全包为空），导出它只会让人以为它在起作用 —— 不导。
        L.append(f"export MINERU_PROCESSING_WINDOW_SIZE={MINERU_WINDOW_SIZE} "
                 "MINERU_PDF_RENDER_TIMEOUT=2000 MINERU_PDF_RENDER_THREADS=5")
        if opts.lang:
            # v4 的 mineru-kit 没有 --lang 选项，语言只能走环境变量
            L.append(f"export MINERU_LANG={_bash_quote_ok(opts.lang)}")
    # 环境激活：命令**原样**取自文件顶部的 ENV_ACTIVATE_CMD[_V3]（用户可改）。
    # 刻意不做任何转义 —— 那是给用户的自由文本，含 $HOME / $(conda info --base)
    # 是**预期用法**；替它加引号或转义反而会把 $HOME 变成字面量。
    if opts.activate_cmd.strip():
        L.append(f"{opts.activate_cmd} || "
                 "{ echo '!! 环境激活失败（改文件顶部的 ENV_ACTIVATE_CMD）' >&2;"
                 " exit 1; }")
    else:
        L.append("# ENV_ACTIVATE_CMD 留空：不插入激活命令，直接用当前环境")
    if pandoc_jobs:
        L.append('PANDOC="$(command -v pandoc || true)"')
        L.append('if [ -z "$PANDOC" ]; then '
                 'echo "!! pandoc 未找到（epub/docx 需要）" >&2; exit 1; fi')
    L.append("")
    L.append("__ok=0; __fail=0; __skip=0" + ("; __bfail=0" if batch_mode else ""))
    L.append("")

    def emit_book(head, job):
        """逐书一段：v3 全部、v4 book 模式、以及 pandoc 的全部。"""
        out_root_wsl, src_wsl, stem, kind = job
        stem_txt = _bash_quote_ok(stem)
        out_q = _sq(str(out_root_wsl))
        out_dir_q = _sq(f"{out_root_wsl}/{stem}")
        # v4 产出后缀由 --v4-format 决定；v3 与 pandoc 恒为 .md
        out_ext = (_v4_out_ext(opts.v4_format)
                   if (v == "4" and kind in mineru_exts(v)) else ".md")
        out_file_q = _sq(f"{out_root_wsl}/{stem}{out_ext}")
        L.append(f"# ---- {head} {kind} :: {stem_txt} ----")
        if not opts.no_skip:
            L.append(f"if {_skip_test(out_dir_q, out_file_q)}; then")
            L.append(f"  echo '{head}[skip] {stem_txt}（已有产出）'")
            L.append("  __skip=$((__skip+1))")
            L.append("else")
        L.append(f"  echo '{head} {stem_txt}'")
        if kind in mineru_exts(v):
            if v == "4":
                # 钉死 -o 的目录语义：mineru-kit 按「末段有没有后缀」判 -o 是文件还是
                # 目录，输出根不存在且末段带点时会被当成文件路径（见文件头 v6.2 一节）。
                L.append(f"  mkdir -p {out_q}")
            L.append("  " + _mineru_cmd(job, opts))
        else:
            L.append("  " + _pandoc_cmd(job))
        L.append("  rc=$?")
        L.append('  if [ $rc -eq 0 ]; then __ok=$((__ok+1)); echo "    OK"; '
                 'else __fail=$((__fail+1)); echo "    FAIL (rc=$rc)"; fi')
        if not opts.no_skip:
            L.append("fi")
        L.append("")

    def emit_batches(mjobs):
        """按 out_root 分组，每组一条命令。组内顺序 = jobs 原序。"""
        order, buckets = [], {}
        for job in mjobs:
            key = str(job.out_root_wsl)
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(job)
        n_groups = len(order)
        for gi, key in enumerate(order, 1):
            gjobs = buckets[key]
            head = f"[组 {gi}/{n_groups}]"
            L.append(f"# ---- {head} {len(gjobs)} 本 -> {key} ----")
            L.append("__batch=()")
            for job in gjobs:
                stem_txt = _bash_quote_ok(job.stem)
                out_dir_q = _sq(f"{job.out_root_wsl}/{job.stem}")
                # batch 只在 v4 下存在，故后缀直接按 v4_format 取
                out_file_q = _sq(
                    f"{job.out_root_wsl}/{job.stem}"
                    f"{_v4_out_ext(opts.v4_format)}")
                if opts.no_skip:
                    L.append(f"__batch+=( {_sq(job.src_wsl)} )")
                else:
                    L.append(f"if {_skip_test(out_dir_q, out_file_q)}; then")
                    L.append(f"  echo '{head}[skip] {stem_txt}（已有产出）'; "
                             "__skip=$((__skip+1))")
                    L.append("else")
                    L.append(f"  __batch+=( {_sq(job.src_wsl)} )")
                    L.append("fi")
            L.append('if [ ${#__batch[@]} -eq 0 ]; then')
            L.append(f"  echo '{head} 全部已有产出，整组跳过'")
            L.append("else")
            L.append(f"  mkdir -p {_sq(key)}")
            L.append("  " + _batch_cmd(gjobs, opts))
            L.append("  rc=$?")
            L.append('  if [ $rc -eq 0 ]; then __ok=$((__ok+${#__batch[@]})); '
                     'echo "    OK ${#__batch[@]} 本"; '
                     'else __bfail=$((__bfail+${#__batch[@]})); '
                     'echo "    BATCHFAIL (rc=$rc)：本组剩余的书没跑（首错即停）"; fi')
            L.append("fi")
            L.append("")

    if batch_mode:
        for idx, job in enumerate(pandoc_jobs, 1):
            emit_book(f"[pandoc {idx}/{len(pandoc_jobs)}]", job)
        emit_batches(mineru_jobs)
    else:
        for idx, job in enumerate(jobs, 1):
            emit_book(f"[{idx}/{total}]", job)

    L.append('echo "=========================="')
    if batch_mode:
        L.append(f'echo "OK $__ok  FAIL $__fail  SKIP $__skip  '
                 f'BATCHFAIL $__bfail  / {total}"')
        L.append('echo "（BATCHFAIL = 该组整体失败、组内剩余的书没跑；'
                 '组内逐本进度未知，不虚报成 FAIL）"')
        L.append("exit $(( (__fail + __bfail) > 0 ? 1 : 0 ))")
    else:
        L.append(f'echo "OK $__ok  FAIL $__fail  SKIP $__skip  / {total}"')
        L.append("exit $(( __fail > 0 ? 1 : 0 ))")
    return "\n".join(L) + "\n"


# ================================================================
# 执行（Linux / Windows 双平台）
# ================================================================

def _tmp_script_name():
    return f"/tmp/mineru_batch_{os.getpid()}.sh"


def launch(script_text, dry_run=False):
    """把脚本交给 bash 执行。Windows：wsl.exe 三步（灌入->chmod->bash -l）；
    Linux：直接写 /tmp -> bash -l。stdout 一律继承控制台（不 PIPE）。
    返回进程退出码。"""
    name = _tmp_script_name()
    if dry_run:
        # stdout 纯净输出脚本本体（信息已由 main 走 stderr）——可直接 > 重定向
        out(script_text, end="")
        return 0

    if IS_WINDOWS:
        # 1) stdin 灌入 /tmp（Windows Python 写不了 WSL 的 /tmp，借 wsl stdin）
        p1 = subprocess.Popen(["wsl", "bash", "-c", f"cat > {name}"],
                              stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE)
        p1.communicate(script_text.encode("utf-8"))
        if p1.returncode != 0:
            eprint(f"X 写入 {name} 失败（wsl 不可用？）")
            return 1
        # 2) chmod
        r2 = subprocess.run(["wsl", "bash", "-lc", f"chmod +x {name}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r2.returncode != 0:
            eprint(f"  !! chmod {name} 返回 {r2.returncode}（继续执行）")
        # 3) 执行。stdout/stderr 不指定 -> 继承控制台 -> wsl 分配 PTY。
        #    用 Popen 而非 call：Ctrl+C 时 Python 也收 CTRL_C_EVENT 抛
        #    KeyboardInterrupt，需捕获后继续等 wsl 自然退出（同组已转发
        #    SIGINT，wsl 内 mineru/bash 正在清理）。
        #    [PLAN-B] 若实测 Ctrl+C 后 Python 抢信号导致 wsl 变孤儿：
        #    改为 p3 = subprocess.Popen(...); 然后 os._exit(0)（不等待），
        #    并删掉下方 except KeyboardInterrupt 的等待逻辑。
        out(f"bash 脚本: {name}（wsl 执行中；Ctrl+C 急停）")
        try:
            p3 = subprocess.Popen(["wsl", "bash", "-l", name])
            rc = p3.wait()
        except KeyboardInterrupt:
            eprint("\n  Ctrl+C 已送达 wsl（同组 SIGINT 转发中）...")
            try:
                rc = p3.wait(timeout=60)   # 等 wsl 内 mineru/bash 自然退出
            except subprocess.TimeoutExpired:
                eprint("  60s 内未退出，terminate wsl.exe")
                p3.terminate()
                try:
                    rc = p3.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p3.kill()
                    rc = 130
            if rc is None:
                rc = 130
        return rc
    else:
        # Linux：直接写 /tmp
        try:
            Path(name).write_text(script_text, encoding="utf-8")
        except OSError as e:
            eprint(f"X 写入 {name} 失败: {e}")
            return 1
        os.chmod(name, 0o755)
        out(f"bash 脚本: {name}（bash 执行中；Ctrl+C 急停）")
        try:
            p3 = subprocess.Popen(["bash", "-l", name])
            rc = p3.wait()
        except KeyboardInterrupt:
            eprint("\n  Ctrl+C 已送达（SIGINT 转发中）...")
            try:
                rc = p3.wait(timeout=60)
            except subprocess.TimeoutExpired:
                p3.terminate()
                try:
                    rc = p3.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p3.kill()
                    rc = 130
            if rc is None:
                rc = 130
        return rc


# ================================================================
# Main
# ================================================================

def main():
    p = argparse.ArgumentParser(
        description="MinerU 批量处理 v6.5 -- bash 生成器（v3/v4 双适配；v4 默认 zip 序列化；按版本分流扩展名；docx->pandoc，其余->mineru）",
    )
    p.add_argument("paths", nargs="+", help="输入文件/目录（相对 cwd），或 @文件列表")
    p.add_argument("-o", "--output", default=None,
                   help="输出根目录（缺省=各书所在目录，各归其位；Windows 或 WSL 路径）")
    p.add_argument("--mineru-version", choices=("auto",) + MINERU_VERSIONS,
                   default=DEFAULT_MINERU_VERSION,
                   help=("MinerU 大版本（缺省 auto：用顶部的 ENV_ACTIVATE_CMD 激活后"
                         "问环境，有 mineru-kit 即 v4，优先 4；显式 3 / 4 可跳过探测）"))
    p.add_argument("--venv", default=None,
                   help=("覆盖环境：激活命令改为 source \"<X>/bin/activate\"，探测也走它"
                         "（缺省用顶部的 ENV_ACTIVATE_CMD[_V3]；env MINERU_WSL_VENV 同效）"))
    p.add_argument("--tier", choices=("flash", "basic", "standard", "advanced"),
                   default=None,
                   help=f"[仅 v4] 解析档位（缺省 {DEFAULT_TIER}；只作用于 pdf —— office 类是 flash-only）")
    p.add_argument("--v4-recycle", choices=V4_RECYCLE_CHOICES, default=None,
                   help=("[仅 v4] 内存回收粒度：book=每书一进程、退出即回收（缺省）；"
                         "batch=全部书共用一个进程，省引擎启动但内存只在本批结束时回收"))
    p.add_argument("--v4-format", choices=V4_FORMAT_CHOICES, default=None,
                   help=(f"[仅 v4] 产出序列化（缺省 {DEFAULT_V4_FORMAT}）。"
                         "zip=markdown.md + 三个侧车 json + images/，保留 tier/"
                         "parse_mode/页号/脚注结构与图片类型；"
                         "markdown=单个 <stem>.md，图片 base64 内联，元数据全丢"))
    p.add_argument("--backend", default=None,
                   help=f"[仅 v3] 解析后端（缺省 {DEFAULT_BACKEND}；pipeline 不设窗口内存控制）")
    p.add_argument("--lang", default=DEFAULT_LANG,
                   help=("文档语言（默认不指定，MinerU 自动检测；如 --lang ch）。"
                         "v3 走 --lang，v4 走 MINERU_LANG 环境变量"))
    p.add_argument("-r", "--recursive", action="store_true",
                   help="递归扫描子目录")
    p.add_argument("--no-skip", action="store_true",
                   help="强制重处理已有结果的书")
    p.add_argument("--split-limit", type=int, default=400,
                   help="PDF 页数超限自动拆卷（默认400；=0不拆；pipeline不拆）")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印生成的 bash 脚本，不执行（拆卷也只预演、不写盘）")
    p.add_argument("--extra-args", nargs=argparse.REMAINDER,
                   help="透传给 mineru CLI 的额外参数（放最后）")
    p.add_argument("--max-path", type=int, default=DEFAULT_MAX_PATH,
                   help=("Win32 路径长度上限（默认 %d；0=不检查长度，只查名字合法性）"
                         % DEFAULT_MAX_PATH))
    p.add_argument("--skip-bad", action="store_true",
                   help="跳过名字非法/路径超长的书，只跑其余的（默认：列出全部问题后退出，不做任何改动）")
    p.add_argument("--images", action="store_true",
                   help=("目录扫描时也收集图片（png/jpg/…；默认只在显式点名文件时收）。"
                         "默认关闭的原因：书库里的 jpg 大多是 MinerU 产出的图片资产"))
    p.add_argument("--include-mineru-out", action="store_true",
                   help=("目录递归时不排除 MinerU 产出目录（hybrid_auto/auto/txt/ocr/images）。"
                         "默认排除，避免把既有产出里的 <stem>_origin.pdf 当成新书重跑"))
    a = p.parse_args()

    enable_console()
    atexit.register(restore_console)  # 只恢复真被改动过的输出模式（没改过就不动）

    # -- 决定用哪一代 MinerU / 哪条激活命令。后面的长度预算、命令形态、skip 判据
    #    全都跟着它走，所以必须在任何磁盘改动之前定下来 --
    #    探测失败 = 配置的激活命令跑不通 ⟹ 当场停，不要生成一份注定在第 3 行
    #    就 exit 1 的脚本（那种"跑了一半才报环境错"最费排查时间）。
    try:
        major, activate_cmd, why = resolve_engine(a.mineru_version, a.venv)
    except EngineProbeError as exc:
        eprint("X %s" % exc)
        sys.exit(1)

    # 版本专属参数写给了另一个版本 = 直接报错。静默忽略会让人以为它生效了，
    # 而"以为 --tier 生效了"的代价是几小时的解析跑在不想要的档位上。
    if major == "4" and a.backend is not None:
        eprint("X --backend 是 MinerU v3 的参数；当前是 v4，请改用 --tier"
               "（或加 --mineru-version 3）")
        sys.exit(1)
    if major == "3" and a.tier is not None:
        eprint("X --tier 是 MinerU v4 的参数；当前是 v3，请改用 --backend"
               "（或加 --mineru-version 4）")
        sys.exit(1)
    if major == "3" and a.v4_recycle is not None:
        eprint("X --v4-recycle 只在 MinerU v4 下有意义（当前是 v3）")
        sys.exit(1)
    if major == "3" and a.v4_format is not None:
        eprint("X --v4-format 只在 MinerU v4 下有意义（当前是 v3）")
        sys.exit(1)
    backend = a.backend or DEFAULT_BACKEND
    tier = a.tier or DEFAULT_TIER
    recycle = a.v4_recycle or DEFAULT_V4_RECYCLE
    # env MINERU_V4_FORMAT 绕过了 argparse 的 choices，故此处再校一遍
    v4_format = a.v4_format or DEFAULT_V4_FORMAT
    if v4_format not in V4_FORMAT_CHOICES:
        eprint("X --v4-format 只收 %s（当前 %r，可能来自 env MINERU_V4_FORMAT）"
               % ("|".join(V4_FORMAT_CHOICES), v4_format))
        sys.exit(1)

    # -- 输出根目录：显式 -o -> 统一 worker 侧；缺省 -> None（各归其位 = 文件所在目录） --
    if a.output:
        if IS_WINDOWS and looks_like_wsl_path(a.output):
            out_root_wsl = a.output.replace("\\", "/").rstrip("/")  # WSL 路径字符串
        else:
            out_root_wsl = to_worker_path(Path(a.output).resolve())
    else:
        out_root_wsl = None

    # -o 指定的输出根目录自身也要过一遍：它名字非法或已吃掉大半路径预算，
    # 会波及每一本书；提前一条说清楚，避免变成「每本书报一遍」的噪音。
    if out_root_wsl is not None:
        if "'" in out_root_wsl or '"' in out_root_wsl:
            eprint("X -o 输出根目录含引号，无法安全生成 bash 脚本：%r" % out_root_wsl)
            sys.exit(1)
        base = out_root_wsl.rstrip("/").rsplit("/", 1)[-1]
        if base and not win_name_ok(base):
            eprint("X -o 输出根目录名不合法：%r —— %s" % (base, win_name_reason(base)))
            sys.exit(1)
        if a.max_path > 0:
            head_win = to_win_form(out_root_wsl)
            # 用该版本最长形态的产出模板量。stem="" 都顶爆上限，就没有任何 stem 放得下。
            if (head_win is not None
                    and predicted_output_len(head_win, "", ".pdf", major,
                                             v4_format)
                    >= a.max_path - PATH_SAFETY):
                eprint("X -o 输出根目录自身已吃掉 %d 字符的预算（上限 %d，余量 %d），"
                       "没有空间再放 <stem>"
                       % (predicted_output_len(head_win, "", ".pdf", major,
                                               v4_format),
                          a.max_path, PATH_SAFETY))
                sys.exit(1)

    worker_tag = "wsl bash -l" if IS_WINDOWS else "bash -l"
    # dry-run 时信息走 stderr，stdout 只留脚本（可 > 重定向出干净 .sh）
    log = eprint if a.dry_run else out
    if major == "4":
        log(f"engine: MinerU v4 / mineru-kit  |  tier: {tier}  |  recycle: {recycle}"
            f"  |  format: {v4_format}")
    else:
        log(f"engine: MinerU v3 / mineru  |  backend: {backend}")
    log(f"  （{why}；激活命令: {activate_cmd or '(留空：不插入)'}）")
    log(f"lang: {a.lang or 'auto'}  |  max_path: {a.max_path if a.max_path > 0 else '不检查'}")
    log(f"output: {out_root_wsl if out_root_wsl is not None else '(缺省) 各书所在目录（各归其位）'}")
    log(f"exec: {worker_tag}  |  {('dry-run 只生成' if a.dry_run else '生成后执行')}")

    # -- 发现文件（@列表 + 目录递归 + 去重保序） --
    files, scan_stats = collect_all_paths(a.paths, a.recursive, major,
                                          images=a.images,
                                          include_out=a.include_mineru_out)
    if scan_stats.get("excluded_dirs"):
        log("  （剪掉 %d 个 MinerU 产出目录；要一起扫加 --include-mineru-out）"
            % scan_stats["excluded_dirs"])
    if scan_stats.get("skipped_images"):
        log("  （跳过 %d 个图片；要一起扫加 --images，或显式点名文件）"
            % scan_stats["skipped_images"])
    unsup = scan_stats.get("unsupported") or {}
    if unsup:
        top = sorted(unsup.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        log("  （跳过 %d 个扩展名不支持的：%s%s）"
            % (sum(unsup.values()),
               "  ".join("%s×%d" % (k, n) for k, n in top),
               "  …" if len(unsup) > len(top) else ""))
        # 最容易被当成 bug 的情形：这批文件在另一代下是支持的 —— 直接说清楚
        others = [o for o in MINERU_VERSIONS if o != major]
        cross = sorted({k for k, _ in top
                        if any(k in supported_exts(o) for o in others)})
        if cross:
            log("     其中 %s 是 MinerU v%s 才支持的扩展名（加 --mineru-version %s）"
                % ("/".join(cross), "/".join(others), others[0]))
    if not files:
        eprint("X 没有发现可处理的文件")
        sys.exit(1)

    def parent_wsl(p):
        """Path -> worker 侧父目录字符串。Windows 路径转 /mnt/... 后字符串级
        rsplit；WSL 路径原样。避免 WindowsPath('/mnt/...').parent 产出
        反斜杠路径污染 worker 侧。"""
        s = to_worker_path(p)
        i = s.rfind("/")
        return s[:i] if i > 0 else "/"

    def eff_out_wsl(fp):
        """无 -o 时各归其位：输出根 = 文件所在目录（worker 侧字符串）。"""
        if out_root_wsl is not None:
            return out_root_wsl
        return parent_wsl(fp)

    def join_wsl(dir_wsl, name):
        """worker 侧字符串级拼接（避免 WindowsPath 反斜杠污染）。"""
        return (dir_wsl.rstrip("/") or "") + "/" + name

    # ============================================================
    # v6.1 名字 / 路径体检（在任何磁盘改动之前）
    # ============================================================
    def inspect(fp):
        """返回 Rec：名字级用 analyze_name，路径级用 stem_budget 反推预算。"""
        st, new_name, new_stem, reason = analyze_name(fp.name)
        detail = ""
        # 单引号（Windows 其实允许）会破坏 _sq 的单引号包裹，而本脚本不做字符
        # 替换，故直接报失败并指出是哪个组件。查整条 worker 侧路径：引号也可能
        # 落在**父目录**上，那种情况改文件名没用。
        wp = to_worker_path(fp)
        if "'" in wp or '"' in wp:
            comp = next(c for c in wp.split("/") if "'" in c or '"' in c)
            return Rec(fp, "FATAL", new_stem, new_name,
                       "路径组件 %r 含引号，无法安全生成 bash 脚本"
                       "（本脚本不替换字符，请手工改名该组件）" % comp, "")
        if st != "FATAL" and a.max_path > 0:
            out_win = to_win_form(eff_out_wsl(fp))
            if out_win is not None:      # None = 非 DrvFs，MAX_PATH 不适用
                kind = fp.suffix.lower()
                # 长度预算按版本算：v3 的 <stem> 在产出路径里出现两次，v4 只出现一次。
                # 拿 v3 的尺子量 v4 会误杀一批 v4 完全放得下的长书名。
                budget = stem_budget(out_win, kind, a.max_path, major, v4_format)
                need = u16_len(new_stem)
                if need > budget:
                    st = "FATAL"
                    reason = ("产出路径超长：预测 %d 字符 > 上限 %d（余量 %d）"
                              % (predicted_output_len(out_win, new_stem, kind, major,
                                                      v4_format),
                                 a.max_path, PATH_SAFETY))
                    detail = ("该 stem %d 字符，预算 %d 字符 -> 需再缩短 %d 字符"
                              % (need, budget, need - budget))
        return Rec(fp, st, new_stem, new_name, reason, detail)

    def report_fatal(rs, skipped):
        eprint("")
        eprint("X 有 %d 个名字/路径必须手工处理%s"
               % (len(rs),
                  "（--skip-bad：本次跳过这些书）" if skipped
                  else "；本次一个都不动，退出码 1："))
        for r in rs:
            eprint("  [%s] %s" % (r.reason, to_worker_path(r.path)))
            if r.detail:
                eprint("        %s" % r.detail)
        if not skipped:
            eprint("  -> 改完名字后重新运行；或加 --skip-bad 跳过这些书先跑其余的")

    recs = [inspect(fp) for fp in files]
    fixable_cand = [r for r in recs if r.status == "FIXABLE"]

    # 撞名检查：清洗后同名（两个源文件撞车，或目标已被别的东西占用）
    # 撞名无法自动处理 -> 与非法名字同等对待（报用户，不猜）
    clash = []
    seen_target = {}
    for r in fixable_cand:
        key = (parent_wsl(r.path), r.new_name)
        if key in seen_target:
            clash.append(Rec(r.path, "FATAL", r.stem, r.new_name,
                             "清洗后与 %s 撞名" % seen_target[key].path.name, ""))
            continue
        seen_target[key] = r
        if not (IS_WINDOWS and looks_like_wsl_path(str(r.path))):
            tgt = r.path.with_name(r.new_name)
            if tgt.exists():
                clash.append(Rec(r.path, "FATAL", r.stem, r.new_name,
                                 "清洗后目标名已被占用：%s" % tgt.name, ""))

    # 撞名的源文件必须从待处理集合里剔除：它既不能自动改名（会覆盖别人），
    # 也不能当正常任务跑（否则同一个输出名会被解析两次）。
    clash_paths = set(r.path for r in clash)
    fatal = [r for r in recs if r.status == "FATAL"] + clash
    if fatal:
        report_fatal(fatal, a.skip_bad)
        if not a.skip_bad:
            sys.exit(1)

    live = [r for r in recs if r.status != "FATAL" and r.path not in clash_paths]
    mm = mineru_exts(major)
    n_mineru_live = sum(1 for r in live if r.path.suffix.lower() in mm)
    log("Total %d  处理 %d（mineru %d / pandoc %d）%s"
        % (len(files), len(live), n_mineru_live, len(live) - n_mineru_live,
           ("  [名字/路径问题跳过 %d]" % len(fatal)) if fatal else ""))
    # 扩展名一多，光看总数就不知道这批到底混了什么 —— 给一行分布
    hist = {}
    for r in live:
        k = r.path.suffix.lower()
        hist[k] = hist.get(k, 0) + 1
    if len(hist) > 1:
        log("  扩展名: " + "  ".join("%s×%d" % (k, hist[k]) for k in sorted(hist)))

    fixable = [r for r in live if r.status == "FIXABLE"]
    if fixable:
        log("")
        log("名字修复（自动；源文件 + 同名旧产出成对改名）:")
        for r in fixable:
            log("  %s  ->  %s" % (r.path.name, r.new_name))

    # -- PDF 拆卷（仅 pdf；pipeline 后端不拆）。拆件用清洗后的主干命名，
    #    否则源文件主干里的尾部点/空格会顺着拆件扩散，下一轮还得再改一遍 --
    rec_by_path = {r.path: r for r in live}
    pdfs = [r.path for r in live if r.path.suffix.lower() == ".pdf"]
    dropped = 0
    # pipeline 后端不支持拆卷（v3 专属约束；v4 无 backend 概念，永远可拆）
    if a.split_limit > 0 and "pipeline" not in backend and pdfs:
        pdfs, dropped = split_oversized(pdfs, a.split_limit,
                                        {r.path: r.stem for r in live},
                                        dry_run=a.dry_run)
        if dropped:
            eprint("  !! %d 个 PDF 打不开或拆卷失败，本轮丢弃"
                   "（不计入下面的任务数，末尾会再结算一次）" % dropped)
    # 是否真产生了新卷：看有没有"原先不存在的卷路径"。不能用 len 变化判断——
    # 只丢不拆（或既丢又拆到数量持平）时 len 会误导。
    split_happened = any(p not in rec_by_path for p in pdfs)

    # 拆件是新生成的文件：重新体检（名字必然是清洗过的，但长度可能顶爆预算）
    final_recs = []
    for fp in pdfs:
        final_recs.append(rec_by_path[fp] if fp in rec_by_path else inspect(fp))
    for r in live:
        if r.path.suffix.lower() != ".pdf":
            final_recs.append(r)
    vol_fatal = [r for r in final_recs if r.status == "FATAL"]
    if vol_fatal:
        report_fatal(vol_fatal, a.skip_bad)
        if split_happened:
            eprint("  !! 注：PDF 拆卷已经发生（拆件落在原目录），这部分改动无法回滚")
        if not a.skip_bad:
            sys.exit(1)
        final_recs = [r for r in final_recs if r.status != "FATAL"]

    # -- 组装 jobs：pandoc 快先跑，mineru（office + pdf）慢后跑 --
    #    skip 检测统一在 bash 侧执行（worker 侧路径一致，Windows/WSL 路径都能判）
    def mk_jobs(rs):
        return [Job(eff_out_wsl(r.path),
                    join_wsl(parent_wsl(r.path), r.new_name),
                    r.stem,
                    r.path.suffix.lower())
                for r in rs]

    # 顺序 = 先快后慢：pandoc（纯文本转换，秒级）-> mineru 的 flash 档扩展名
    # （office/html/epub/csv/…，读文本层，很快）-> 需要模型的 pdf/图片（走 VLM，最慢）。
    # 慢的放最后，好处是中途 Ctrl+C 时已经拿到的都是便宜的成果。
    def _suf(r):
        return r.path.suffix.lower()

    jobs = (mk_jobs([r for r in final_recs if _suf(r) in pandoc_exts(major)])
            + mk_jobs([r for r in final_recs
                       if _suf(r) in mineru_exts(major) and _suf(r) not in TIERED_EXTS])
            + mk_jobs([r for r in final_recs
                       if _suf(r) in mineru_exts(major) and _suf(r) in TIERED_EXTS]))
    if not jobs:
        eprint("X 没有可处理的文件")
        sys.exit(1)

    # -- 改名前导段：源文件 + 同名**旧产出**成对。旧产出必须一起改，否则留孤儿、
    #    且下次重跑会重复解析。形态有三种，全列上（不存在的会被 __ren 静默跳过）：
    #    v3 是目录 <out>/<stem>/，v4 是文件 <out>/<stem>.md 或 <out>/<stem>.zip --
    renames = []
    for r in fixable:
        d = parent_wsl(r.path)
        old_stem = split_name(r.path.name)[0]
        renames.append((join_wsl(d, r.path.name), join_wsl(d, r.new_name)))
        if old_stem != r.stem:
            out_w = eff_out_wsl(r.path)
            renames.append((join_wsl(out_w, old_stem), join_wsl(out_w, r.stem)))
            # v4 两种序列化都可能已在盘上（历史 flat .md + 现行 .zip），两个都改
            for ext in (".md", ".zip"):
                renames.append((join_wsl(out_w, old_stem) + ext,
                                join_wsl(out_w, r.stem) + ext))

    if a.no_skip:
        log("\nForce reprocess all (--no-skip)")
    else:
        log("\nto process: %d（已有产出的会在 bash 内自动 skip：%s / "
            "<out>/<stem>/ 下的 .md）"
            % (len(jobs),
               "<out>/<stem>.zip" if v4_format == "zip" else "<out>/<stem>.md"))
    for job in jobs:
        log("  [%s] %s" % (job.kind, job.src_wsl))
    if major == "4" and recycle == "batch":
        log("")
        log("  ⚠ batch 粒度：按输出根分组，每组一个 mineru-kit 进程；"
            "组内任一本失败 -> 该组剩余的书都不跑（上游首错即停）")
        log("     无 -o（各归其位）时每组通常只有一本，实际等于 book 粒度")
    log()

    opts = ScriptOpts(version=major, backend=backend, tier=tier, lang=a.lang,
                      activate_cmd=activate_cmd,
                      extra_args=a.extra_args if a.extra_args else None,
                      no_skip=a.no_skip, recycle=recycle, v4_format=v4_format)
    try:
        script_text = build_bash_script(jobs, opts, renames=renames)
    except ValueError as e:
        eprint(f"X {e}")
        sys.exit(1)
    if script_text is None:
        eprint("X 无可生成任务")
        sys.exit(1)

    rc = launch(script_text, dry_run=a.dry_run)

    # 结算：把"没进入本轮任务"的书明确列出来。否则 OK/FAIL/SKIP 的合计只覆盖
    # 进了任务的那些，被丢弃/被跳过的会像凭空消失。只写 stderr，不污染 dry-run
    # 的 stdout（那份 stdout 是干净的 .sh，可直接重定向）。
    if dropped or fatal:
        parts = []
        if dropped:
            parts.append("DROP %d（PDF 打不开或拆卷失败，本轮丢弃）" % dropped)
        if fatal:
            parts.append("SKIPBAD %d（名字/路径问题%s）"
                         % (len(fatal), "，--skip-bad 跳过" if a.skip_bad else ""))
        eprint("")
        eprint("本轮未进入任务：" + "；".join(parts))
        eprint("            （上面的 OK/FAIL/SKIP 合计只含已进入任务的那些）")
    sys.exit(rc)


if __name__ == "__main__":
    main()
