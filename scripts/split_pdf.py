"""PDF 拆卷：qpdf --pages 按页范围切分，输出 <stem>_卷NN_pAAAA-BBBB.pdf。

用法: python scripts/split_pdf.py <pdf路径> [卷大小页数,默认400] [输出目录,默认<pdf同目录>/<stem>_split]

背景: MinerU hybrid 模式 VLM 引擎常驻 + 单文件内内存不释放（htop 实测），
3083 页巨型 PDF 处理到后半程内存挤爆 23.5G WSL MEM。拆卷后每卷独立进程、
卷内累积截断在 N 页量级，失败隔离。
"""
import shutil
import subprocess
import sys
from pathlib import Path


def main(pdf: str, vol_size: int = 400, out_dir: str | None = None) -> int:
    src = Path(pdf)
    if not src.is_file():
        print(f"PDF 不存在: {src}")
        return 1
    qpdf = shutil.which("qpdf")
    if not qpdf:
        print("qpdf 缺失")
        return 1

    npages = int(subprocess.check_output([qpdf, "--show-npages", str(src)]).strip())
    n_vols = (npages + vol_size - 1) // vol_size
    print(f"{src.name}: {npages} 页, 每卷 {vol_size} 页 → {n_vols} 卷")

    out_root = Path(out_dir) if out_dir else src.parent / (src.stem + "_split")
    out_root.mkdir(parents=True, exist_ok=True)

    for i, start in enumerate(range(1, npages + 1, vol_size), 1):
        end = min(start + vol_size - 1, npages)
        out = out_root / f"{src.stem}_卷{i:02d}_p{start:04d}-{end:04d}.pdf"
        subprocess.run(
            [qpdf, "--empty", "--pages", str(src), f"{start}-{end}", "--", str(out)],
            check=True,
        )
        print(f"  {out.name} ({end - start + 1} 页)")

    print(f"输出目录: {out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 400,
                  sys.argv[3] if len(sys.argv) > 3 else None))
