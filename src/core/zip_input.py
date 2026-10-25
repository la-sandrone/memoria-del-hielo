"""
文件名: zip_input.py
摘要: MinerU v4 `--format zip` 产出的**物化器** —— 把 .zip 输入解成一个普通目录，
      之后交给既有的 md 导入路径，下游零改动。
      只解正文与两个侧车 + images/，跳过 model_output.json（8.4MB 且本项目不消费）；
      成员名一律过 zip-slip 防护与总量上限，不合格就整包拒绝（不清洗后继续）。
依赖: hashlib, json, os, shutil, zipfile
      dataclasses, pathlib
      core.config（定位 cache 根目录）
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import get_config

# ── 官方口径（docs/reference/output_files，2026-09-29 核对） ──
# `ParseResult.save(writer)` 写 markdown.md + middle_json.json +
# structured_content.json + images/，有原始模型输出时再加 model_output.json。
# 三个消费形态引用同一批素材 ⟹ 归档根下这几个成员名是**固定名**，不带 stem。
REQUIRED_MEMBER = "markdown.md"          # 正文；也是「这是不是一个能导入的 zip」的唯一判据
SIDECAR_MEMBERS = ("middle_json.json", "structured_content.json")
# 只有原始模型输出里才有的成员，本项目从不读它（实测 77 页论文里它 8.4 MB，
# 比正文大 70 倍）。少解它 = 少占 cache，也少一份没人读的文件。
SKIPPED_MEMBERS = ("model_output.json",)
IMAGES_PREFIX = "images/"

# 解包总量上限（zip 炸弹防护）。正常产物：77 页论文 11.4 MB；一本 45 MB 图片的书
# 解完也就百 MB 级。4 GiB 离正常形态两个数量级，只拦病态输入。
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
_CACHE_SUBDIR = "mineru_unzip"


class ZipInputError(RuntimeError):
    """zip 输入不合格（不是 MinerU 产出 / 成员名越界 / 超出总量上限）。"""


@dataclass(frozen=True)
class Materialized:
    """一次 zip 物化的结果。"""

    md_path: Path           # 解出来的正文（交给既有导入路径的就是它）
    work_dir: Path          # 解包根（= md 的父目录，images/ 就在它下面）
    reused: bool            # True = 命中既有解包目录，本次没写盘
    n_files: int            # 本次落盘的文件数（复用时为 0）
    n_bytes: int            # 本次落盘的字节数（复用时为 0）
    skipped: tuple[str, ...]  # 本次按设计跳过的成员名


# ── 成员名归一与防护 ────────────────────────────────────


def _norm_member(name: str) -> str:
    """成员名归一：反斜杠→斜杠，剥前导斜杠。

    Windows 侧打的 zip 可能用 `\\` 当分隔符（ZipFile 不替我们归一），
    `+/abs` 形态的前导斜杠也要剥掉，否则拼接时会跳出解包根。
    """
    n = name.replace("\\", "/")
    while n.startswith("/"):
        n = n[1:]
    return n


def _guard_member(norm: str) -> None:
    """zip-slip 防护：任何一段是 `..`，或含 `:`（盘符 / NTFS ADS）即整包拒绝。

    ⚠ 故意**不做「清洗后继续」**——把越界路径悄悄改成安全路径，比直接拒绝更危险：
    调用方以为拿到了完整产物，实际少了东西。要拒就拒整个包。
    """
    for part in norm.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ZipInputError(f"zip 成员名含 '..'，疑似路径穿越，整包拒绝: {norm!r}")
        if ":" in part:
            raise ZipInputError(
                f"zip 成员名含 ':'（盘符或 NTFS 数据流语法），整包拒绝: {norm!r}"
            )


def _root_prefix(names: set[str]) -> str | None:
    """返回正文成员所在的目录前缀（'' = 归档根）；**找不到就返回 None**。

    ⚠ '' 与 None 必须分开：'' 是"正文在归档根"这一合法结论，None 才是"没有正文"。
    首版把落空返回值也写成 ''，于是 is_mineru_zip() 对任何 zip 都返回 True ——
    一个把"没找到"混进"找到了、只是位置不同"的经典 bug。

    官方 CLI `--format zip` 把成员**平铺在归档根**（实测），故 '' 是正常路径。
    仍容错一层目录包装（V1 API 侧归档形态不保证与 CLI 相同）——
    但只认「恰好一层」这一种，不猜任意深度。
    """
    if REQUIRED_MEMBER in names:
        return ""
    for n in names:
        if n.endswith("/" + REQUIRED_MEMBER) and n.count("/") == 1:
            return n.rsplit("/", 1)[0] + "/"
    return None


# ── 判据与物化 ──────────────────────────────────────────


def is_mineru_zip(zip_path: Path) -> bool:
    """这个 zip 是不是一份可导入的 MinerU 产出？

    判据只有一条：归档里有 `markdown.md`。**不做启发式**——没有正文的 zip
    （如 `--format middle_json` 的裸 json 打成的包）无论结构多像，都不能导入
    （官方原话：standalone structural JSON is not a complete result package）。
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = {_norm_member(n) for n in zf.namelist()}
    except (OSError, zipfile.BadZipFile, NotImplementedError):
        return False
    return _root_prefix(names) is not None


def cache_root() -> Path:
    """解包根目录 = <项目根>/<cache.dir>/mineru_unzip（按 config 走，不硬写路径）。"""
    cfg = get_config()
    base = Path(cfg.get("cache.dir", "cache"))
    if not base.is_absolute():
        base = cfg.config_path.parent / base
    return (base / _CACHE_SUBDIR).resolve()


def _md5_file(p: Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _should_keep(rel: str) -> bool:
    """rel = 去掉包装前缀后的相对名。返回是否要落盘。"""
    if rel in SKIPPED_MEMBERS:
        return False
    if rel in (REQUIRED_MEMBER, *SIDECAR_MEMBERS):
        return True
    return rel.startswith(IMAGES_PREFIX)


def materialize(
    zip_path: Path,
    *,
    root: Path | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> Materialized:
    """把 MinerU zip 解成一个可导入的目录（幂等：同一内容只解一次）。

    目录名 = **zip 内容的 md5**（不是路径的 md5）：同一份产物换个位置或改个名，
    命中同一个解包目录；重新解析过的新产物只有一字之差也算不出同一个 md5，
    于是不会被旧内容串味——这一条比「按路径缓存」重要得多。

    解到 `<md5>.tmp/` 再原子改名，故不存在「解了一半的目录被当成完整产物」。

    参数:
        zip_path:   MinerU v4 `--format zip` 的产出。
        root:       解包根（缺省 config 的 cache/mineru_unzip；测试可注入）。
        stop_check: 中断检查；返回 True 则 raise KeyboardInterrupt（退出前清 tmp）。

    返回: Materialized
    """
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise ZipInputError(f"zip 不存在: {zip_path}")

    digest = _md5_file(zip_path)
    base = Path(root) if root is not None else cache_root()
    work_dir = base / digest
    md_path = work_dir / REQUIRED_MEMBER
    if md_path.is_file():
        return Materialized(
            md_path=md_path, work_dir=work_dir, reused=True,
            n_files=0, n_bytes=0, skipped=(),
        )

    base.mkdir(parents=True, exist_ok=True)
    tmp_dir = base / (digest + ".tmp")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    n_files = 0
    n_bytes = 0
    skipped: list[str] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = []
            for info in zf.infolist():
                norm = _norm_member(info.filename)
                _guard_member(norm)
                infos.append((norm, info))
            prefix = _root_prefix({n for n, _ in infos})
            if prefix is None:
                raise ZipInputError(
                    f"归档里找不到 {REQUIRED_MEMBER}，不是可导入的 MinerU 产出: {zip_path}"
                )

            # 选定要落的成员（rel = 去掉包装前缀后的相对名）。
            # 目录条目本身不落盘——mkdir(parents=True) 会按需建。
            planned: list[tuple[str, zipfile.ZipInfo]] = []
            for norm, info in infos:
                if norm.endswith("/"):
                    continue
                rel = norm[len(prefix):] if prefix else norm
                if rel in SKIPPED_MEMBERS:
                    skipped.append(rel)
                    continue
                if _should_keep(rel):
                    planned.append((rel, info))

            # 总量先算后解：不等到写满磁盘才发现是炸弹
            total = sum(max(info.file_size, 0) for _rel, info in planned)
            if total > MAX_TOTAL_BYTES:
                raise ZipInputError(
                    "解包总量 %d 字节超过上限 %d 字节（疑似 zip 炸弹）: %s"
                    % (total, MAX_TOTAL_BYTES, zip_path)
                )

            for rel, info in planned:
                if stop_check is not None and stop_check():
                    raise KeyboardInterrupt
                dest = tmp_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                n_files += 1
                n_bytes += max(info.file_size, 0)

        if not (tmp_dir / REQUIRED_MEMBER).is_file():
            raise ZipInputError(
                f"归档里的 {REQUIRED_MEMBER} 没解出来: {zip_path}"
            )

        # 溯源小票：解包目录名是哈希，人看不出它来自哪个 zip。
        # 排查「这本书是哪次解析出来的」时，这一行比翻日志快。
        try:
            st = zip_path.stat()
            (tmp_dir / "_zip_source.json").write_text(
                json.dumps(
                    {
                        "zip_path": str(zip_path.resolve()),
                        "zip_md5": digest,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "members_kept": n_files,
                        "members_skipped": sorted(set(skipped)),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass  # 小票写不进去不影响导入（cache 目录只读等），不阻断

        try:
            tmp_dir.rename(work_dir)
        except OSError:
            # 并发/抢占：别人先建好了同名目录 ⟹ 用别人那份，扔掉自己的
            if not md_path.is_file():
                raise
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return Materialized(
                md_path=md_path, work_dir=work_dir, reused=True,
                n_files=0, n_bytes=0, skipped=(),
            )
    except BaseException:
        # 任何出口（含 KeyboardInterrupt）都不留半成品目录
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    return Materialized(
        md_path=work_dir / REQUIRED_MEMBER,
        work_dir=work_dir,
        reused=False,
        n_files=n_files,
        n_bytes=n_bytes,
        skipped=tuple(sorted(set(skipped))),
    )


def describe(m: Materialized) -> str:
    """一行人类可读的物化结论（进身份头）。"""
    if m.reused:
        return f"zip 已解包（复用）: {m.work_dir}"
    skipped = f"，跳过 {','.join(m.skipped)}" if m.skipped else ""
    return (f"zip 已解包: {m.n_files} 个文件 / {m.n_bytes / 1048576:.1f} MB"
            f"{skipped} -> {m.work_dir}")


def _default_cache_root_for_tests() -> Path:
    """给测试用的稳定入口（避免测试各自拼路径）。"""
    return cache_root()


__all__ = [
    "IMAGES_PREFIX",
    "MAX_TOTAL_BYTES",
    "Materialized",
    "REQUIRED_MEMBER",
    "SIDECAR_MEMBERS",
    "SKIPPED_MEMBERS",
    "ZipInputError",
    "cache_root",
    "describe",
    "is_mineru_zip",
    "materialize",
]
