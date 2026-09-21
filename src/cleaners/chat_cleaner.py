"""
文件名: chat_cleaner.py
摘要: AI 对话清洗器。
      输入 Chatbox 导出 Markdown（两种格式），
      清洗出干净的纯对话 Markdown + 摘要。
依赖: re
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# ── 格式检测 ──────────────────────────────────────────────

# 格式 A: **user** / **assistant** / **system** 标记
_RE_FORMAT_A_USER = re.compile(r"^\*\*user\*\*\s*:", re.IGNORECASE | re.MULTILINE)
_RE_FORMAT_A_ASSISTANT = re.compile(r"^\*\*assistant\*\*\s*:", re.IGNORECASE | re.MULTILINE)
_RE_FORMAT_A_SYSTEM = re.compile(r"^\*\*system\*\*\s*:", re.IGNORECASE | re.MULTILINE)

# 格式 B: ## 🧑‍💻 User / ## 🤖 Assistant 标题
_RE_FORMAT_B_USER = re.compile(r"^#{1,3}\s+[\U0001F9D1\u200D\U0001F4BB]?\s*User\b", re.IGNORECASE | re.MULTILINE)
_RE_FORMAT_B_ASSISTANT = re.compile(r"^#{1,3}\s+[\U0001F916]?\s*Assistant\b", re.IGNORECASE | re.MULTILINE)

# ── 清洗模式 ──────────────────────────────────────────────

# 工具调用 JSON 块
# Chatbox 有时用 ```json ... ``` 包裹工具调用
_RE_TOOL_CALL_JSON = re.compile(
    r"```json\s*\n.*?\"type\"\s*:\s*\"function\".*?\n```\s*\n?",
    re.DOTALL,
)
# 单独的 tool_calls 块（带参数）
_RE_TOOL_CALLS = re.compile(
    r"工具调用[：:].*?\n(?:.*?)\n```(?:json)?\n.*?\n```\s*\n?",
    re.DOTALL,
)

# 搜索结果原文、网页内容（大段引用）
_RE_LARGE_QUOTE_BLOCK = re.compile(
    r"搜索结果[：:].*?\n(?:>.+\n){3,}",
    re.DOTALL,
)
_RE_FETCHED_CONTENT = re.compile(
    r"(?:拉取|抓取|读取)(?:的)?(?:网页|URL|页面)(?:内容)?[：:].*?\n(?:>.+\n)+",
    re.DOTALL,
)

# AI 冗长推理铺垫
_RE_REASONING_PREAMBLE = re.compile(
    r"(好的[，,]\s*(?:我[来來]|让|这)|"
    r"让我(?:先|来|想想|查一查|搜索|看看|分析|思考)|"
    r"我先(?:查|看|想|确认|搜索)|"
    r"根据(?:我|您|你)(?:的)?(?:查询|问题|要求|需求)|"
    r"这是一个(?:很)?(?:有趣|好|复杂)的?问题|"
    r"关于这个问题|"
    r"首先[，,]\s*(?:我|让|我们)|"
    r"抱歉[，,]\s*(?:我|让|之前)|"
    r"对不起[，,]\s*(?:我|让|之前)|"
    r"我来(?:为|给)?你(?:详细|简单|重新)?(?:解释|介绍|回答|整理|总结|分析)|"
    r"让我来帮你|"
    r"你说得[对没错]|"
    r"你的理解(?:是)?(?:正确|很准确|没错|对的)|"
    r"这是个(?:很)?好的(?:问题|观察|观点|想法)|"
    r"这确实(?:是|一个)|"
    r"没问题[，,]\s*(?:我|让|这)|"
    r"当然可以[，,]\s*(?:我|让|这)|"
    r"很好的问题[！!。.，,]"
    r")",
    re.IGNORECASE,
)

# 对话摘要块
_RE_SUMMARY_BLOCK = re.compile(
    r"#+\s*(?:对话)?(?:摘要|总结|概述)[：:].*?(?:\n(?!\s*#).*)*",
    re.DOTALL | re.IGNORECASE,
)

# ── 格式检测辅助 ──────────────────────────────────────────


def detect_format(md_text: str) -> str:
    """检测对话格式: 'A' (旧), 'B' (新), 或 'unknown'。"""
    has_a_user = bool(_RE_FORMAT_A_USER.search(md_text))
    has_a_assistant = bool(_RE_FORMAT_A_ASSISTANT.search(md_text))
    has_b_user = bool(_RE_FORMAT_B_USER.search(md_text))
    has_b_assistant = bool(_RE_FORMAT_B_ASSISTANT.search(md_text))

    if has_a_user and has_a_assistant:
        return "A"
    if has_b_user and has_b_assistant:
        return "B"
    # 混合或单侧
    if has_a_user or has_a_assistant:
        return "A"
    if has_b_user or has_b_assistant:
        return "B"
    return "unknown"


# ── 清洗器 ────────────────────────────────────────────────


class ChatCleaner:
    """AI 对话清洗器。"""

    def clean(self, md_text: str) -> str:
        """清洗对话：移除 System Prompt、工具调用、搜索原文、推理铺垫。

        返回清洗后的纯文本 Markdown。
        """
        text = md_text

        # 1. 移除对话摘要块
        text = _RE_SUMMARY_BLOCK.sub("", text)

        # 2. 移除 System Prompt 块（格式 A 的 **system**: 块）
        text = self._remove_system_prompt(text)

        # 3. 移除工具调用 JSON
        text = _RE_TOOL_CALL_JSON.sub("", text)
        text = _RE_TOOL_CALLS.sub("", text)

        # 4. 移除搜索结果原文和抓取的网页内容
        text = _RE_LARGE_QUOTE_BLOCK.sub("", text)
        text = _RE_FETCHED_CONTENT.sub("", text)

        # 5. 移除 AI 冗长推理铺垫（仅在助手回复开头出现）
        text = self._remove_reasoning_preamble(text)

        # 6. 清理多余空行
        text = self._normalize_blank_lines(text)

        return text.strip()

    def clean_and_split(
        self, md_text: str
    ) -> tuple[str, list[dict[str, str]]]:
        """清洗并按轮次拆分对话。

        返回:
            cleaned_md: 清洗后的完整 Markdown。
            rounds: [{"role": "user"/"assistant", "content": "..."}, ...]
        """
        cleaned = self.clean(md_text)
        fmt = detect_format(md_text)
        rounds = self._split_rounds(cleaned, fmt)
        return cleaned, rounds

    def generate_summary(self, md_text: str) -> str:
        """基于规则生成摘要（≤200 字中文）。

        策略：提取首轮用户问题 + 主题切换点。
        """
        _, rounds = self.clean_and_split(md_text)

        if not rounds:
            return "（空对话）"

        parts: list[str] = []

        # 第一条用户消息
        for r in rounds:
            if r["role"] == "user" and r["content"].strip():
                first_msg = r["content"].strip()
                # 取前 100 字作为基础
                first_short = first_msg[:100]
                parts.append(first_short)
                break

        # 检测主题切换
        topic_switches = self._detect_topic_switches(rounds)
        for switch in topic_switches[:2]:
            parts.append(f"→ {switch[:60]}")

        summary = "；".join(parts)
        # 截断到 200 字
        if len(summary) > 200:
            summary = summary[:197] + "..."

        return summary

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _remove_system_prompt(self, text: str) -> str:
        """移除 System Prompt 区域。"""
        # 格式 A: **system**: ... 直到下一个人物标记或结尾
        lines = text.split("\n")
        keep = True
        result: list[str] = []
        for line in lines:
            stripped = line.strip()
            if _RE_FORMAT_A_SYSTEM.match(stripped):
                keep = False
                continue
            if keep:
                result.append(line)
            else:
                # 检查是否回到用户/助手标记
                if (_RE_FORMAT_A_USER.match(stripped)
                        or _RE_FORMAT_A_ASSISTANT.match(stripped)
                        or stripped.startswith("##")):
                    keep = True
                    result.append(line)
        return "\n".join(result)

    def _remove_reasoning_preamble(self, text: str) -> str:
        """移除助手回复开头的冗长推理铺垫。"""
        lines = text.split("\n")
        result: list[str] = []
        in_assistant = False
        preamble_skipped = False

        for line in lines:
            stripped = line.strip()

            # 检测助手回复开始
            if _RE_FORMAT_A_ASSISTANT.match(stripped) or _RE_FORMAT_B_ASSISTANT.match(stripped):
                in_assistant = True
                preamble_skipped = False
                result.append(line)
                continue

            if in_assistant and not preamble_skipped:
                # 跳过空行和推理铺垫
                if not stripped:
                    continue
                if _RE_REASONING_PREAMBLE.match(stripped):
                    preamble_skipped = True
                    continue
                # 非空非推理铺垫 → 结束跳过
                preamble_skipped = True
                result.append(line)
                continue

            # 检测新的用户/助手标记，重置状态
            if (_RE_FORMAT_A_USER.match(stripped)
                    or _RE_FORMAT_B_USER.match(stripped)):
                in_assistant = False
                preamble_skipped = False

            result.append(line)

        return "\n".join(result)

    def _split_rounds(
        self, cleaned: str, fmt: str
    ) -> list[dict[str, str]]:
        """按轮次拆分清洗后的对话。"""
        rounds: list[dict[str, str]] = []
        lines = cleaned.split("\n")
        current_role: str | None = None
        current_content: list[str] = []

        def flush():
            if current_role and current_content:
                content = "\n".join(current_content).strip()
                if content:
                    rounds.append({"role": current_role, "content": content})

        for line in lines:
            stripped = line.strip()
            if fmt == "A":
                user_match = _RE_FORMAT_A_USER.match(stripped)
                asst_match = _RE_FORMAT_A_ASSISTANT.match(stripped)
            else:
                user_match = _RE_FORMAT_B_USER.match(stripped)
                asst_match = _RE_FORMAT_B_ASSISTANT.match(stripped)

            if user_match:
                flush()
                current_role = "user"
                # 提取标记后的内容
                after_marker = stripped[user_match.end():].strip().lstrip(":")
                current_content = [after_marker] if after_marker else []
            elif asst_match:
                flush()
                current_role = "assistant"
                after_marker = stripped[asst_match.end():].strip().lstrip(":")
                current_content = [after_marker] if after_marker else []
            elif current_role:
                current_content.append(line)

        flush()
        return rounds

    @staticmethod
    def _detect_topic_switches(rounds: list[dict[str, str]]) -> list[str]:
        """简单主题切换检测：如果某个用户消息和之前差异大，记录下来。"""
        switches: list[str] = []
        prev_short = ""
        for r in rounds:
            if r["role"] == "user":
                content = r["content"].strip()
                if content and prev_short:
                    # 简单启发式：如果新消息开头和之前完全不同
                    cur_start = content[:30]
                    prev_start = prev_short[:30]
                    if cur_start and prev_start and cur_start != prev_start:
                        switches.append(content[:80])
                prev_short = content
        return switches

    @staticmethod
    def _normalize_blank_lines(text: str) -> str:
        """将连续 3 行以上空行压缩为 2 行。"""
        lines = text.split("\n")
        result: list[str] = []
        blank_count = 0
        for line in lines:
            if line.strip() == "":
                blank_count += 1
                if blank_count <= 2:
                    result.append(line)
            else:
                blank_count = 0
                result.append(line)
        return "\n".join(result)


# ── 便捷函数 ──────────────────────────────────────────────


def clean_chat_file(
    input_path: Path,
    output_path: Path | None = None,
    with_summary: bool = False,
) -> dict[str, Any]:
    """清洗单个对话文件。

    参数:
        input_path: 输入 .md 文件。
        output_path: 输出路径。None = 覆盖输入文件。
        with_summary: 是否额外生成并返回摘要。

    返回:
        {"cleaned_length": int, "summary": str (如适用)}
    """
    text = input_path.read_text(encoding="utf-8")
    cleaner = ChatCleaner()
    cleaned = cleaner.clean(text)

    out = output_path or input_path
    out.write_text(cleaned, encoding="utf-8")

    result: dict[str, Any] = {"cleaned_length": len(cleaned)}
    if with_summary:
        result["summary"] = cleaner.generate_summary(text)
    return result
