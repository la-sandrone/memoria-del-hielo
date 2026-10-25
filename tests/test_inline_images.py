"""
测试: image_pipeline.materialize_inline_images —— MinerU v4 扁平 md 的内联 base64 物化。

不碰真库：monkeypatch get_db() 返回记录型替身，OCR 与可用性探测都打桩。
这样能把一条**关键不变式**钉成断言：物化阶段不得自增 ref_count（否则同一张图
会在随后的 process_image_refs 里被计两次）。
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from src.core import image_pipeline as ip

# 真 1×1 PNG（44 字节），用作确定性的内联载荷
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BlAAAEhQGAj0GZ6QAAAABJRU5ErkJggg=="
)


class _FakeDb:
    """记录型 DB 替身：只实现本方法用到的三条语句。"""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}
        self.calls: list[tuple[str, tuple]] = []

    def execute_sql(self, sql: str, params: tuple = ()):  # noqa: ANN201
        flat = " ".join(sql.split())
        self.calls.append((flat, params))
        if flat.startswith("SELECT ocr_text FROM images"):
            key = params[0]
            return [{"ocr_text": self.rows[key]}] if key in self.rows else []
        if flat.startswith("INSERT INTO images"):
            self.rows[params[0]] = params[2]
            return []
        return []

    def kinds(self) -> list[str]:
        out = []
        for flat, _ in self.calls:
            out.append(flat.split()[0].upper() + " " + flat.split()[1])
        return out


@pytest.fixture()
def pipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    p = ip.ImagePipeline()
    monkeypatch.setattr(p, "storage_dir", tmp_path / "images")
    monkeypatch.setattr(p, "ocr_enabled", True)
    monkeypatch.setattr(p, "ocr_endpoint", "http://stub/ocr")
    monkeypatch.setattr(p, "_ocr_available", lambda: True)
    ocr_calls: list[str] = []
    monkeypatch.setattr(p, "_ocr_image", lambda path: (ocr_calls.append(str(path)), "识别文本")[1])
    fake = _FakeDb()
    monkeypatch.setattr(ip, "get_db", lambda: fake)
    return p, fake, ocr_calls


def _uri(payload: bytes = PNG_1X1, alt: str = "", mime: str = "png") -> str:
    return f"![{alt}](data:image/{mime};base64,{base64.b64encode(payload).decode()})"


# ── 基本行为 ────────────────────────────────────────────


def test_no_data_uri_untouched(pipe):
    p, fake, _ = pipe
    text = "普通正文\n\n![](images/abc.jpg)\n"
    out, done, failed = p.materialize_inline_images(text)
    assert out == text and done == 0 and failed == 0
    assert fake.calls == []


def test_inline_image_written_and_ref_rewritten(pipe):
    p, fake, ocr = pipe
    out, done, failed = p.materialize_inline_images(f"前\n{_uri()}\n后\n")

    md5 = hashlib.md5(PNG_1X1).hexdigest()
    dest = p.storage_dir / md5
    assert done == 1 and failed == 0
    assert dest.is_file() and dest.read_bytes() == PNG_1X1, "落盘内容必须逐字节等于解码结果"
    assert f"]({dest})" in out, f"引用未改写成存储路径: {out!r}"
    assert "data:image" not in out, "base64 必须从正文里消失"
    assert out.startswith("前\n") and out.endswith("\n后\n"), "前后文必须原样保留"
    assert md5 in fake.rows, "必须登记 images 表"
    assert len(ocr) == 1, "OCR 只做一次"


def test_alt_text_preserved(pipe):
    p, _, _ = pipe
    out, _, _ = p.materialize_inline_images(_uri(alt="月背照片"))
    assert "![月背照片](" in out


# ── 关键不变式：不得重复计引用 ──────────────────────────


def test_materialize_never_bumps_ref_count(pipe):
    """物化阶段不得 UPDATE ref_count —— 那是 process_image_refs 的职责。

    否则同一张图会被计两次（物化一次 + 后续管线一次）。

    ⚠️ **必须走两条路径**：本方法有 INSERT（首次）与「命中已存在」两条分支，
    只在空库上测就只覆盖到前者。首版只调一次，用正对照（把 ref_count++ 注入
    命中分支）**测不出红** —— 判据是假的。现在两次调用各走一条。
    """
    p, fake, _ = pipe
    p.materialize_inline_images(_uri())      # 首次：走 INSERT 分支
    assert not any(k.startswith("UPDATE") for k in fake.kinds())

    p.materialize_inline_images(_uri())      # 二次：走「命中已存在」分支
    assert not any(k.startswith("UPDATE") for k in fake.kinds()), \
        f"命中分支里出现了 UPDATE: {fake.kinds()}"
    assert sum(1 for k in fake.kinds() if k == "INSERT INTO") == 1


def test_same_image_twice_ingested_once(pipe):
    p, fake, ocr = pipe
    out, done, failed = p.materialize_inline_images(_uri() + "\n" + _uri())
    md5 = hashlib.md5(PNG_1X1).hexdigest()
    assert done == 2 and failed == 0
    assert len(ocr) == 1, "同一张图只 OCR 一次"
    assert sum(1 for k in fake.kinds() if k == "INSERT INTO") == 1, "只插一行"
    assert out.count(f"]({p.storage_dir / md5})") == 2, "两处引用都要改写"


def test_existing_row_is_reused_without_reocr(pipe):
    p, fake, ocr = pipe
    md5 = hashlib.md5(PNG_1X1).hexdigest()
    fake.rows[md5] = "库里已有的 OCR"
    out, done, _ = p.materialize_inline_images(_uri())
    assert done == 1
    assert ocr == [], "已有记录就不该再 OCR"
    assert sum(1 for k in fake.kinds() if k == "INSERT INTO") == 0


# ── 失败与降级必须显式 ──────────────────────────────────


def test_malformed_base64_gets_visible_marker(pipe):
    """畸形 base64：**不保留原 data URI**（它是污染源），换成看得见的标记。"""
    p, fake, _ = pipe
    bad = "![x](data:image/png;base64,AAAAA)"      # 5 个字符 → 非法长度
    out, done, failed = p.materialize_inline_images(bad)
    assert done == 0 and failed == 1
    assert out == "![x](INLINE-IMAGE-DECODE-FAILED)"
    assert "data:image" not in out
    assert fake.rows == {}, "失败不得登记"


def test_ocr_unavailable_still_materializes(pipe, monkeypatch):
    """OCR 服务不可达是**可接受的局部降级**：照常落盘 + 登记，只是 ocr_text 为空。"""
    p, fake, ocr = pipe
    monkeypatch.setattr(p, "_ocr_available", lambda: False)
    out, done, failed = p.materialize_inline_images(_uri())
    assert done == 1 and failed == 0
    assert ocr == [] and out.count("data:image") == 0


def test_skip_ocr_flag(pipe):
    p, fake, ocr = pipe
    p.materialize_inline_images(_uri(), skip_ocr=True)
    assert ocr == [], "skip_ocr=True 时不得调 OCR"
    assert list(fake.rows.values()) == [""]


def test_stop_check_raises(pipe):
    p, _, _ = pipe
    with pytest.raises(KeyboardInterrupt):
        p.materialize_inline_images(_uri(), stop_check=lambda: True)


# ── 内联图政策（2026-09-29 用户裁定）：只落盘换引用，不 OCR ──────


class _ChainDb:
    """只为"物化 → 图片管线"这条链服务的替身。

    必须同时支持两种 SELECT 形状（一个列名、两个列名）—— 首版替身只认前者，
    于是 process_image_refs 一律查不到行、走了 INSERT 分支，把"复用已有记录"
    这条最该验的路径整个绕过去了。
    """

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}
        self.updates = 0
        self.inserts = 0

    def execute_sql(self, sql: str, params: tuple = ()):  # noqa: ANN201
        flat = " ".join(sql.split())
        if flat.startswith("SELECT ocr_text, file_path FROM images"):
            k = params[0]
            return ([{"ocr_text": self.rows[k], "file_path": ""}]
                    if k in self.rows else [])
        if flat.startswith("SELECT ocr_text FROM images"):
            k = params[0]
            return [{"ocr_text": self.rows[k]}] if k in self.rows else []
        if flat.startswith("UPDATE images"):
            self.updates += 1
        elif flat.startswith("INSERT INTO images"):
            self.inserts += 1
            self.rows[params[0]] = params[2]
        return []


@pytest.fixture()
def chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    p = ip.ImagePipeline()
    monkeypatch.setattr(p, "storage_dir", tmp_path / "images")
    monkeypatch.setattr(p, "ocr_enabled", True)
    monkeypatch.setattr(p, "ocr_endpoint", "http://stub/ocr")
    monkeypatch.setattr(p, "_ocr_available", lambda: True)
    ocr_calls: list[str] = []
    monkeypatch.setattr(
        p, "_ocr_image", lambda path: (ocr_calls.append(str(path)), "识别文本")[1]
    )
    fake = _ChainDb()
    monkeypatch.setattr(ip, "get_db", lambda: fake)
    return p, fake, ocr_calls


def test_inline_policy_is_no_ocr():
    """政策本身：内联 base64 图一律不 OCR（取代 D1 在该档上的口径）。

    断言一个常量看着弱，但它后面挂着一条**用户裁定**：D1 的"unknown ⟹ 保守
    OCR"对内联图作废，因为 flat 是唯一 unknown 的形态、而它恰好没有图片类型映射，
    照片/插图/公式全会被当文字读。这条断言是"不要再把它改回 True"的墓碑。
    """
    from src.importers.book_importer import _INLINE_IMAGE_OCR

    assert _INLINE_IMAGE_OCR is False, (
        "内联图 OCR 政策被改回 True —— 若确要改，请先复核 book_importer 里的理由"
    )


def test_skip_ocr_chain_leaves_no_ocr_line(chain):
    """决策甲的**后半句**：不 OCR 还不够，正文里也不能冒出 `> 📝 OCR:` 行。

    机制：物化写进 images 表的 ocr_text 是空串 ⟹ 随后 process_image_refs 命中
    该行拿到 ''，插行判据 `if raw_path in line and seen.get(raw_path)` 因空串为假
    而不插。本测试钉的就是这条链 —— 不是空串本身。

    顺带钉住"OCR 只做一次、且这一次也不做"：全过程 _ocr_image 零调用，
    images 表零 UPDATE（ref_count 由 process_image_refs 计，物化阶段不得动）。
    """
    p, fake, ocr = chain
    text = f"正文\n{_uri()}\n"
    materialized, done, failed = p.materialize_inline_images(text, skip_ocr=True)
    assert done == 1 and failed == 0 and ocr == []

    final = p.process_image_refs(materialized)
    md5 = hashlib.md5(PNG_1X1).hexdigest()
    assert f"]({p.storage_dir / md5})" in final, "引用必须还在（图片没被丢）"
    assert "📝 OCR" not in final, "不 OCR 就不该插 OCR 行"
    assert ocr == [], "整条链上一次 OCR 都不该发生"
    assert fake.inserts == 1 and fake.updates == 1, (
        "登记一次（物化）、计引用一次（图片管线）—— 不能重复计数"
    )
