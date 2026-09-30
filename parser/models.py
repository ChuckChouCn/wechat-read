"""微信数据模型 — 稳定的对外 JSON 结构。

字段关系（全部在真实 4.1.15.6 数据库上验证）：

  会话  session.SessionTable.username
        ├─ session.Name2Id        username -> 会话序号
        ├─ message.Name2Id        rowid  <-> 发送者 wxid
        └─ message.Msg_<md5(会话名)[:32]>   该会话的消息表

  消息  Msg_*.real_sender_id ──rowid──> message.Name2Id.user_name (发送者 wxid)
        Msg_*.message_content   按 WCDB_CT_message_content 决定是否 zstd 压缩
        Msg_*.local_type        = 基础类型，或 (app_sub_type << 32) | 49

  联系人 contact.contact.username        wxid / 群名 / 公众号
         contact.chat_room.id            👈 chatroom_member.room_id
         contact.chatroom_member.member_id ──> contact.contact.id

  显示名优先级：remark（备注）> nick_name（昵称）> username
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any

# ============================================================
# 消息类型
# ============================================================
MSG_TYPE_TEXT = 1
MSG_TYPE_IMAGE = 3
MSG_TYPE_VOICE = 34
MSG_TYPE_CARD = 42
MSG_TYPE_VIDEO = 43
MSG_TYPE_STICKER = 47
MSG_TYPE_LOCATION = 48
MSG_TYPE_APP = 49        # 链接/文件/引用/接龙…，细分看 app_sub_type
MSG_TYPE_CALL = 50
MSG_TYPE_SYSTEM = 10000
MSG_TYPE_RECALL = 10002

BASE_TYPE_NAMES = {
    MSG_TYPE_TEXT: "text",
    MSG_TYPE_IMAGE: "image",
    MSG_TYPE_VOICE: "voice",
    MSG_TYPE_CARD: "card",
    MSG_TYPE_VIDEO: "video",
    MSG_TYPE_STICKER: "sticker",
    MSG_TYPE_LOCATION: "location",
    MSG_TYPE_APP: "app",
    MSG_TYPE_CALL: "call",
    MSG_TYPE_SYSTEM: "system",
    MSG_TYPE_RECALL: "recall",
}

# local_type = (sub << 32) | 49 时的 app 子类型
APP_SUB_TYPE_NAMES = {
    4: "link",
    5: "link",
    6: "file",
    8: "file",
    19: "chat_history",   # 合并转发
    24: "record",         # 转发记录/收藏
    51: "quote",
    53: "solitaire",      # 接龙
    57: "quote",          # 引用回复
    62: "quote",
    87: "live",
    2000: "transfer",
    2001: "red_packet",
}


def decode_local_type(local_type: int) -> tuple[int, int | None]:
    """把 local_type 拆成 (基础类型, app 子类型)。

    微信 4.x 对 app 消息把子类型塞进高 32 位：
        local_type = (sub_type << 32) | 49
    实测 59 个样本零例外。
    """
    if local_type is None:
        return 0, None
    lt = int(local_type)
    if lt > 0xFFFF:
        base = lt & 0xFFFF_FFFF
        sub = lt >> 32
        if base == MSG_TYPE_APP and sub:
            return MSG_TYPE_APP, sub
        # 其他高位编码一律按低 32 位取基础类型
        return base, None
    return lt, None


def type_name(local_type: int) -> str:
    base, sub = decode_local_type(local_type)
    if base == MSG_TYPE_APP and sub:
        return APP_SUB_TYPE_NAMES.get(sub, f"app_{sub}")
    return BASE_TYPE_NAMES.get(base, f"unknown_{base}")


# ============================================================
# 数据模型
# ============================================================
def _clean_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        try:
            v = bytes(v).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return None
    s = str(v)
    return s if s else None


@dataclass
class Contact:
    """联系人 / 群 / 公众号。"""
    username: str
    display_name: str | None = None
    nickname: str | None = None
    remark: str | None = None
    alias: str | None = None
    local_type: int | None = None
    is_group: bool = False
    is_official: bool = False
    member_count: int | None = None
    owner: dict | None = None          # 群主 {username, display_name}

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class Session:
    """会话（最近聊天列表的一项）。"""
    username: str
    display_name: str | None = None
    is_group: bool = False
    unread_count: int = 0
    summary: str | None = None
    last_timestamp: int | None = None      # 秒
    sort_timestamp: int | None = None      # 秒
    last_msg_type: str | None = None
    last_msg_sender: str | None = None
    last_sender_display_name: str | None = None
    is_hidden: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.last_timestamp:
            d["last_time"] = _iso(self.last_timestamp)
        if self.sort_timestamp:
            d["sort_time"] = _iso(self.sort_timestamp)
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class Message:
    """一条消息。"""
    local_id: int
    create_time: int | None = None         # 秒
    type: str = "unknown"
    local_type: int | None = None
    app_sub_type: int | None = None
    sender: str | None = None              # 发送者 wxid
    sender_name: str | None = None         # 发送者显示名
    is_sender_me: bool = False
    content: str | None = None
    content_truncated: bool = False
    chat: str | None = None                # 所属会话 username

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.create_time:
            d["time"] = _iso(self.create_time)
        return {k: v for k, v in d.items() if v is not None}


def _iso(ts: int) -> str | None:
    """秒级时间戳 -> ISO 本地时间字符串。"""
    import datetime
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return None


def table_for_chat(chat_username: str) -> str:
    """会话名 -> 消息表名。已验证 80/80 命中。"""
    return "Msg_" + hashlib.md5(chat_username.encode("utf-8")).hexdigest()
