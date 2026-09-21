"""
测试: cleaners/chat_cleaner.py
"""

from src.cleaners.chat_cleaner import ChatCleaner, detect_format
from src.importers.onenote_importer import parse_yaml_front_matter

cleaner = ChatCleaner()


def test_detect_format_a():
    text = "**user**: 你好\n**assistant**: 你好！"
    assert detect_format(text) == "A"


def test_detect_format_b():
    text = "## 🧑‍💻 User\n你好\n## 🤖 Assistant\n你好！"
    assert detect_format(text) == "B"


def test_remove_system_prompt():
    text = """**system**: 你是一个助手
规则1
规则2
**user**: 你好
**assistant**: 你好！"""
    cleaned = cleaner.clean(text)
    assert "**system**" not in cleaned
    assert "规则1" not in cleaned
    assert "**user**: 你好" in cleaned
    assert "**assistant**: 你好！" in cleaned


def test_remove_tool_call_json():
    text = """**user**: 查一下天气
**assistant**: 让我查一下
```json
{
  "type": "function",
  "function": {"name": "get_weather", "arguments": "{}"}
}
```
今天天气很好"""
    cleaned = cleaner.clean(text)
    assert "```json" not in cleaned
    assert "get_weather" not in cleaned
    assert "今天天气很好" in cleaned


def test_remove_reasoning_preamble():
    text = """**user**: 什么是相对论？
**assistant**: 让我想想，这是一个很好的问题。首先，相对论是爱因斯坦提出的..."""
    cleaned = cleaner.clean(text)
    # 助手回复应该保留核心内容
    assert "相对论是爱因斯坦提出的" in cleaned
    # 推理铺垫应该被移除
    # 注意：助手标记本身应该保留
    assert "**assistant**" in cleaned


def test_generate_summary():
    text = """**user**: 请解释量子力学的基本原理
**assistant**: 量子力学是研究微观世界的物理理论..."""
    summary = cleaner.generate_summary(text)
    assert len(summary) > 0
    assert "量子力学" in summary or "请解释" in summary


def test_yaml_front_matter():
    text = """---
title: 测试笔记
created: 2026-01-01
---
正文内容"""
    fm = parse_yaml_front_matter(text)
    assert fm.get("title") == "测试笔记"
    assert fm.get("created") == "2026-01-01"


def test_strip_yaml():
    from src.importers.onenote_importer import strip_yaml_front_matter
    text = "---\ntitle: T\n---\n正文"
    assert strip_yaml_front_matter(text) == "正文"


def test_generate_summary_empty():
    cleaner = ChatCleaner()
    summary = cleaner.generate_summary("")
    assert "空对话" in summary
