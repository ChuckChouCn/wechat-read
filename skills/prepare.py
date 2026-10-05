"""摘要输入包 — 为宿主 Agent 准备确定性数据，本层不做任何推理。

职责边界（关键）：
  - **只做确定性加工**：取数（经 service）、噪声过滤、id 标注、分组、统计
  - **不做总结**：话题聚类、重点判断、结论归纳全部由宿主 Agent 完成
  - **不调用任何 LLM**：项目不绑定 LLM SDK，也不需要额外 API Key

产出「摘要输入包」（digest pack）：结构化消息 + 每条消息的稳定 id，
Agent 读 SKILL.md 的流程说明后，基于这个包产出最终结论。

架构：
    Host Agent → Skill instructions → CLI → service → parser → db

为什么由 CLI 而不是 Skill 做这一步：
  Skill 是给 Agent 看的说明文档，不能执行代码。数据准备必须是可运行的
  确定性程序，且要复用 service 层的名称解析与错误码。
"""
from __future__ import annotations

import datetime
import json
import re

from service import ServiceError, WeChatService

from .cache import SummaryCache

# ============================================================
# 低信息量过滤（确定性规则，不是判断）
# ============================================================
_NOISE_PATTERNS = [
    r"[\s\W]*",                                    # 纯符号/空白
    r"哈+|呵+|嘿+|嘻+|笑死|哈哈+",
    r"收到|好的|好|嗯+|哦+|行|可以|OK|ok|🆗|get|1|\+1|支持|赞|顶|同|了解|明白",
    r"\[表情\]|\[图片\]|\[语音[^\]]*\]|\[视频\]|\[动画表情\]",
    r"@\S+$",                                      # 单纯 @ 某人
]
_NOISE_RE = re.compile(r"^(?:" + "|".join(_NOISE_PATTERNS) + r")[!！。.~～]*$", re.I)

# 明显需要回应的信号
_REPLY_SIGNALS = re.compile(
    r"@所有人|@\S{2,}|请问|有没有人|谁能|求解|帮忙|投票|确认一下|"
    r"大家看|麻烦|请回复|回复我|在线等|怎么|为什么|什么时候|谁有|求推荐")

def _s(v) -> str | None:
    """安全转字符串：None 保持 None，不要把 'None' 当值写进数据结构。"""
    return None if v is None else str(v)


def is_low_value(content: str) -> bool:
    """低信息量消息判定（纯规则，Agent 可复核）。"""
    if not content:
        return True
    s = content.strip()
    if len(s) <= 1:
        return True
    return bool(_NOISE_RE.match(s))


def has_reply_signal(content: str) -> bool:
    """消息里是否有明显需要回应的信号。"""
    return bool(content) and bool(_REPLY_SIGNALS.search(content))


# ============================================================
# id 与引用
# ============================================================
def msg_id(m: dict, chat: dict | None = None) -> str:
    """稳定消息 id，格式 `username:local_id`，供 Agent 在结论里引用。"""
    c = chat or m.get("chat") or {}
    return f"{c.get('username') or '-'}:{m.get('local_id')}"


def _msg_entry(m: dict, chat: dict | None = None) -> dict:
    """把一条消息整理成给 Agent 的最小结构。"""
    c = chat or m.get("chat") or {}
    content = str(m.get("content") or "").strip()
    return {
        "id": msg_id(m, c),
        "time": m.get("time"),
        # create_time 供增量游标比对（time 是给人看的字符串，不便比较）
        "create_time": m.get("create_time"),
        "sender": m.get("sender_name") or m.get("sender"),
        "type": m.get("type"),
        "content": content,
        "low_value": is_low_value(content),
        "reply_signal": has_reply_signal(content),
    }


def _stats(entries: list[dict]) -> dict:
    types: dict[str, int] = {}
    senders: dict[str, int] = {}
    low = 0
    for e in entries:
        t = e.get("type") or "?"
        types[t] = types.get(t, 0) + 1
        s = e.get("sender")
        if s:
            senders[s] = senders.get(s, 0) + 1
        if e.get("low_value"):
            low += 1
    top = sorted(senders.items(), key=lambda kv: -kv[1])[:15]
    return {
        "message_count": len(entries),
        "low_value_count": low,
        "type_breakdown": dict(sorted(types.items(), key=lambda kv: -kv[1])),
        "top_senders": [{"name": n, "message_count": c} for n, c in top],
    }


def _audit(svc_audit: dict | None, entries: list[dict],
           fresh: list[dict]) -> dict:
    """对账：证明数据没被静默丢弃。

    这是「零丢失」的硬证据。Agent 和用户都能拿它对账：
      - loss 必须为 0
      - earliest_time 必须覆盖到时间范围的起点（比如"今天"要能看到 00:00 附近的）
    """
    times = [int(e["create_time"]) for e in entries if e.get("create_time")]
    out = {
        "sql_total": (svc_audit or {}).get("sql_total"),
        "fetched": len(entries),            # 从库里取到的
        "kept": len(fresh),                 # 实际交给模型处理的
        "deduped": len(entries) - len(fresh),  # 增量跳过的（已总结过）
        "earliest_time": _iso_ts(min(times)) if times else None,
        "latest_time": _iso_ts(max(times)) if times else None,
    }
    if svc_audit:
        out["range_since"] = svc_audit.get("range_since")
        out["range_until"] = svc_audit.get("range_until")
        # 丢失 = 库里有、但没取到。只有截断才会 >0
        out["loss"] = max(0, (svc_audit.get("sql_total") or 0) - len(entries))
        # 覆盖度自检：最早消息是否贴近范围起点。
        # 只作提示，不算"丢失" —— 真有可能一整个上午没人说话。
        if times and svc_audit.get("range_since"):
            try:
                start = datetime.datetime.strptime(
                    svc_audit["range_since"], "%Y-%m-%d %H:%M:%S").timestamp()
                gap_h = (min(times) - start) / 3600
                out["coverage"] = ("完整" if gap_h <= 1
                                   else f"范围内最早消息距起点 {gap_h:.1f} 小时"
                                        f"（可能该时段确实无消息）")
            except (ValueError, TypeError):
                pass
    # 零丢失判定：取到的 + 被噪声过滤的(仍会进 entries) == 库里总数
    if out.get("loss") == 0:
        out["zero_loss"] = True
    return out


def _iso_ts(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# 「不限时间」的哨兵值。传它当 since 表示从最早一条开始取。
ALL_TIME = 0


def _default_range(since, until) -> tuple:
    """补全时间范围。**单群和日报都必须走这里**，否则两条路的默认值会不一致。

    规则（与 `--since` 的 --help 文案一致）：

        since/until 都给        -> 原样用
        只给 since              -> until = since（当天）
        只给 until              -> since = 当天（截到 until 为止）
        都不给                  -> 今天一整天 00:00:00 ~ 23:59:59

    历史 bug：单群那条路**没有**这个补全，`since=None` 被原样传给
    list_messages_range，而 None 在那里表示「不限起始」—— 于是文档说
    「省略即今天」，底层却把一个群从建群到现在的全部消息都拉了回来
    （实测某群一次拉出 9 月 23 日以来的 3000+ 条）。
    """
    if since is None and until is None:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        return today, today
    if since is None:
        # 只给了 until：从"那一天"开始，不要一路回溯到建群
        return until, until
    if until is None:
        # 注意用 `is None` 而不是 falsy 判断：ALL_TIME(0) 是合法值，
        # 表示"不限起始"，不能顺手把 until 也设成 0（那会变成截止到 1970 年，
        # 一条都取不到）。
        if since == ALL_TIME:
            return ALL_TIME, None
        return since, since
    return since, until


# ============================================================
# summarize_chat 的输入包
# ============================================================
def chat_pack(svc: WeChatService, chat: str, since=None, until=None,
              include_low_value: bool = False,
              cache: "SummaryCache | None" = None,
              rebuild: bool = False) -> dict:
    """为「某个会话的摘要」准备输入包。

    cache 非 None 时启用增量：只给游标之后的新消息，并附已有摘要。

    Returns:
        结构化输入包，含 chat / range / messages[] / stats / 给 Agent 的指引。
        **不含任何总结内容** —— 那由宿主 Agent 产出。
    """
    # 补全时间范围：省略即今天（不能把 None 直接传下去 —— 那表示不限时间）
    since, until = _default_range(since, until)

    # 用「报表语义」全量取：区间内一条不落。
    # 不能用 list_messages —— 它是翻页语义（DESC LIMIT），
    # 当天消息多时会把早晨的静默丢掉。
    r = svc.list_messages_range(chat, start_time=since, end_time=until,
                                content_limit=0)
    chat_info, messages = r["chat"], r["messages"]
    if not messages:
        raise ServiceError("NO_MESSAGE_DB",
                           f"{chat_info['display_name']} 在该时间范围内没有消息")

    entries = [_msg_entry(m, chat_info) for m in messages]
    low = sum(1 for e in entries if e["low_value"])

    username = chat_info.get("username")
    prev_summary, cursor = None, 0
    if cache is not None and username and not rebuild:
        prev_summary = cache.summary(username)
        cursor = cache.cursor(username)

    fresh = entries
    if cursor:
        fresh = [e for e in entries
                 if int(e.get("create_time") or 0) > cursor]

    # 不做任何截断：区间内有多少条就给多少条。
    # 全量交给模型比丢消息好 —— 现代模型的上下文放得下，
    # 而截断会直接损失信息（曾经因此丢掉 89% 的消息）。
    kept = fresh if include_low_value else [e for e in fresh if not e["low_value"]]

    out = {
        "kind": "chat_summary_input",
        "truncated": False,
        "chat": chat_info,
        "range": {"since": messages[0].get("time"),
                  "until": messages[-1].get("time"),
                  "since_param": since, "until_param": until},
        "messages": kept,
        "message_count": len(entries),
        "new_message_count": len(fresh),
        "excluded_low_value": low,
        "reply_signals": [e["id"] for e in entries if e["reply_signal"]][:50],
        "stats": _stats(entries),
        "audit": _audit(r.get("audit"), entries, fresh),
        "instructions": _CHAT_INSTRUCTIONS,
    }
    if cache is not None:
        out["incremental"] = {
            "enabled": True,
            "has_previous": prev_summary is not None,
            "cursor": cursor,
            "new_messages": len(fresh),
            "total_messages": len(entries),
            "nothing_new": not kept and prev_summary is not None,
        }
        if prev_summary:
            out["previous_summary"] = prev_summary
    return out


# ============================================================
# daily_digest 的输入包
# ============================================================
def digest_pack(svc: WeChatService, since=None, until=None,
                include_low_value: bool = False,
                cache: "SummaryCache | None" = None,
                rebuild: bool = False) -> dict:
    """为「全部会话的日报」准备输入包。默认今天。

    **增量模式**（cache 非 None 时）：
        只把每个会话中「上次处理之后的新消息」放进 messages[]，
        并附上该会话已有的 summary。这样日报的输入体积不随历史增长。
        已有摘要里出现过的内容不必重新分析，Agent 只需处理新增部分。

    Args:
        cache: 摘要缓存；传入即启用增量
        rebuild: 忽略游标，从头处理（用户显式要求时用）
    """
    since, until = _default_range(since, until)

    messages = _fetch_all(svc, since, until)
    if not messages:
        raise ServiceError("NO_MESSAGE_DB", "该时间范围内没有任何消息")

    by_chat: dict[str, list[dict]] = {}
    meta: dict[str, dict] = {}
    for m in messages:
        c = m.get("chat") or {}
        u = c.get("username")
        if not u:
            continue
        by_chat.setdefault(u, []).append(m)
        meta.setdefault(u, c)

    chats, all_entries = [], []
    skipped_chats = 0
    new_total = 0

    for u, msgs in sorted(by_chat.items(), key=lambda kv: -len(kv[1])):
        c = meta[u]
        entries = [_msg_entry(m, c) for m in msgs]
        low = sum(1 for e in entries if e["low_value"])
        all_entries.extend(entries)

        prev_summary = None
        cursor = 0
        if cache is not None and not rebuild:
            prev_summary = cache.summary(u)
            cursor = cache.cursor(u)

        # 增量：只取游标之后的新消息
        fresh = entries
        if cursor:
            fresh = [e for e in entries
                     if int(e.get("create_time") or 0) > cursor]

        # 该会话本来就没有新消息、且已有摘要 → 不必进包，直接复用
        # （这正是省 token 的关键：老会话不再出现在输入里）
        # 不做任何截断（见 chat_pack 的说明）
        kept = fresh if include_low_value else [e for e in fresh if not e["low_value"]]

        if not kept:
            if prev_summary:
                skipped_chats += 1        # 已有摘要可复用，无需重算
                continue
            # 没有摘要也没有有效新消息 → 跳过
            continue

        new_total += len(kept)
        item = {
            "username": u,
            "display_name": c.get("display_name"),
            "is_group": c.get("is_group", False),
            "truncated": False,
            "message_count": len(entries),          # 范围内总条数
            "new_message_count": len(fresh),        # 其中未处理过的
            "excluded_low_value": low,
            "messages": kept,
            "reply_signals": [e["id"] for e in entries if e["reply_signal"]][:30],
        }
        if prev_summary:
            # 已有摘要一并给出：Agent 只需在原基础上补充新消息的内容
            item["previous_summary"] = prev_summary
        try:
            ctx = svc.list_messages_range(u, start_time=since, end_time=until,
                                          content_limit=0)
            item["audit"] = _audit(ctx.get("audit"), entries, fresh)
        except ServiceError:
            item["audit"] = _audit(None, entries, fresh)
        chats.append(item)

    # 已有摘要、无需重算的会话列出来，供 Agent 直接引用
    reusable = []
    if cache is not None:
        for u in by_chat:
            s = cache.summary(u)
            if s and not any(c["username"] == u for c in chats):
                reusable.append({
                    "username": u,
                    "display_name": (meta.get(u) or {}).get("display_name"),
                    "summary": s,
                })

    out = {
        "kind": "daily_digest_input",
        "range": {"since": _s(since), "until": _s(until)},
        "chats": chats,
        "stats": _stats(all_entries),
        "audit": _audit({"sql_total": len(all_entries),
                         "range_since": _s(since) + " 00:00:00"
                         if since and len(str(since)) == 10 else _s(since),
                         "range_until": _s(until)},
                        all_entries, all_entries),
        "instructions": _DIGEST_INSTRUCTIONS,
    }
    if cache is not None:
        out["incremental"] = {
            "enabled": True,
            "new_messages": new_total,
            "total_messages": len(all_entries),
            "chats_with_updates": len(chats),
            "chats_reused_from_cache": skipped_chats,
        }
        if reusable:
            out["cached_summaries"] = reusable
    return out


# ============================================================
# 紧凑文本形态（给宿主 Agent 读的）
# ============================================================
def _short_time(t) -> str:
    """`2026-09-29 14:20:01` -> `09-29 14:20`；无法识别时原样返回。"""
    s = str(t or "")
    if len(s) >= 16 and s[4] == "-" and s[13] == ":":
        return s[5:16]
    return s


def _one_line(s) -> str:
    """把内容压成单行 —— 保证「一条消息 = 一行」，Agent 才能按行翻页。"""
    return " ".join(str(s or "").split())


def _msg_line(e: dict) -> str:
    return (f"{e.get('id')} | {_short_time(e.get('time'))} | "
            f"{_one_line(e.get('sender'))} | {_one_line(e.get('content'))}")


def _cached_line(item: dict) -> str | None:
    """已有摘要（previous_summary / cached_summaries）压成一行 JSON。"""
    s = item.get("summary")
    name = item.get("display_name") or item.get("username")
    if not s:
        return None
    return f"#   已有摘要 {name}: " + json.dumps(
        s, ensure_ascii=False, separators=(",", ":"))


def compact_text(pack: dict) -> str:
    """把输入包压成「一条消息一行」的纯文本，供 Agent 直接读。

    为什么需要它：输入包的 JSON 形态（indent=2、每条 8 个字段）膨胀约 3.7 倍
    —— 1115 条消息 = 315 KB / 11229 行，超过 Agent 读文件工具的单次上限，
    Agent 读不进去只能自己写脚本切片。同内容的紧凑文本只有 85 KB / 1115 行，
    两次读得完，也就没有写脚本的必要了。

    行格式（每行一条，字段用 ` | ` 分隔，第 4 段起整段是内容）：

        <消息id> | <MM-DD HH:MM> | <发送者> | <内容>

    开头若干 `#` 行是元信息（范围、对账、统计），Agent 先读它们做完整性核对。
    """
    if pack.get("kind") == "daily_digest_input":
        return _compact_digest(pack)
    return _compact_chat(pack)


def _compact_chat(pack: dict) -> str:
    chat = pack.get("chat") or {}
    rng = pack.get("range") or {}
    audit = pack.get("audit") or {}
    st = pack.get("stats") or {}
    msgs = pack.get("messages") or []

    out = [
        f"# kind={pack.get('kind')}",
        f"# chat={chat.get('display_name')} ({chat.get('username')}) "
        f"group={1 if chat.get('is_group') else 0}",
        f"# range={rng.get('since')} ~ {rng.get('until')}  "
        f"messages={pack.get('message_count')} new={pack.get('new_message_count')} "
        f"excluded_low_value={pack.get('excluded_low_value')}",
        f"# audit: fetched={audit.get('fetched')} kept={audit.get('kept')} "
        f"loss={audit.get('loss')} zero_loss={audit.get('zero_loss')} "
        f"coverage={audit.get('coverage')}",
        f"# top_senders=" + ", ".join(
            f"{s.get('name')}({s.get('message_count')})"
            for s in (st.get("top_senders") or [])[:8]),
        f"# 每行一条：id | MM-DD HH:MM | 发送者 | 内容",
    ]
    if pack.get("incremental"):
        inc = pack["incremental"]
        out.append(f"# incremental: has_previous={inc.get('has_previous')} "
                   f"new_messages={inc.get('new_messages')} "
                   f"total_messages={inc.get('total_messages')}")
    if pack.get("previous_summary"):
        out.append("# 已有摘要（在其基础上补充，不要重新分析）: "
                   + json.dumps(pack["previous_summary"], ensure_ascii=False,
                                separators=(",", ":")))
    out.append("")
    out.extend(_msg_line(e) for e in msgs)
    return "\n".join(out) + "\n"


def _compact_digest(pack: dict) -> str:
    rng = pack.get("range") or {}
    audit = pack.get("audit") or {}
    st = pack.get("stats") or {}
    chats = pack.get("chats") or []

    total = sum(len(c.get("messages") or []) for c in chats)
    out = [
        f"# kind={pack.get('kind')}",
        f"# range={rng.get('since')} ~ {rng.get('until')}  "
        f"chats={len(chats)} messages={total}",
        f"# audit: fetched={audit.get('fetched')} kept={audit.get('kept')} "
        f"loss={audit.get('loss')} zero_loss={audit.get('zero_loss')}",
        f"# 每行一条：id | MM-DD HH:MM | 发送者 | 内容",
    ]
    if pack.get("incremental"):
        inc = pack["incremental"]
        out.append(f"# incremental: new_messages={inc.get('new_messages')} "
                   f"chats_with_updates={inc.get('chats_with_updates')} "
                   f"chats_reused_from_cache={inc.get('chats_reused_from_cache')}")
    for s in pack.get("cached_summaries") or []:
        line = _cached_line(s)
        if line:
            out.append(line)
    out.append("")

    for c in chats:
        out.append("")
        out.append(f"## {c.get('display_name')} ({c.get('username')}) "
                   f"group={1 if c.get('is_group') else 0} "
                   f"messages={c.get('message_count')} "
                   f"new={c.get('new_message_count')} "
                   f"excluded_low_value={c.get('excluded_low_value')}")
        if c.get("previous_summary"):
            out.append("#   已有摘要（在其基础上补充）: "
                       + json.dumps(c["previous_summary"], ensure_ascii=False,
                                    separators=(",", ":")))
        out.extend(_msg_line(e) for e in (c.get("messages") or []))
    return "\n".join(out) + "\n"


def _fetch_all(svc: WeChatService, since, until) -> list[dict]:
    """取时间范围内**所有会话**的消息，全量不截断。

    实现要点：遍历会话逐个用 list_messages_range 全量取，
    **不要**用 list_recent_messages —— 后者有 limit，会在会话之间
    抢预算，导致活跃群把其他会话挤掉，而且单群内部也会被截断。

    对没有消息表的会话（如「服务通知」）跳过，不中断整体。
    """
    msgs: list[dict] = []
    for s in svc.list_sessions(limit=500)["sessions"]:
        u = s["username"]
        try:
            r = svc.list_messages_range(u, start_time=since, end_time=until,
                                        content_limit=0)
        except ServiceError as exc:
            if exc.code in ("NO_MESSAGE_DB", "CHAT_NOT_FOUND"):
                continue
            raise
        for m in r["messages"]:
            m["chat"] = {"username": u,
                         "display_name": s.get("display_name"),
                         "is_group": s.get("is_group", False)}
            msgs.append(m)
    return msgs


# ============================================================
# 给宿主 Agent 的指引（也内嵌在包里，便于自描述）
# ============================================================
_CHAT_INSTRUCTIONS = {
    "goal": "把这个会话的消息做深度挖掘，按话题类型整理",
    "data_completeness": (
        "messages[] 是该时间范围内的**全部消息**，不是抽样。"
        "低信息量消息只是被标记 low_value，仍然在列表里。"
    ),
    "output_volume": (
        "话题数量按内容来，不设上限。一个活跃的群一天可能聊了十几件事，"
        "都要挖出来。少于 5 个通常意味着挖得不够深。"
    ),
    "no_importance_judgment": (
        "**不要判断重要性**，不要分「重点」，也不要写待办、需要回复 —— "
        "那是主观判断，容易出错。只做两件事：挖全、归类。"
    ),
    "topic_scope": (
        "**一个话题必须来自一段连续对话，时间跨度不超过 40 分钟。**\n"
        "聊天是流动的，上午聊的 A 和下午聊的 B 是两回事，要分开写。"
    ),
    "rules": [
        "一个话题 = 一段连续对话里的一件事（跨度 ≤ 40 分钟）",
        "每条带 category 和 source_message_ids",
        "只能引用 messages[] 里真实存在的 id",
    ],
    "categories": (
        "**按「聊的是哪个领域/主题」分类**（如 AI 工具、自媒体运营、"
        "大模型、行业动态），不是按内容形式。\n"
        "**3-6 个类，通常 3-4 个就够。** 同一领域的话题归到一起，"
        "不要为一个话题单开一类。"
    ),
    "output_schema": {
        "overview": "str，一句话总览",
        "topics": [{
            "title": "str", "category": "str", "summary": "str",
            "key_points": ["str"], "participants": ["str"],
            "source_message_ids": ["str"],
        }],
    },
}

_DIGEST_INSTRUCTIONS = {
    "goal": "把这一天的消息做深度挖掘，按话题类型整理成简报",
    "output_volume": (
        "目标 **15-40 个话题**。一天几百上千条消息里，"
        "值得记录的信息远不止三五个。少于 15 个通常意味着挖得不够深。"
    ),
    "no_importance_judgment": (
        "**不要判断重要性。** 不需要分「重点」和「其他」，"
        "也不要写待办、需要回复这类推断 —— 那是主观判断，容易出错。\n"
        "你只做两件事：**把内容挖全**，**按类型归类**。"
        "哪条对读者有用，让读者自己判断。"
    ),
    "digging": (
        "逐条扫过所有消息，把有信息量的都挖出来：\n"
        "  - 讨论（大家在聊什么、谁说了什么观点）\n"
        "  - 提问（谁在问什么、得到的回答是什么）\n"
        "  - 资源（分享的链接、工具、文章、教程）\n"
        "  - 经验（踩坑、心得、方法论）\n"
        "  - 事实（时间、地点、价格、规则、数字）\n"
        "  - 观点（金句、判断、看法）\n"
        "  - 动态（人员变动、约见、合作、发布）"
    ),
    "topic_scope": (
        "**一个话题必须来自一段连续对话，时间跨度不超过 40 分钟。**\n"
        "聊天是流动的：上午聊 A、下午聊 B，即使都关于「AI 工具」"
        "也是两个不同的话题，要分开写。\n"
        "判断依据是**对话的连贯性** —— 如果中间隔了几小时、"
        "或者话题已经转到别的事情上，那就是新话题。\n"
        "引用消息时只引用这一小段里的，不要把一天的消息混进一个话题。"
    ),
    "rules": [
        "一个话题 = 一段连续对话里的一件事（跨度 ≤ 40 分钟）",
        "每条带 category（领域分类）和 source_message_ids",
        "只能引用 chats[].messages[] 里真实存在的 id",
        "只有纯表情、纯符号、哈哈/收到/好的、无意义刷屏可以略过",
    ],
    "categories": (
        "**按「聊的是哪个领域/主题」分类，不是按内容形式。**\n"
        "\n"
        "  对：AI 工具、自媒体运营、大模型、创业变现、社群运营\n"
        "  错：讨论、提问、资源、经验、观点（这些是形式，不是领域）\n"
        "\n"
        "**一份日报只分 3-6 个类，通常 3-4 个就够。**\n"
        "分类名由你根据当天实际聊的内容归纳，用简短中文（2-6 字）。\n"
        "同一领域的话题必须归到同一个类；不要为一个话题单开一类。"
    ),
    "output_schema": {
        "overview": "str，两三句概括这一天在聊什么",
        "topics": [{
            "title": "str，短标题（一句话说清这件事）",
            "category": "str，上面 7 种之一",
            "summary": "str，一到两句展开说明",
            "key_points": ["str，具体要点，可省略"],
            "participants": ["str，涉及的人，可省略"],
            "source_message_ids": ["str"],
        }],
    },
}
