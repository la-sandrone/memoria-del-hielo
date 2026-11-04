"""
文件名: mhi_annotate.py
摘要: CLI: mhi note（规范名 mhi-annotate）—— 给**已入库的书**合并误差统计/注记。
      两条入口：
        · 批量：--from <manifest.jsonl|manifest.csv>
        · 直连：--guid/-g、--path/-p ＋ --untrusted/-u [TEXT]

      为什么需要它：机械那部分误差（形近串写 / 断词 / 成串汉字 / 康熙部首 / PUA）
      由导入器按同一套判据自动算，但**每本书的情况不一样** —— 离线扫描、人工翻看、
      「这批是扫描件、符号系统整体崩」这类判断，代码里写不出规则。manifest 与
      --untrusted 就是那两个外部入口，殊途同归到 errata_stats.notes。

      `mhi note -g <guid> -u -y`：最常用的形状（短选项是给手伤场景留的）。
      注记的 source 记 `annotate:--untrusted`，与导入时的 `import:--untrusted`、
      离线审计的 `audit_zip.py@日期` 分开 —— 三种来源可信度不同。

      **默认 dry-run**（不给 --apply 只报会改什么）—— 因为匹配错了会把注记挂到
      别的书上，而且这种错误**不报错、只静默污染**。

      ── 匹配键（按 robustness 降序，逐个试到唯一命中为止）──────────────
        content_md5    md5(正文)。最硬，但**需要对全库算一次 md5**（几百 MB 扫描）
        source_path    原始文件路径（文档里就是 .zip 的绝对路径）
        title          书名（key_metadata）—— 重名风险最高，命中多本直接拒绝
      路径两侧都做归一：Windows 盘符 ↔ /mnt/<盘>、反斜杠、重复斜杠、NFC。
      （本库被这件事咬过：改名 hash 漂移、/mnt/e 与 E:\\ 两种风格混存。）

      **多本命中一律拒绝，绝不静默取第一条。**

      ── manifest 形状 ──────────────────────────────────────────
      JSONL（推荐，能表达嵌套）:
        {"match": {"source_path": "<corpus-root>/解析后/.../某某.zip"},
         "errata": {"script_mix": 233, "hyphen_break": 29},
         "glossary": {"my_metric": "这东西是什么，一句人话"},
         "note": "离线扫描：233 处串写、29 处断词",
         "source": "audit_zip.py@2026-10-06"}

      CSV（表格友好，嵌套字段请走 JSONL）:
        source_path,script_mix,hyphen_break,note,source
        /mnt/<drive>/...,233,29,离线扫描,audit_zip.py@2026-10-06

      ⚠️ errata 里出现**既不在判据词表、又没自带 glossary 释义**的键会被拒绝
      （进 unregistered，渲染时只标「未登记信号」）—— 不许把一个 LLM 看不懂的
      键静默塞进 payload。
依赖: click, core.db, core.text_errata
"""

from __future__ import annotations

import csv
import json
import sys
import unicodedata
from pathlib import Path
from typing import Any

import click

from ..core.db import get_db
from ..core.text_errata import (
    _COUNT_KEYS,
    SIGNAL_GLOSSARY,
    UNTRUSTED_DEFAULT_NOTE,
    merge_errata,
)

# 匹配键按 robustness 降序。guid 最硬（本库自己发的，永不重名）；
# content_md5 次之（要全库算一次 md5）；title 最软（重名即拒绝）。
_MATCH_KEYS = ("guid", "content_md5", "source_path", "title")
_META_COLS = ("match", "errata", "glossary", "note", "source")


def _norm_path(p: str) -> str:
    """路径归一：盘符↔/mnt、反斜杠→斜杠、去重复斜杠、NFC、去尾斜杠。"""
    s = unicodedata.normalize("NFC", (p or "").strip())
    s = s.replace("\\", "/")
    if len(s) > 1 and s[1] == ":" and s[0].isalpha():
        s = "/mnt/" + s[0].lower() + s[2:]
    while "//" in s:
        s = s.replace("//", "/")
    return s.rstrip("/")


def _load_jsonl(path: Path) -> list[dict]:
    out = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rec = json.loads(line)
        except ValueError as exc:
            raise click.ClickException(f"{path.name}:{lineno} 不是合法 JSON: {exc}")
        if not isinstance(rec, dict):
            raise click.ClickException(f"{path.name}:{lineno} 顶层必须是对象")
        rec.setdefault("_line", lineno)
        out.append(rec)
    return out


def _load_csv(path: Path) -> list[dict]:
    """CSV 形状：匹配列 + 计数列（_COUNT_KEYS 名）+ note/source/glossary(JSON)。

    其余列视为外部键，必须在该行的 glossary 里给出释义，否则被拒。
    """
    out = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), 2):
            rec: dict[str, Any] = {"_line": lineno}
            match = {k: row[k] for k in _MATCH_KEYS if row.get(k)}
            if match:
                rec["match"] = match
            errata: dict[str, Any] = {}
            glossary: dict[str, str] = {}
            if row.get("glossary"):
                try:
                    glossary = json.loads(row["glossary"])
                except ValueError as exc:
                    raise click.ClickException(
                        f"{path.name}:{lineno} glossary 不是合法 JSON: {exc}")
            for col, val in row.items():
                if not col or col in _MATCH_KEYS or col in _META_COLS:
                    continue
                if val is None or str(val).strip() == "":
                    continue
                if col in _COUNT_KEYS or col.startswith("errata_"):
                    key = col[7:] if col.startswith("errata_") else col
                    try:
                        errata[key] = int(float(val))
                    except ValueError:
                        raise click.ClickException(
                            f"{path.name}:{lineno} 列 {col} 不是数字: {val!r}")
                else:
                    errata[col] = val
            if errata:
                rec["errata"] = errata
            if glossary:
                rec["glossary"] = glossary
            for k in ("note", "source"):
                if row.get(k):
                    rec[k] = row[k]
            out.append(rec)
    return out


def _book_index(db, need_md5: bool) -> list[dict]:
    """全库轻量索引（guid + 路径 + 书名 [+ body_md5]）。

    798 行的路径/书名是 KB 级；只有 manifest 用到 content_md5 时才算正文 md5
    （几百 MB 扫描，几秒级）—— 不无条件付这个代价。
    """
    cols = "guid, source_path, key_metadata"
    if need_md5:
        cols += ", MD5(content) AS body_md5"
    return db.execute_sql(f"SELECT {cols} FROM documents_book")


def _match(rec: dict, index: list[dict]) -> tuple[dict | None, str]:
    """返回 (命中的书, 失败原因)。多本命中 → (None, 原因)。"""
    spec = rec.get("match") or {}
    if not spec:
        return None, "没有 match 段（至少给 guid / source_path / content_md5 / title 之一）"
    for key in _MATCH_KEYS:
        want = spec.get(key)
        if not want:
            continue
        if key == "guid":
            want_g = str(want).strip().lower()
            hits = [b for b in index if (b.get("guid") or "").lower() == want_g]
        elif key == "content_md5":
            want_c = str(want).strip().lower()
            hits = [b for b in index if (b.get("body_md5") or "") == want_c]
        elif key == "source_path":
            want_p = _norm_path(str(want))
            hits = [b for b in index if _norm_path(b.get("source_path") or "") == want_p]
        else:
            hits = [b for b in index if (b.get("key_metadata") or "") == str(want)]
        if len(hits) == 1:
            return hits[0], ""
        if len(hits) > 1:
            return None, (f"{key} 命中 {len(hits)} 本，拒绝静默取第一条"
                          f"（{', '.join((h.get('key_metadata') or '')[:24] for h in hits[:3])}）")
    return None, "匹配键都没命中（路径可能改过名，试试 guid 或 content_md5）"


@click.command("mhi-annotate")
@click.option("--from", "from_", default=None,
              help="manifest 文件（.jsonl 或 .csv），绝对路径")
@click.option("-g", "--guid", "guids", multiple=True,
              help="直接点名一本书（可重复）；配 --untrusted 用")
@click.option("-p", "--path", "paths", multiple=True,
              help="按原始文件路径点名（可重复；盘符/反斜杠会自动归一）")
@click.option("-u", "--untrusted", "untrusted", is_flag=False,
              flag_value=UNTRUSTED_DEFAULT_NOTE, default=None, metavar="[TEXT]",
              help="手动标注该书「扫描内容不可信」（不带值时用内置文案："
                   "核对上下文 + 补充联网搜索）。只能配 --guid/--path")
@click.option("-y", "--apply", is_flag=True,
              help="真的写库（默认 dry-run，只报会改什么）")
@click.option("--replace", is_flag=True,
              help="丢弃已有的注记与外部键，完全按 manifest 重算（默认是合并）")
@click.option("--show", default=20, show_default=True, help="每类最多列几行明细")
def main(from_: str | None, guids: tuple[str, ...], paths: tuple[str, ...],
         untrusted: str | None, apply: bool, replace: bool, show: int):
    direct = bool(guids or paths)
    if from_ and direct:
        raise click.ClickException(
            "--from 与 --guid/--path 互斥（manifest 已带 match 段，别再点名）")
    if not from_ and not direct:
        raise click.ClickException(
            "要么 --from <manifest>，要么用 --guid/--path 点名一本书")
    if direct and untrusted is None:
        raise click.ClickException(
            "--guid/--path 必须配 --untrusted（否则没有要写的内容）")

    if direct:
        # 直连模式：把每个点名包成与 manifest 同形的 rec，后面走同一条落库路径。
        # source 写死带 CLI 前缀 —— 事后能分清「导入时标的」与「事后补的」。
        src = "annotate:--untrusted"
        recs = [{"match": {"guid": g}, "note": untrusted, "source": src}
                for g in guids]
        recs += [{"match": {"source_path": p}, "note": untrusted, "source": src}
                 for p in paths]
        click.echo(f"点名 {len(recs)} 本；模式 {'APPLY' if apply else 'dry-run（不写库）'}")
    else:
        mpath = Path(from_)
        if not mpath.is_file():
            raise click.ClickException(f"manifest 不存在: {mpath}")
        recs = (_load_jsonl(mpath) if mpath.suffix.lower() == ".jsonl"
                else _load_csv(mpath))
        click.echo(f"manifest {mpath.name}：{len(recs)} 条记录；"
                   f"模式 {'APPLY' if apply else 'dry-run（不写库）'}")
    if not recs:
        return

    need_md5 = any("content_md5" in (r.get("match") or {}) for r in recs)
    if need_md5:
        click.echo("  用到 content_md5 → 需要对全库正文算一次 md5（几秒）")
    index = _book_index(get_db(), need_md5)

    db = get_db()
    ok = skipped_existing = 0
    failures: list[tuple] = []
    unregistered: list[tuple] = []
    notes_added = 0

    for rec in recs:
        book, why = _match(rec, index)
        if book is None:
            failures.append((rec.get("_line"), why))
            continue
        guid = book["guid"]
        row = db.execute_sql("SELECT errata_stats FROM documents_book WHERE guid = ?",
                             (guid,))
        raw = (row[0].get("errata_stats") if row else "") or "{}"
        try:
            existing = json.loads(raw)
        except ValueError:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}

        incoming = dict(rec.get("errata") or {})
        src = str(rec.get("source") or mpath.stem)
        new = merge_errata(
            {} if replace else existing,
            incoming,
            src,
            note=rec.get("note"),
            glossary=rec.get("glossary"),
        )
        if new.get("unregistered"):
            keys = ", ".join(str(u.get("key")) for u in new["unregistered"])
            unregistered.append((rec.get("_line"), guid[:8], keys))

        before_notes = len(existing.get("notes") or [])
        after_notes = len(new.get("notes") or [])
        notes_added += max(0, after_notes - before_notes)
        if new == existing:
            skipped_existing += 1
            continue
        ok += 1
        if apply:
            db.execute_sql(
                "UPDATE documents_book SET errata_stats = ? WHERE guid = ?",
                (json.dumps(new, ensure_ascii=False, sort_keys=True), guid),
            )

    click.echo()
    click.echo("=" * 68)
    click.echo(f"命中并合并 {ok} 本；注记新增 {notes_added} 条；"
               f"内容无变化跳过 {skipped_existing} 本；匹配失败 {len(failures)} 条")
    if failures:
        click.echo()
        click.echo("⚠️ 匹配失败（凭 line 号回 manifest 查）：")
        for line, why in failures[:show]:
            click.echo(f"  line {line}: {why}")
    if unregistered:
        click.echo()
        click.echo("⚠️ 未登记信号（没在判据词表里、也没自带 glossary 释义）——")
        click.echo("   它们**不会**进 LLM 可见的 payload，等于白算：")
        for line, g, keys in unregistered[:show]:
            click.echo(f"  line {line} book {g}: {keys}")
    if not apply:
        click.echo()
        click.echo("dry-run 结束。确认无误后加 --apply 真写。")
        sys.exit(1 if failures else 0)
    else:
        sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
