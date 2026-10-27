#!/usr/bin/env python3
"""诊断：从会话日志抽取"真实检索查询"并统计分布（只读；可当作召回回归集的底座）。

来源
    DSH 会话 jsonl（<某创作项目>项目的"落实"系列），其中 `mcp__mcp-memoria__search_docs`
    的 tool/call 记录了真实检索查询、tool/result 记录了结果（可判空）。

用法
    python scripts\\diag_query_corpus.py --stats          # 统计 + 主题簇分类 + 示例（默认）
    python scripts\\diag_query_corpus.py --dump           # 打印全部去重查询（一行一条，供人工核对）
    python scripts\\diag_query_corpus.py --calib          # 追做书级信号标定（需 DB + 嵌入服务）

已知结论（2026-09-21 实测，详见 docs/真实检索查询分布案例.md）
    · 191 次 search_docs 调用 / 190 条去重查询；55% 的调用带 tag
    · 17 条返回空 → **100% 是调用方显式传了 fulltext_only=true**，与题材是否存在无关
    · 书级"存在性"判据不成立：104 条题材在库的真实查询上，任何阈值都会误伤 ~27%

依赖: 标准库（--calib 另需 mariadb + 嵌入 HTTP）
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 默认留空：本脚本会上公开仓，不能把个人目录写进默认值
#   用 --dir 指定，或设环境变量 MHI_QUERY_CORPUS_DIR（例如 <你的查询语料目录>）
DEFAULT_DIR = os.environ.get("MHI_QUERY_CORPUS_DIR", "")

# 主题簇关键词（用于把查询归类；先匹配先生效，顺序即优先级）
CLUSTERS: list[tuple[str, tuple[str, ...]]] = [
    ("原神/游戏剧情", ("原神", "<创作专名>", "璃月", "枫丹", "须弥", "至冬", "愚人众", "世界树", "<创作专名>",
                   "虚假之天", "<创作专名>", "月宫", "<创作专名>", "坎瑞亚", "天空岛", "雷神", "钟离",
                   "纳塔", "蒙德", "深渊", "地脉", "龙蜥", "Irminsul", "Narzissenkreuz", "Columbina",
                   "可莉", "莱茵多特", "爱丽丝", "凯亚", "迪卢克", "旅行者", "教令院", "天理",
                   "派蒙", "七七", "甘雨", "凝光", "星穹", "<创作专名>", "提纳里", "克莱纳", "Kleiner")),
    ("项目自造专名", ("<创作专名>", "月幕", "银月", "月灵", "月荡", "<创作专名>", "<创作专名>", "彩特琳德",
                 "<创作专名>", "<创作专名>", "<某创作项目>", "是啊，吃什么", "靛天", "<创作专名>罅隙", "疗养院")),
    ("SCP·基金会设定", ("基金会", "SCP", "先锋队", "分级", "收容", "异常", "Hogar")),
    ("冰质月球·天体地质", ("月球", "冰质", "极区", "水冰", "永久阴影", "月震", "冰盖",
                     "冰岩", "regolith", "lunar", "moon", "permafrost", "水岩比", "蛇纹石", "月海",
                     "月壳", "KREEP")),
    ("光学·干涉测量", ("光学", "干涉", "望远镜", "镜面", "液镜", "像差", "瑞利", "衍射", "集光",
                  "optical", "interferomet", "telescope", "diffraction", "aberration")),
    ("生命起源与演化", ("密码子", "遗传密码", "LUCA", "古菌", "氨基酸", "核糖体", "生命起源", "镜像生命",
                  "tRNA", "amino acid", "codon", "mycorrhiz", "菌丝", "古网", "Paleodictyon",
                  "脱硫丝菌", "钩连渊")),
    ("天体物理·空间天气", ("CME", "日冕", "红矮星", "恒星风", "UV", "FUV", "NUV", "SED", "系外行星",
                     "M dwarf", "heliosphere", "耀斑", "星际介质")),
    ("粒子·核物理", ("加速器", "对撞", "强子", "粲", "多重数", "氦", "截面", "激发", "cross section",
                 "helium", "triplet", "wakefield", "hadroniz", "pseudorapidity", "P b-Pb", "Pb-Pb")),
    ("行星流体·气候", ("罗斯贝", "较差自转", "对流", "瑞利数", "纬向流", "湍流", "气候", "大气",
                  "Rayleigh", "convection", "Rossby", "circulation")),
    ("相对论·数学物理", ("克尔", "度规", "测地线", "张量", "李群", "球谐", "微分几何", "广义相对论",
                   "Carter", "geodesic", "Kerr", "relativ")),
    ("科学传播·思想", ("卡尔萨根", "萨根", "宇宙日历", "魔鬼出没", "伪科学", "怀疑", "祛魅", "科普",
                  "Sagan", "cosmic calendar")),
    ("科幻·文学", ("科幻", "三体", "克苏鲁", "沙丘", "刘慈欣", "克拉克", "赛博", "星际航行",
                "齐奥尔科夫斯基", "Tsiolkovsky")),
    ("语言·词典", ("拉丁语", "希腊语", "词典", "正字法", "词源", "语法", "Latin", "Greek")),
]


def load_calls(src_dir: str) -> tuple[list[tuple[str, dict]], dict[str, str]]:
    """返回 (search_docs 调用参数, callId→结果文本)。"""
    calls: list[tuple[str, dict]] = []
    results: dict[str, str] = {}
    for p in sorted(glob.glob(os.path.join(src_dir, "*.jsonl"))):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"tool/' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                data = d.get("data") or {}
                if d.get("type") == "tool/call" and "search_docs" in str(data.get("name", "")):
                    raw = data.get("arguments")
                    if isinstance(raw, str):
                        try:
                            raw = json.loads(raw)
                        except Exception:
                            raw = None
                    if isinstance(raw, dict) and isinstance(raw.get("query"), str):
                        calls.append((raw["query"].strip(), raw))
                elif d.get("type") == "tool/result":
                    msg = data.get("message") or {}
                    cid = (msg.get("source") or {}).get("callId")
                    texts = []
                    for block in (msg.get("content") or []):
                        for inner in (block.get("content") or []):
                            if isinstance(inner.get("text"), str):
                                texts.append(inner["text"])
                    if cid:
                        results[cid] = "\n".join(texts)
    return calls, results


def cluster_of(query: str) -> str:
    for name, kws in CLUSTERS:
        low = query.lower()
        if any(k.lower() in low for k in kws):
            return name
    return "(未归类)"


def main() -> int:
    ap = argparse.ArgumentParser(description="真实检索查询语料的抽取与统计（只读）")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="会话 jsonl 目录（或设 MHI_QUERY_CORPUS_DIR）")
    ap.add_argument("--stats", action="store_true", help="统计（默认模式）")
    ap.add_argument("--dump", action="store_true", help="打印全部去重查询（一行一条）")
    ap.add_argument("--calib", action="store_true", help="追加书级信号标定（需 DB + 嵌入服务）")
    args = ap.parse_args()

    calls, results = load_calls(args.dir)
    print("== 来源 ==")
    print("   目录: %s" % args.dir)
    print("   文件: %d 个 jsonl" % len(glob.glob(os.path.join(args.dir, "*.jsonl"))))
    if not calls:
        print("✗ 未找到 search_docs 调用——检查 --dir / MHI_QUERY_CORPUS_DIR")
        return 2

    uniq = collections.Counter(q for q, _ in calls)
    lens = sorted(len(q) for q in uniq)
    tagged = sum(1 for _, a in calls if a.get("tags"))
    print("\n== 调用统计 ==")
    print("   search_docs 调用 %d 次；去重查询 %d 条" % (len(calls), len(uniq)))
    print("   带 tag 的调用 %d 次（%.0f%%）" % (tagged, 100.0 * tagged / len(calls)))
    print("   查询长度: min=%d 中位=%d max=%d" % (lens[0], lens[len(lens) // 2], lens[-1]))

    # 空结果归因
    empty_flags = collections.Counter()
    for q, a in calls:
        # callId 未在参数里，用结果文本反查（按 query 命中过于粗糙）→ 这里按参数旗标统计
        pass
    only_ft = sum(1 for _, a in calls if a.get("fulltext_only"))
    print("   fulltext_only 调用 %d 次" % only_ft)

    print("\n== 主题簇分布（去重后）==")
    by = collections.defaultdict(list)
    for q in uniq:
        by[cluster_of(q)].append(q)
    for name, qs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        print("   %-16s %3d 条" % (name, len(qs)))
        for q in qs[:3]:
            print("        · %s" % q[:74])

    if args.dump:
        print("\n== 全部去重查询 ==")
        for q, n in uniq.most_common():
            print("   ×%-2d %s" % (n, q))

    if args.calib:
        import statistics
        import tomllib
        import mariadb
        from src.core.config import load_config
        from src.core.embedding import get_embedding_service

        load_config()
        with open(ROOT / "config.toml", "rb") as fh:
            cfg = tomllib.load(fh)["database"]
        conn = mariadb.connect(host=cfg["host"], port=int(cfg["port"]), user=cfg["user"],
                               password=cfg["password"], database=cfg["name"])
        cur = conn.cursor()
        cur.execute("SET SESSION max_statement_time=120")
        cur.execute("SELECT tags FROM documents_book")
        lib_tags = {e.strip() for (t,) in cur.fetchall() for e in (t or "").split(",") if e.strip()}
        pos = sorted({q for q, a in calls
                      if a.get("tags") and all(e.strip() in lib_tags
                                               for e in str(a["tags"]).split(",") if e.strip())})
        neg = ["Python 异步编程最佳实践", "法式甜点马卡龙的配方", "股票量化交易策略回测",
               "自行车链条更换教程", "Nginx 反向代理配置", "Kubernetes 集群运维",
               "中医针灸穴位图解", "化妆品成分评测", "流行音乐编曲制作", "手机 App 界面设计规范"]
        emb = get_embedding_service()

        def sig(q):
            vec = "[" + ",".join(str(v) for v in emb.embed_one(q)) + "]"
            cur.execute("SELECT VEC_DISTANCE_COSINE(b.embedding, q.qv) AS d FROM documents_book b, "
                        "(SELECT VEC_FromText(?) AS qv) q ORDER BY d ASC", (vec,))
            ds = [float(r[0]) for r in cur.fetchall()]
            if len(ds) < 5:
                return None
            sd = statistics.pstdev(ds)
            return {"d1": ds[0], "gap": ds[4] - ds[0],
                    "z": (statistics.mean(ds) - ds[0]) / sd if sd else 0.0}

        rows = [(q, 1, sig(q)) for q in pos] + [(q, 0, sig(q)) for q in neg]
        rows = [r for r in rows if r[2]]
        print("\n== 书级信号标定（正=题材在库 %d 条 / 负=确定不在库 %d 条）==" % (
            sum(1 for r in rows if r[1]), sum(1 for r in rows if not r[1])))

        def auroc(name, higher_pos):
            P = [r[2][name] for r in rows if r[1] == 1]
            N = [r[2][name] for r in rows if r[1] == 0]
            w = t = 0
            for p in P:
                for n in N:
                    if p == n:
                        t += 1
                    elif (p > n) if higher_pos else (p < n):
                        w += 1
            return (w + 0.5 * t) / (len(P) * len(N)) if P and N else float("nan")

        for name, higher in (("d1", False), ("gap", True), ("z", True)):
            print("   %-5s AUROC = %.3f" % (name, auroc(name, higher)))
        P = sorted(r[2]["d1"] for r in rows if r[1] == 1)
        N = sorted(r[2]["d1"] for r in rows if r[1] == 0)
        for thr in (0.45, 0.49, 0.53):
            print("   d1 thr=%.2f → 判 absent 命中 %d/%d，误伤在库 %d/%d"
                  % (thr, sum(1 for x in N if x > thr), len(N),
                     sum(1 for x in P if x > thr), len(P)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
