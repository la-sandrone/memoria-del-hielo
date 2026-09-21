"""检查 PDF 拆卷工具可用性（qpdf / pdftk / pymupdf）。"""
import shutil
import sys

tools = {t: shutil.which(t) for t in ("qpdf", "pdftk", "pdfseparate", "gs")}
for t, p in tools.items():
    print(f"{t}: {p or '缺失'}")

try:
    import fitz
    print(f"pymupdf: OK ({fitz.__version__ if hasattr(fitz, '__version__') else '?'})")
except ImportError as e:
    print(f"pymupdf: 缺失 ({e})")
