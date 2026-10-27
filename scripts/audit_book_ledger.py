#!/usr/bin/env python3
"""书账目对账 —— 逐层查"库里的账"与"真实情况"对不对得上。

■ 为什么不是一条 SQL
账目不平有六种成因，各自要不同的查法，而且**必须从上往下**查（上层的异常会
污染下层的统计）。本脚本按层输出，每层给出"发现了什么"与"不发现说明什么"。

■ 六层
  ① 状态分布        status 里有没有躺在 'processing' 的僵尸行（导入中断的残留）
  ② 完整性          expected_chunks 与实际 chunk 数是否一致（切块/嵌入中途失败）
  ③ 孤儿            chunk / 脚注 / 图的 book_guid 是否都还在 documents_book 里
                    （删书时级联漏掉 ⟹ 表看着有数据、按书查却查不到）
  ④ 幂等键          source_path_hash 有没有重复（重复 ⟹ 同一本书进了两次）
  ⑤ 无 tag          空 tag 的书（tag 过滤搜不到、只有 OMNIA 看得到）
  ⑥ 文件系统对账    给定语料根目录，逐个 md/txt 反查库 —— **这一层才是真账**
                    （前五层只能证明"库里自洽"，证明不了"该进的书都进了"）

■ 用法
    python scripts/audit_book_ledger.py                    # 只查库内（①–⑤）
    python scripts/audit_book_ledger.py <local-path>  # 加第 ⑥ 层
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tomllib

import mariadb

CFG = Path(__file__).resolve().parent.parent / "config.toml"


def connect():
    db = tomllib.load(open(CFG, "rb"))["database"]
    return mariadb.connect(host=db["host"], port=int(db["port"]), user=db["user"],
                           password=db["password"], database=db["name"])


def _win(p: Path) -> str:
    """盘上路径 → 库里 source_path 的形态（Windows 反斜杠、盘符大写）。

    ⚠️ 对账必须**两侧归一化后精确相等**。本脚本第一版用 `LIKE %根目录名%` +
    「basename 后缀」启发式匹配 —— 那会匹配到别的树里同名的目录、也会漏掉
    改过名的文件。弱探针的输出只能当线索，不能当结论。
    """
    s = str(p)
    if s.startswith("/mnt/") and len(s) > 6 and s[6] == "/":
        return s[5].upper() + ":" + s[6:].replace("/", "\\")
    return s.replace("/", "\\")


def q(cur, sql, params=()):
    """按**列名**取行（不用位置索引 —— 新增一列就全错位）。"""
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main() -> int:
    roots = [Path(a) for a in sys.argv[1:]]
    c = connect()
    cur = c.cursor()

    print("=" * 68)
    print("① 状态分布")
    for r in q(cur, "SELECT status, COUNT(*) n FROM documents_book GROUP BY status ORDER BY n DESC"):
        print(f"   {r['status']:<12} {r['n']:>6}")
    zombies = q(cur, "SELECT guid, key_metadata, source_path, expected_chunks, created_at "
                     "FROM documents_book WHERE status <> 'done' LIMIT 20")
    if zombies:
        print("   ⚠️ 非 done 的行（导入中断残留；重导该书即可，md5(source_path) 幂等）：")
        for z in zombies:
            print(f"      [{z['guid']}] {z['key_metadata'][:30]!r} chunks={z['expected_chunks']} {z['created_at']}")
    else:
        print("   ✅ 全部 done，无僵尸行")

    print("=" * 68)
    print("② 完整性：expected_chunks vs 实际 chunk 数")
    rows = q(cur, """SELECT b.guid, b.key_metadata, b.expected_chunks,
                            (SELECT COUNT(*) FROM documents_book_chunk c WHERE c.book_guid=b.guid) actual
                       FROM documents_book b""")
    bad = [r for r in rows if r["expected_chunks"] != r["actual"]]
    print(f"   共 {len(rows)} 本；不一致 {len(bad)} 本")
    for r in bad[:15]:
        print(f"      ⚠️ {r['key_metadata'][:34]!r} 声明={r['expected_chunks']} 实际={r['actual']}")
    if not bad:
        print("   ✅ 全部一致（切块与嵌入都没中途掉队）")

    print("=" * 68)
    print("③ 孤儿行（book_guid 已不在 documents_book 里）")
    for tbl, label in (("documents_book_chunk", "chunk"), ("page_footnotes", "脚注")):
        r = q(cur, f"SELECT COUNT(*) n FROM {tbl} t "
                   f"WHERE NOT EXISTS (SELECT 1 FROM documents_book b WHERE b.guid=t.book_guid)")[0]
        print(f"   {label:<6} 孤儿 {r['n']:>6}" + ("   ⚠️ 级联删除漏了" if r["n"] else "   ✅"))

    print("=" * 68)
    print("④ 幂等键重复（source_path_hash 应唯一）")
    dup = q(cur, """SELECT source_path_hash, COUNT(*) n, GROUP_CONCAT(guid) guids,
                           GROUP_CONCAT(key_metadata SEPARATOR ' | ') names
                      FROM documents_book GROUP BY source_path_hash HAVING n > 1""")
    if dup:
        for d in dup[:10]:
            print(f"      ⚠️ {d['n']} 行同 hash {d['source_path_hash'][:12]}… : {d['names'][:60]}")
    else:
        print("   ✅ 无重复（同一本书没被导入两次）")

    print("=" * 68)
    print("⑤ 无 tag 的书（tag 过滤搜不到、只有 OMNIA 全集可见）")
    unt = q(cur, "SELECT guid, key_metadata, source_path FROM documents_book "
                 "WHERE tags IS NULL OR TRIM(COALESCE(tags,'')) = ''")
    print(f"   共 {len(unt)} 本")
    for u in unt[:10]:
        print(f"      • {u['key_metadata'][:34]!r}  {u['source_path'][:56]}")

    if roots:
        print("=" * 68)
        print("⑥ 文件系统对账（**这一层才是真账**）")
        for root in roots:
            if not root.is_dir():
                print(f"   ⚠️ 不是目录，跳过: {root}")
                continue
            files = sorted(x for x in root.rglob("*")
                           if x.is_file() and x.suffix.lower() in (".md", ".txt"))
            prefix = _win(root) + "\\"
            # ⚠️ **不要用 SQL LIKE 做这个前缀筛选**：LIKE 里 `\` 是默认转义符，
            # `\%` 会被读成"转义后的百分号=字面量 %"，于是模式恒不命中（实测踩过：
            # 231 个文件全被判成"应进未进"）。取全表到 Python 里 startswith ——
            # 库就几百行、路径百来字节，代价可忽略，换来零转义歧义。
            all_paths = [r["source_path"] for r in q(cur, "SELECT source_path FROM documents_book")]
            have = {p_ for p_ in all_paths if p_ and p_.startswith(prefix)}
            print(f"   {root}")
            print(f"      盘上 {len(files)} 个 md/txt；库里 source_path 以该前缀开头的 {len(have)} 本")
            miss = [f for f in files if _win(f) not in have]
            extra = sorted(h for h in have
                           if h and not any(_win(f) == h for f in files))
            if miss:
                print(f"      ⚠️ 盘上有、库里没有（应进未进）{len(miss)} 个：")
                for m in miss[:15]:
                    print(f"         {_win(m)}")
            if extra:
                print(f"      ⚠️ 库里有、盘上没有（文件已挪/删，行还在）{len(extra)} 个：")
                for e in extra[:15]:
                    print(f"         {e}")
            if not miss and not extra:
                print("      ✅ 精确一致：盘上每个 md/txt 都有行，库里每行都有文件")
            print(f"      小结：盘 {len(files)} vs 库 {len(have)} "
                  f"（差 {len(files) - len(have):+d}；差 = 应进未进 {len(miss)} − 已删未清 {len(extra)}）")
    else:
        print("=" * 68)
        print("⑥ 已跳过 —— 未给语料根目录。**前五层只能证明「库里自洽」**，")
        print("   证明不了「该进的书都进了」。要查真账：")
        print("       python scripts/audit_book_ledger.py <local-path> [...]")

    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
