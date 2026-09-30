"""parser/query 层 — 会话列表、消息查询、联系人解析。

对外入口：

    from parser import WeChatStore

    with WeChatStore(db_dir, "keys.json") as s:
        s.sessions(limit=10)          # 会话列表
        s.messages(chat, limit=20)    # 消息查询
        s.contacts(query="张三")         # 联系人解析

数据模型与字段关系见 models.py 顶部注释。
"""
from .models import (
    APP_SUB_TYPE_NAMES,
    BASE_TYPE_NAMES,
    Contact,
    Message,
    Session,
    decode_local_type,
    table_for_chat,
    type_name,
)
from .store import WeChatStore

__all__ = [
    "WeChatStore",
    "Contact", "Session", "Message",
    "type_name", "decode_local_type", "table_for_chat",
    "BASE_TYPE_NAMES", "APP_SUB_TYPE_NAMES",
]
