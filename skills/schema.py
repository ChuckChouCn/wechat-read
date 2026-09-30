"""Advanced Skill 的输入/输出 schema。

两个稳定契约：
  - ChatSummary  —— summarize_chat 的输出
  - DailyDigest  —— daily_digest 的输出

设计要点：
  1. **事实与推断分离**：每条结论带 `confidence`，`inferred=true` 的明确标记
  2. **可溯源**：所有话题/待办/决定都带 source_message_ids
  3. **不绑死渲染**：core 输出是纯 JSON；text/markdown/html 由 renderer 生成
  4. 话题先聚类再总结，不逐条复述
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

# ============================================================
# 溯源锚点
# ============================================================
@dataclass
class SourceRef:
    """一条结论的来源锚点。"""
    chat_username: str | None = None
    chat_display_name: str | None = None
    local_id: int | None = None
    time: str | None = None

    @property
    def key(self) -> str:
        return f"{self.chat_username or '-'}:{self.local_id}"

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ============================================================
# 通用条目
# ============================================================
@dataclass
class Topic:
    """一个话题（聚类后的）。"""
    title: str
    summary: str = ""
    key_points: list[str] = field(default_factory=list)
    participants: list[str] = field(default_factory=list)
    source_message_ids: list[str] = field(default_factory=list)
    # 事实 vs 推断
    inferred: bool = False
    confidence: str = "high"          # high | medium | low
    message_count: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, [], "")}


@dataclass
class ActionItem:
    """待办 / 约定。"""
    text: str
    owner: str | None = None
    due: str | None = None
    source_message_ids: list[str] = field(default_factory=list)
    inferred: bool = False
    confidence: str = "medium"

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, [], "")}


@dataclass
class Decision:
    """明确的结论 / 决定。"""
    text: str
    by: list[str] = field(default_factory=list)
    source_message_ids: list[str] = field(default_factory=list)
    inferred: bool = False
    confidence: str = "high"

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, [], "")}


# ============================================================
# summarize_chat 输出
# ============================================================
@dataclass
class ChatSummary:
    """summarize_chat 的输出。"""
    chat: dict = field(default_factory=dict)       # {username, display_name, is_group}
    range: dict = field(default_factory=dict)      # {since, until}
    overview: str = ""                             # 一句话总览
    topics: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    participants: list[dict] = field(default_factory=list)  # [{name, message_count}]
    stats: dict = field(default_factory=dict)      # {message_count, ...}
    source_messages: list[dict] = field(default_factory=list)  # 锚点展开
    # 生成元信息（含降级信息，调用方据此判断可信度）
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
# daily_digest 输出
# ============================================================
@dataclass
class DigestSection:
    """日报的一个分区（今日重点 / 重要通知 / 待办 / 需回复 / 值得关注）。"""
    title: str
    items: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"title": self.title, "items": self.items}


@dataclass
class DailyDigest:
    """daily_digest 的输出。"""
    range: dict = field(default_factory=dict)      # {since, until}
    overview: str = ""
    highlights: list[dict] = field(default_factory=list)    # 今日重点
    notices: list[dict] = field(default_factory=list)       # 重要通知
    actions: list[dict] = field(default_factory=list)       # 待办/约定
    needs_reply: list[dict] = field(default_factory=list)   # 需要回复
    topics: list[dict] = field(default_factory=list)        # 值得关注的话题
    chats: list[dict] = field(default_factory=list)         # 来源会话概览
    stats: dict = field(default_factory=dict)
    source_messages: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
# LLM 结构化输出用的 JSON Schema
# （传给 messages.parse 的 Pydantic 模型由 llm.py 定义）
# ============================================================
CHAT_SUMMARY_SCHEMA_HINT = """{
  "overview": str,                      // 一句话总览
  "topics": [{
    "title": str,                       // 话题标题（聚类命名）
    "summary": str,                     // 该话题核心内容 2-4 句
    "key_points": [str],                // 要点
    "participants": [str],              // 参与者显示名
    "source_message_ids": [str],        // 形如 "34567890123@chatroom:7484"
    "inferred": bool,                   // 是否为模型推断（非消息中明说）
    "confidence": "high"|"medium"|"low"
  }],
  "decisions": [{
    "text": str, "by": [str],
    "source_message_ids": [str], "inferred": bool, "confidence": str
  }],
  "actions": [{
    "text": str, "owner": str|null, "due": str|null,
    "source_message_ids": [str], "inferred": bool, "confidence": str
  }]
}"""

DIGEST_SCHEMA_HINT = """{
  "overview": str,
  "highlights": [{ "text": str, "chat": str, "source_message_ids": [str],
                   "inferred": bool, "confidence": str }],
  "notices":    [{ "text": str, "chat": str, "source_message_ids": [str],
                   "inferred": bool, "confidence": str }],
  "actions":    [{ "text": str, "owner": str|null, "due": str|null, "chat": str,
                   "source_message_ids": [str], "inferred": bool, "confidence": str }],
  "needs_reply":[{ "text": str, "chat": str, "why": str,
                   "source_message_ids": [str], "inferred": bool, "confidence": str }],
  "topics":     [{ "title": str, "summary": str, "chat": str, "participants": [str],
                   "source_message_ids": [str], "inferred": bool, "confidence": str }]
}"""
