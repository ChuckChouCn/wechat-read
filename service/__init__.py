"""Service 层 — CLI 与 Skill 的统一门面。

    from service import WeChatService, ServiceError

    with WeChatService(db_dir, "keys.json") as svc:
        svc.list_sessions(limit=10)

职责：名称解析、时间解析、参数校验、稳定 schema、统一错误码。
Skill 与 CLI 都只调这里，不直接碰 parser/db。
"""
from .api import CONTACT_TYPE_FILTERS, TYPE_FILTERS, WeChatService
from .errors import (
    AMBIGUOUS_NAME,
    CHAT_NOT_FOUND,
    DB_DECRYPT_FAILED,
    INVALID_PARAM,
    KEYS_MISSING,
    NO_MESSAGE_DB,
    ServiceError,
)
from .resolve import iso, parse_time, resolve_chat, resolve_contact

__all__ = [
    "WeChatService",
    "ServiceError",
    # 错误码
    "CHAT_NOT_FOUND", "AMBIGUOUS_NAME", "NO_MESSAGE_DB",
    "KEYS_MISSING", "DB_DECRYPT_FAILED", "INVALID_PARAM",
    # 工具
    "parse_time", "iso", "resolve_chat", "resolve_contact",
    "TYPE_FILTERS", "CONTACT_TYPE_FILTERS",
]
