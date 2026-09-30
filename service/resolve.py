"""名称与时间解析。

Agent 拿到的是「张三」「工作群」这类**显示名**，而数据层只认 username。
这层负责把两者打通，并在歧义时给出候选而不是猜测。
"""
from __future__ import annotations

import datetime
import re

from .errors import ServiceError, ambiguous, invalid_param, not_found

# 已经是 wxid / 群名 / 系统账号的特征
_USERNAME_RE = re.compile(
    r"^(@?[\w\-]{6,64})$|"
    r"^gh_[0-9a-f]+$|"
    r"^wxid_[\w]+$|"
    r"^[\w\-]+@chatroom$|"
    r"^(notifymessage|weixin|fmessage|medianote|floatbottle|newsapp|"
    r"qqmail|tmessage|qmessage|officialaccounts|helper_entry)$"
)


def looks_like_username(s: str) -> bool:
    """粗判输入是否已经是 username（wxid / 群名 / 系统账号）。"""
    if not s:
        return False
    if s.endswith("@chatroom") or s.startswith("wxid_") or s.startswith("gh_"):
        return True
    return bool(re.match(r"^\d+@chatroom$", s))


def parse_time(value, *, end_of_day: bool = False) -> int | None:
    """把时间参数解析成 epoch 秒。

    接受：
      - None / ""            -> None
      - int / 纯数字字符串     -> 直接当 epoch 秒
      - "2026-09-29"         -> 当天 00:00:00（end_of_day=True 时 23:59:59）
      - "2026-09-29 11:50"   -> 精确到分
      - "2026-09-29 11:50:00"-> 精确到秒

    日期单独给时，start 取 00:00:00、end 取 23:59:59，符合"查一整天"的直觉。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise invalid_param(f"时间参数无效: {value!r}")
    if isinstance(value, (int, float)):
        return int(value)

    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)

    # 纯日期
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        t = datetime.time(23, 59, 59) if end_of_day else datetime.time(0, 0, 0)
        try:
            return int(datetime.datetime(y, mo, d, t.hour, t.minute, t.second).timestamp())
        except ValueError as exc:
            raise invalid_param(f"日期无效: {s} ({exc})") from exc

    # 日期 + 时间
    m = re.fullmatch(
        r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        y, mo, d, hh, mm, ss = m.groups()
        try:
            dt = datetime.datetime(int(y), int(mo), int(d), int(hh), int(mm),
                                   int(ss) if ss else 0)
            return int(dt.timestamp())
        except ValueError as exc:
            raise invalid_param(f"时间无效: {s} ({exc})") from exc

    raise invalid_param(f"无法解析时间: {s!r}（用 YYYY-MM-DD 或 epoch 秒）")


def iso(ts: int | None) -> str | None:
    """epoch 秒 -> 'YYYY-MM-DD HH:MM:SS'。"""
    if ts is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return None


def resolve_chat(store, name: str, *, need_messages: bool = False) -> dict:
    """把用户输入解析成确定的一个会话。

    依次尝试：精确 username -> 精确显示名 -> 模糊匹配。
    命中多个时抛 AMBIGUOUS_NAME 并给出候选，绝不猜。

    Returns:
        {"username", "display_name", "is_group"}
    """
    if not name or not str(name).strip():
        raise invalid_param("会话名不能为空")
    name = str(name).strip()

    # 1) 精确 username
    detail = store.contact(name)
    if detail:
        _ensure_messages(store, name, need_messages)
        return {"username": name,
                "display_name": detail.get("display_name") or name,
                "is_group": bool(detail.get("is_group"))}

    # 2) 在各会话中按显示名匹配
    sessions = store.sessions(limit=500, include_hidden=True)
    exact = [s for s in sessions if s.get("display_name") == name]
    if len(exact) == 1:
        _ensure_messages(store, exact[0]["username"], need_messages)
        return _as_chat(exact[0])

    # 3) 模糊匹配（同时覆盖会话与联系人）
    cands = [s for s in sessions if name.lower() in (s.get("display_name") or "").lower()]
    if not cands:
        for c in store.contacts(query=name, limit=50):
            cands.append({"username": c["username"],
                          "display_name": c.get("display_name") or c["username"],
                          "is_group": bool(c.get("is_group"))})
    else:
        # 备注/昵称恰好等于输入的联系人优先，避免 "张三" 被 "张三丰" 拖成歧义
        exact = [c for c in store.contacts(query=name, limit=50)
                 if c.get("remark") == name or c.get("nickname") == name]
        if exact:
            cands = [{"username": c["username"],
                      "display_name": c.get("display_name") or c["username"],
                      "is_group": bool(c.get("is_group"))} for c in exact]

    if len(cands) == 1:
        _ensure_messages(store, cands[0]["username"], need_messages)
        return _as_chat(cands[0])
    if len(cands) > 1:
        uniq = {c["username"]: c for c in cands}
        if len(uniq) == 1:
            c = next(iter(uniq.values()))
            _ensure_messages(store, c["username"], need_messages)
            return _as_chat(c)
        raise ambiguous("会话", name, [
            {"username": c["username"], "display_name": c.get("display_name")}
            for c in list(uniq.values())[:10]
        ])
    raise not_found("聊天对象", name)


def _as_chat(item: dict) -> dict:
    return {"username": item["username"],
            "display_name": item.get("display_name") or item["username"],
            "is_group": bool(item.get("is_group"))}


def _ensure_messages(store, username: str, need_messages: bool):
    """需要消息时才校验消息表存在，避免查联系人时误报。"""
    if not need_messages:
        return
    from .errors import ServiceError, NO_MESSAGE_DB
    if store.message_count(username) == 0:
        raise ServiceError(NO_MESSAGE_DB,
                           f"{store.display_name(username)} 没有消息记录")


def _prefer_exact(cands: list, name: str) -> list:
    """优先返回「备注或昵称恰好等于输入」的候选。

    用户说"张三"时，若存在备注就叫"张三"的人，就该直接命中；
    但因为还有"张三丰"这类模糊匹配，简单按数量判断会误报歧义。
    """
    exact = [c for c in cands
             if c.get("remark") == name or c.get("nickname") == name]
    return exact or cands


def resolve_contact(store, name: str) -> dict:
    """解析联系人（含群），返回 contact dict。"""
    if not name or not str(name).strip():
        raise invalid_param("联系人名不能为空")
    name = str(name).strip()

    # 1) 精确 username
    detail = store.contact(name)
    if detail:
        return detail

    # 2) 模糊搜索，但精确命中的优先
    cands = _prefer_exact(store.contacts(query=name, limit=50), name)
    if len(cands) == 1:
        return cands[0]
    if not cands:
        raise not_found("联系人", name)
    raise ambiguous("联系人", name, [
        {"username": c["username"], "display_name": c.get("display_name")}
        for c in cands[:10]
    ])
