#!/usr/bin/env python3
# 诊断：量子力学I 图片引用丢失断点 v2
# 链路：原始MD → MineruPostprocessor.prepare_images → ImagePipeline._find_image
#      → _process_single_ref（MD5/硬链接/db）
#
# v2 改动：上一版只验证了查找链（0 丢失），断点在查找之后。
#         本版对查找命中的引用继续跑完整单图处理链路（_process_single_ref），
#         捕获并打印每张图的真实异常类型（type + message），按异常类型聚合。
#         真实导入中这些异常被 process_image_refs 的 except 吞掉只计 missing，
#         OCR 异常更是被吞不进 missing —— 本版默认 --skip-ocr 聚焦
#         MD5/硬链接/db 环节（OCR 失败本就不计入 missing）。
import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 输入书：由 MHI_DIAG_MD 指定（某书的 hybrid_auto/*.md）
MD = Path(os.environ.get("MHI_DIAG_MD", ""))
if not str(MD) or not MD.exists():
    sys.exit("用法: MHI_DIAG_MD=<某书的 hybrid_auto/*.md> python scripts/diag_images.py")
SRC = MD.parent

from src.core.mineru_postprocess import MineruPostprocessor
from src.core.image_pipeline import ImagePipeline


def main() -> int:
    ap = argparse.ArgumentParser(description="图片引用丢失断点诊断 v2")
    ap.add_argument(
        "--skip-ocr",
        action="store_true",
        help="跳过 OCR 步骤（诊断硬链接/MD5/db 环节，OCR 失败本就被吞不计 missing）",
    )
    args = ap.parse_args()

    text = MD.read_text(encoding="utf-8", errors="replace")
    proc = MineruPostprocessor()
    pipe = ImagePipeline()

    RE_IMG = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
    before = RE_IMG.findall(text)
    prep = proc.prepare_images(text, SRC)
    after = RE_IMG.findall(prep)

    print(f"引用总数: {len(before)}  去重: {len(set(before))}")
    print(f"prepare_images 后引用数: {len(after)}")
    rewritten = sum(1 for a, b in zip(before, after) if a != b)
    print(f"prepare_images 重写: {rewritten} 张")

    diff = [(a, b) for a, b in zip(before, after) if a != b][:5]
    for a, b in diff:
        print(f"  重写: {a} -> {b}")

    missing: list[str] = []
    err_counter: Counter = Counter()   # 异常聚合: 键 = "TypeName: message"
    err_examples: dict[str, str] = {}  # 每类异常的首个引用示例
    ok = 0
    for ref in dict.fromkeys(after):
        hit = pipe._find_image(ref, SRC)
        if hit is None:
            missing.append(ref)
            cand = SRC / ref
            print(f"MISS {ref}  实际文件存在={cand.exists()}")
            continue
        # 找到文件 → 跑完整单图处理链路（真实导入的 missing 正是从这里抛的）
        try:
            pipe._process_single_ref(ref, SRC, skip_ocr=args.skip_ocr)
            ok += 1
        except Exception as exc:
            key = f"{type(exc).__name__}: {exc}"
            err_counter[key] += 1
            err_examples.setdefault(key, ref)
            print(f"ERR  {ref}  ->  {key}")

    n_handled = ok + sum(err_counter.values())
    print(f"\n查找命中 {n_handled}  / 查找丢失 {len(missing)} / 去重引用 {len(set(after))}")
    print(f"处理链路异常 {sum(err_counter.values())} 张，按异常类型分布:")
    for key, n in err_counter.most_common():
        print(f"  {n:4d}  {key}")
        print(f"        例: {err_examples[key]}")
    if not err_counter:
        print("  无异常（处理链路全部成功）")

    img_dir = SRC / "images"
    if img_dir.is_dir():
        n = len(list(img_dir.iterdir()))
        print(f"images/ 目录实际文件数: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
