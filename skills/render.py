"""Renderer —— 把 Agent 产出的结论渲染成 HTML。

core 输出的是稳定 JSON；renderer 只做呈现，不改数据。

只做 HTML：日报是对外分享/归档的产物，需要真正的排版。
其他格式（text/markdown/json）由调用方直接处理 JSON 即可。

设计细节见 html_report.py。
"""
from __future__ import annotations

import json


def render(data: dict, fmt: str = "html",
           messages_by_id: dict | None = None) -> str:
    """渲染。

    Args:
        fmt: html（默认）| json
        messages_by_id: {消息id: 消息}，用于 HTML 的点击回溯
    """
    if fmt == "json":
        return json.dumps(data, ensure_ascii=False, indent=2)
    if fmt == "html":
        from .html_report import render_digest
        return render_digest(data, messages_by_id)
    raise ValueError(f"未知格式: {fmt}（只支持 html / json）")


def _pack_ids(pack: dict) -> set[str]:
    """从输入包里收集所有合法消息 id。"""
    ids: set[str] = set()
    if pack.get("kind") == "daily_digest_input":
        for c in pack.get("chats", []):
            for m in c.get("messages", []):
                if m.get("id"):
                    ids.add(m["id"])
    else:
        for m in pack.get("messages", []):
            if m.get("id"):
                ids.add(m["id"])
    return ids


_REF_KEYS = ("source_message_ids",)


def verify_sources(data: dict, pack: dict) -> int:
    """丢弃结论里编造的 source_message_ids（原地修改），返回丢弃条数。

    Agent 有可能引用不存在的消息 id。渲染前用输入包做一次校验，
    只保留真实存在的 id —— 否则「可溯源」就是空话。
    """
    valid = _pack_ids(pack)
    if not valid:
        return 0

    dropped = 0

    def clean(item: dict):
        nonlocal dropped
        if not isinstance(item, dict):
            return
        for key in _REF_KEYS:
            ids = item.get(key)
            if not isinstance(ids, list):
                continue
            keep = [i for i in ids if i in valid]
            dropped += len(ids) - len(keep)
            if keep:
                item[key] = keep
            else:
                item.pop(key, None)

    for item in data.get("topics") or []:
        clean(item)

    # source_messages 也一并裁剪（若结论里带了）
    sm = data.get("source_messages")
    if isinstance(sm, list):
        kept = [m for m in sm if not m.get("id") or m["id"] in valid]
        dropped += len(sm) - len(kept)
        data["source_messages"] = kept

    return dropped


def _is_chat(data: dict) -> bool:
    """判断是会话摘要还是日报。

    兼容两种输入：Agent 产出的结论（带 kind/chat），
    以及 CLI 的输入包（带 kind=chat_summary_input）。
    """
    kind = str(data.get("kind") or (data.get("meta") or {}).get("skill") or "")
    if "digest" in kind:
        return False
    if "chat" in kind:
        return True
    return "chat" in data and "chats" not in data
