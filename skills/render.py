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
        fmt: html（默认）| markdown | text | json
        messages_by_id: {消息id: 消息}，用于 HTML 的点击回溯
    """
    if fmt == "json":
        return json.dumps(data, ensure_ascii=False, indent=2)
    if fmt == "html":
        from .html_report import render_digest
        return render_digest(data, messages_by_id)
    if fmt == "markdown":
        return render_markdown(data)
    if fmt == "text":
        return render_text(data)
    raise ValueError(f"未知格式: {fmt}（只支持 html / markdown / text / json）")


def _iter_topics(data: dict) -> list[dict]:
    """结论里的话题（日报与单群共用 topics 结构）。"""
    return [t for t in (data.get("topics") or []) if isinstance(t, dict)]


def _summary_body(data: dict) -> list[str]:
    """话题 + 决定 + 待办，markdown 与 text 共用的主体行。"""
    out: list[str] = []
    topics = _iter_topics(data)
    if topics:
        out.append("话题：")
        for i, t in enumerate(topics, 1):
            title = t.get("title") or t.get("text") or ""
            cat = t.get("category")
            chat = t.get("chat")
            head = f"{i}. {title}"
            if cat:
                head += f"（{cat}）"
            if chat:
                head += f" — {chat}"
            out.append(head)
            if t.get("summary"):
                out.append(f"   {t['summary']}")
            for p in t.get("key_points") or []:
                out.append(f"   · {p}")
            refs = [r for r in (t.get("source_message_ids") or []) if r]
            if refs:
                out.append(f"   来源：{', '.join(refs)}")

    for label, key in (("决定", "decisions"), ("待办", "actions")):
        items = [x for x in (data.get(key) or []) if isinstance(x, dict)]
        if not items:
            continue
        out.append(f"{label}：")
        for x in items:
            txt = x.get("text") or x.get("title") or ""
            who = x.get("by") or x.get("owner")
            dash = f"（{'、'.join(who)}）" if isinstance(who, list) else (
                f"（{who}）" if who else "")
            out.append(f"· {txt}{dash}")
    return out


def render_text(data: dict) -> str:
    """紧凑纯文本 —— Agent 可读，也是聊天框回答的骨架。

    与 `summarize --format text`（消息行）不同，这里渲染的是
    **结论**（话题/决定/待办），不再含原始消息。
    """
    rng = data.get("range") or {}
    lines = []
    title = data.get("title") or (data.get("chat") or {}).get("display_name")
    if title:
        lines.append(f"【{title}】")
    if rng.get("since"):
        span = (str(rng.get("since")) if rng.get("since") == rng.get("until")
                else f"{rng.get('since')} ~ {rng.get('until')}")
        lines.append(f"范围：{span}")
    if data.get("overview"):
        lines.append("")
        lines.append(data["overview"])
    lines.append("")
    lines.extend(_summary_body(data))
    return "\n".join(lines).rstrip() + "\n"


def render_markdown(data: dict) -> str:
    """给人看的 Markdown（日报 / 分享用）。"""
    rng = data.get("range") or {}
    title = data.get("title") or (data.get("chat") or {}).get("display_name")
    if not title:
        title = f"微信日报 {rng.get('since') or ''}".strip()
    out = [f"# {title}", ""]
    if rng.get("since"):
        span = (str(rng.get("since")) if rng.get("since") == rng.get("until")
                else f"{rng.get('since')} ~ {rng.get('until')}")
        out.append(f"> 范围：{span}")
    if data.get("overview"):
        out += ["", data["overview"]]
    out.append("")

    topics = _iter_topics(data)
    if topics:
        out += ["## 话题", ""]
        for t in topics:
            head = f"### {t.get('title') or t.get('text') or ''}"
            meta = [x for x in (t.get("category"), t.get("chat")) if x]
            if meta:
                head += f"  `{' · '.join(meta)}`"
            out.append(head)
            if t.get("summary"):
                out += ["", t["summary"]]
            for p in t.get("key_points") or []:
                out.append(f"- {p}")
            refs = [r for r in (t.get("source_message_ids") or []) if r]
            if refs:
                out += ["", f"<sub>来源：{', '.join(refs)}</sub>"]
            out.append("")

    for label, key in (("决定", "decisions"), ("待办", "actions")):
        items = [x for x in (data.get(key) or []) if isinstance(x, dict)]
        if not items:
            continue
        out += [f"## {label}", ""]
        for x in items:
            txt = x.get("text") or x.get("title") or ""
            who = x.get("by") or x.get("owner")
            dash = f" —— {'、'.join(who)}" if isinstance(who, list) else (
                f" —— {who}" if who else "")
            out.append(f"- {txt}{dash}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


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

