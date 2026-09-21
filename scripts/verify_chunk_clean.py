"""
文件名: verify_chunk_clean.py
摘要: 真实书回归冒烟 —— collapse_whitespace + chunk_markdown 全链路。
      遍历 <root>/<书>/auto/*.md，输出清洗折叠率、chunk 数、尺寸护栏、
      PUA 计数；异常捕获不中断（便于看完整本书）。
用法: python scripts/verify_chunk_clean.py <书籍根目录>（或设环境变量 MHI_VERIFY_ROOT）
依赖: cleaners.text_cleaner, core.chunker（项目内）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cleaners.text_cleaner import collapse_whitespace
from src.core.chunker import chunk_markdown
from src.core.unicode_utils import visible_len


def main(root: str) -> int:
    root_path = Path(root)
    md_files = sorted(root_path.rglob("*.md"))  # auto/、hybrid_auto/ 等全部命中
    if not md_files:
        print(f"未找到 auto/*.md: {root_path}")
        return 1

    header = (
        f"{'书':<26}{'原码点':>10}{'清码点':>10}{'折叠%':>7}"
        f"{'chunks':>7}{'最大可见':>8}{'最大总码':>8}{'PUA':>4}  状态"
    )
    print(header)
    print("-" * len(header))
    ok = True
    for f in md_files:
        name = f.parent.parent.name
        name = name if len(name) <= 26 else name[:23] + "..."
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            cleaned = collapse_whitespace(text)
            chunks = chunk_markdown(cleaned)
            orig_n, clean_n = len(text), len(cleaned)
            fold = 100.0 * (orig_n - clean_n) / orig_n if orig_n else 0.0
            max_v = max((visible_len(c.content) for c in chunks), default=0)
            max_t = max((len(c.content) for c in chunks), default=0)
            pua_n = sum(1 for c in chunks if c.has_pua)
            print(
                f"{name:<26}{orig_n:>10}{clean_n:>10}{fold:>6.1f}%"
                f"{len(chunks):>7}{max_v:>8}{max_t:>8}{pua_n:>4}  OK"
            )
        except Exception as exc:  # noqa: BLE001 —— 冒烟脚本：单书失败不中断
            ok = False
            print(f"{name:<26}{'-':>10}{'-':>10}{'-':>7}{'-':>7}{'-':>8}{'-':>8}{'-':>4}  FAIL: {exc}")
    print("-" * len(header))
    print("全部通过" if ok else "存在失败（见上）")
    return 0 if ok else 1


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MHI_VERIFY_ROOT", "")
    if not target:
        print("用法: python scripts/verify_chunk_clean.py <书籍根目录>（或设 MHI_VERIFY_ROOT）")
        sys.exit(2)
    sys.exit(main(target))
