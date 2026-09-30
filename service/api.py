"""Service 层 — CLI 与 Skill 的统一门面。

三层职责：
  - Skill：告诉 Agent 何时用、怎么调（无业务逻辑）
  - CLI  ：参数校验 + JSON 输出 + 退出码
  - service（本层）：名称解析、时间解析、schema 稳定化、错误码

Skill 与 CLI 都只调这里，不直接碰 parser/db。

所有方法返回**纯 dict/list**（可直接 json.dumps），失败抛 ServiceError。
"""
from __future__ import annotations

from parser import WeChatStore

from .errors import ServiceError, invalid_param, not_found
from .resolve import iso, parse_time, resolve_chat, resolve_contact

# 消息类型过滤：对外名字 -> parser 的 type 名集合
TYPE_FILTERS = {
    "text": {"text"},
    "image": {"image"},
    "voice": {"voice"},
    "video": {"video"},
    "sticker": {"sticker"},
    "location": {"location"},
    "link": {"link"},
    "file": {"file"},
    "call": {"call"},
    "system": {"system"},
    "app": {"link", "file", "quote", "solitaire", "chat_history",
            "record", "transfer", "red_packet"},
}

CONTACT_TYPE_FILTERS = ("all", "person", "group")


class WeChatService:
    """CLI / Skill 的统一入口。

    用法::

        with WeChatService(db_dir, keys_path) as svc:
            svc.list_sessions(limit=10)
    """

    def __init__(self, db_dir: str, keys_path: str):
        self._store = WeChatStore(db_dir, keys_path)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self._store.close()

    # ================= 1. list_sessions =================
    def list_sessions(self, limit: int = 20, unread_only: bool = False,
                      include_hidden: bool = False) -> dict:
        limit = _check_limit(limit, 1, 500)
        rows = self._store.sessions(limit=limit, only_unread=unread_only,
                                    include_hidden=include_hidden)
        sessions = [{
            "username": r["username"],
            "display_name": r.get("display_name"),
            "is_group": r.get("is_group", False),
            "unread_count": r.get("unread_count", 0),
            "summary": r.get("summary"),
            "last_message_type": r.get("last_msg_type"),
            "last_sender": r.get("last_msg_sender"),
            "last_sender_name": r.get("last_sender_display_name"),
            "last_time": r.get("last_time"),
            "last_timestamp": r.get("last_timestamp"),
        } for r in rows]
        return {
            "sessions": sessions,
            "count": len(sessions),
            "has_more": len(sessions) >= limit,
        }

    # ================= 2. list_messages =================
    def list_messages(self, chat: str, limit: int = 50, offset: int = 0,
                      start_time=None, end_time=None, type: str | None = None,
                      content_limit: int = 200) -> dict:
        limit = _check_limit(limit, 1, 500)
        offset = _check_limit(offset, 0, 10 ** 9)
        content_limit = _check_limit(content_limit, 0, 100000)

        if type is not None and type not in TYPE_FILTERS:
            raise invalid_param(
                f"未知消息类型: {type!r}（可选: {', '.join(sorted(TYPE_FILTERS))}）")
        start_ts = parse_time(start_time)
        end_ts = parse_time(end_time, end_of_day=True)

        target = resolve_chat(self._store, chat, need_messages=True)

        # 类型过滤在内存里做（parser 不做类型过滤），因此多取一些再筛
        fetch = limit if type is None else min(limit * 8 + 50, 2000)
        rows = self._store.messages(
            target["username"], limit=fetch, offset=offset,
            start_time=start_ts, end_time=end_ts, content_limit=content_limit)

        if type is not None:
            allowed = TYPE_FILTERS[type]
            rows = [m for m in rows if m.get("type") in allowed][:limit]

        messages = [_clean_message(m) for m in rows]
        return {
            "chat": target,
            "messages": messages,
            "count": len(messages),
            "offset": offset,
            "start_time": iso(start_ts),
            "end_time": iso(end_ts),
            "type": type,
            "has_more": len(messages) >= limit,
        }

    # ================= 2b. list_messages_range（报表语义）=================
    def list_messages_range(self, chat: str, start_time=None, end_time=None,
                            content_limit: int = 0) -> dict:
        """取时间范围内的**全部**消息（不分页、不截断）。

        与 list_messages 的区别（重要）：

            list_messages       翻页语义 —— 取最近的 N 条，适合"看看最近聊了啥"
            list_messages_range 闭包语义 —— 区间内一条不落，适合日报/统计

        用翻页语义做日报会静默丢数据：当天 1000 条只拿到最新的 500 条，
        早晨的消息全没了。**不设上限、不截断**，区间内有多少给多少。

        返回值带 `audit` 对账信息，用于验证零丢失：
            sql_total      数据库里该范围的真实条数
            returned       实际返回的条数
            loss           丢失条数（应为 0）
            earliest/latest 实际覆盖的时间范围
        """
        start_ts = parse_time(start_time)
        end_ts = parse_time(end_time, end_of_day=True)
        target = resolve_chat(self._store, chat, need_messages=True)
        username = target["username"]

        rows = self._store.messages_range(
            username, start_time=start_ts, end_time=end_ts,
            content_limit=content_limit)
        messages = [_clean_message(m) for m in rows]

        # 对账：拿数据库的真实 count 比对，确认没丢
        sql_total = self._store.count_messages(username, start_ts, end_ts)
        times = [m["create_time"] for m in messages if m.get("create_time")]
        audit = {
            "sql_total": sql_total,
            "returned": len(messages),
            "loss": max(0, sql_total - len(messages)),
            "earliest_time": iso(min(times)) if times else None,
            "latest_time": iso(max(times)) if times else None,
            "range_since": iso(start_ts),
            "range_until": iso(end_ts),
        }
        return {"chat": target, "messages": messages,
                "count": len(messages), "audit": audit}

    # ================= 3. list_recent_messages =================
    def list_recent_messages(self, since=None, limit: int = 50,
                             chat: str | None = None, unread_only: bool = False,
                             content_limit: int = 200) -> dict:
        limit = _check_limit(limit, 1, 500)
        content_limit = _check_limit(content_limit, 0, 100000)
        since_ts = parse_time(since)

        chat_username = None
        if chat:
            # 这里不要求有消息表：可能是空的会话
            chat_username = resolve_chat(self._store, chat)["username"]

        rows = self._store.recent_messages(
            since=since_ts, limit=limit, chat=chat_username,
            unread_only=unread_only, content_limit=content_limit)

        messages = []
        for m in rows:
            item = _clean_message(m)
            item["chat"] = {
                "username": m.get("chat_username"),
                "display_name": m.get("chat_display_name"),
                "is_group": m.get("chat_is_group", False),
            }
            messages.append(item)

        latest = max((m["create_time"] for m in messages if m.get("create_time")),
                     default=None)
        return {
            "since": iso(since_ts),
            "messages": messages,
            "count": len(messages),
            "latest_time": iso(latest),
            "latest_timestamp": latest,
        }

    # ================= 4. search_contacts =================
    def search_contacts(self, query: str, limit: int = 20,
                        type: str = "all") -> dict:
        """搜联系人。type: all | person | group"""
        if not query or not str(query).strip():
            raise invalid_param("搜索关键词不能为空")
        if type not in CONTACT_TYPE_FILTERS:
            raise invalid_param(
                f"type 必须是 {'/'.join(CONTACT_TYPE_FILTERS)}，收到 {type!r}")
        limit = _check_limit(limit, 1, 500)

        groups_only = (type == "group")
        rows = self._store.contacts(query=str(query).strip(), limit=limit,
                                    groups_only=groups_only)
        if type == "person":
            rows = [c for c in rows if not c.get("is_group")]

        contacts = [_clean_contact(c) for c in rows]
        return {
            "query": query,
            "type": type,
            "contacts": contacts,
            "count": len(contacts),
        }

    # ================= 5. get_contact =================
    def get_contact(self, username: str) -> dict:
        return {"contact": _clean_contact(resolve_contact(self._store, username))}

    # ================= 6. list_group_members =================
    def list_group_members(self, group: str, limit: int = 500) -> dict:
        limit = _check_limit(limit, 1, 5000)
        detail = resolve_contact(self._store, group)

        if not detail.get("is_group"):
            raise invalid_param(
                f"{detail.get('display_name') or group} 不是群聊")

        username = detail["username"]
        members = self._store.members(username, limit=limit)
        owner = self._store.group_owner(username)
        return {
            "group": {
                "username": username,
                "display_name": detail.get("display_name"),
                "member_count": detail.get("member_count"),
            },
            "owner": owner,
            "members": [{
                "username": m["username"],
                "display_name": m.get("display_name"),
                "nickname": m.get("nickname"),
                "remark": m.get("remark"),
            } for m in members],
            "count": len(members),
            "has_more": len(members) >= limit,
        }

    # ================= 7. get_message_count =================
    def get_message_count(self, chat: str, start_time=None,
                          end_time=None) -> dict:
        start_ts = parse_time(start_time)
        end_ts = parse_time(end_time, end_of_day=True)
        target = resolve_chat(self._store, chat, need_messages=True)

        if start_ts is None and end_ts is None:
            count = self._store.message_count(target["username"])
        else:
            count = len(self._store.messages(
                target["username"], limit=10 ** 6, start_time=start_ts,
                end_time=end_ts, content_limit=0))

        return {
            "chat": target,
            "count": count,
            "start_time": iso(start_ts),
            "end_time": iso(end_ts),
        }


# ============================================================
# 辅助
# ============================================================
def _check_limit(v, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError) as exc:
        raise invalid_param(f"参数必须是整数，收到 {v!r}") from exc
    if not (lo <= n <= hi):
        raise invalid_param(f"参数需在 {lo}~{hi} 之间，收到 {n}（上限 {hi}）")
    return n


def _clean_message(m: dict) -> dict:
    """裁剪成稳定 schema，去掉内部字段。"""
    out = {
        "local_id": m.get("local_id"),
        "time": m.get("time"),
        "create_time": m.get("create_time"),
        "type": m.get("type"),
        "sender": m.get("sender"),
        "sender_name": m.get("sender_name"),
        "is_sender_me": m.get("is_sender_me", False),
        "content": m.get("content"),
    }
    if m.get("content_truncated"):
        out["content_truncated"] = True
    if m.get("meta"):
        out["meta"] = m["meta"]
    return {k: v for k, v in out.items() if v is not None}


def _clean_contact(c: dict) -> dict:
    out = {
        "username": c.get("username"),
        "display_name": c.get("display_name"),
        "nickname": c.get("nickname"),
        "remark": c.get("remark"),
        "alias": c.get("alias"),
        "is_group": c.get("is_group", False),
        "is_official": c.get("is_official", False),
        "member_count": c.get("member_count"),
        "owner": c.get("owner"),
    }
    return {k: v for k, v in out.items() if v is not None}
