"""查询层 — 在解密后的数据库上做会话/消息/联系人查询。

设计要点：
  - 只读普通业务表，不碰 FTS / MMFtsTokenizer
  - 解密结果缓存到临时文件，一次会话内复用（避免反复解密 22MB 的 message_0.db）
  - 群聊消息内容带 "wxid_xxx:\n" 前缀，读取时剥离
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

from db import KeyStore, decrypt_file, find_db_files

from .content import parse_embedded_xml, parse_message_content
from .models import (
    Contact,
    MSG_TYPE_APP,
    MSG_TYPE_SYSTEM,
    Message,
    Session,
    decode_local_type,
    table_for_chat,
    type_name,
)

DEFAULT_CONTENT_LIMIT = 200


def _maybe_decompress(blob, ct_flag) -> str:
    """按 WCDB_CT_* 标记决定是否 zstd 解压，返回文本。"""
    if blob is None:
        return ""
    if isinstance(blob, (bytes, bytearray)):
        raw = bytes(blob)
        if ct_flag == 4 and raw[:4] == b"\x28\xb5\x2f\xfd":
            try:
                import zstandard  # noqa: PLC0415
                return zstandard.ZstdDecompressor().decompress(
                    raw, max_output_size=16 * 1024 * 1024).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return raw.decode("utf-8", "replace")
        return raw.decode("utf-8", "replace")
    return str(blob)


def strip_group_prefix(content: str, sender: str) -> str:
    """群聊消息体常以 "wxid_xxx:\\n" 开头，剥掉它只留正文。"""
    if not content or not sender:
        return content
    head = f"{sender}:"
    if content.startswith(head):
        return content[len(head):].lstrip("\r\n")
    # 也有些用显示名前缀
    if content.startswith(sender):
        rest = content[len(sender):]
        if rest.startswith(":") or rest.startswith("："):
            return rest[1:].lstrip("\r\n")
    return content


def _sender_from_meta(meta: dict | None) -> str | None:
    """从解析出的 meta 里兜底取发送者（部分消息 real_sender_id 为 0）。"""
    if not meta:
        return None
    return meta.get("from_user") or None


class WeChatStore:
    """解密后的微信数据库查询入口。

    用法::

        with WeChatStore(db_dir, keys_path) as s:
            s.sessions(limit=10)
            s.messages("12345678@chatroom", limit=20)
            s.contacts(query="张三")
    """

    def __init__(self, db_dir: str, keys_path: str):
        self.db_dir = db_dir
        self.keystore = KeyStore.load(keys_path)
        self._tmpdir = tempfile.mkdtemp(prefix="wxparser_")
        self._conns: dict[str, sqlite3.Connection] = {}
        self._names: dict[str, str] = {}          # wxid -> 显示名
        self._contacts_by_id: dict[int, dict] = {}  # contact.id -> contact 行
        self._loaded = False

    # ---------- 生命周期 ----------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        for con in self._conns.values():
            try:
                con.close()
            except sqlite3.Error:
                pass
        self._conns.clear()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ---------- 底层 ----------
    def _conn(self, rel: str) -> sqlite3.Connection | None:
        """解密并打开指定数据库（结果缓存）。"""
        if rel in self._conns:
            return self._conns[rel]
        # 允许传相对路径或文件名
        path = None
        for p in find_db_files(self.db_dir, min_size=4096):
            if p.replace("\\", "/").endswith(rel.replace("\\", "/")):
                path = p
                break
        if path is None:
            return None
        key = self.keystore.key_for_path(path)
        if key is None:
            return None
        res = decrypt_file(path, key, with_wal=True)
        if not res.ok:
            return None
        out = os.path.join(self._tmpdir, os.path.basename(path))
        with open(out, "wb") as fh:
            fh.write(res.data)
        con = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
        self._conns[rel] = con
        return con

    def _ensure_names(self):
        """建立 wxid -> 显示名 和 contact.id -> 行 的索引。"""
        if self._loaded:
            return
        self._loaded = True
        con = self._conn("contact/contact.db")
        if con is None:
            return
        for row in con.execute(
                "SELECT id, username, nick_name, remark, alias, local_type, "
                "big_head_url, small_head_url FROM contact"):
            cid, username, nick, remark, alias, ltype, big, small = row
            if not username:
                continue
            self._contacts_by_id[cid] = {
                "id": cid, "username": username, "nick_name": nick,
                "remark": remark, "alias": alias, "local_type": ltype,
                "big_head_url": big, "small_head_url": small,
            }
            # 备注名优先，其次昵称，最后 wxid
            self._names[username] = remark or nick or username

        # 群名也放进显示名表（群在 chat_room / contact 里都有）
        for row in con.execute("SELECT cr.username, c.nick_name FROM chat_room cr "
                               "LEFT JOIN contact c ON c.id=cr.id"):
            username, nick = row
            if username and nick:
                self._names.setdefault(username, nick)

    def display_name(self, username: str | None) -> str | None:
        if not username:
            return None
        self._ensure_names()
        return self._names.get(username, username)

    # ---------- 能力 1: 会话列表 ----------
    def sessions(self, limit: int = 20, include_hidden: bool = False,
                 only_unread: bool = False) -> list[dict]:
        con = self._conn("session/session.db")
        if con is None:
            return []
        self._ensure_names()

        sql = ("SELECT username, type, unread_count, summary, is_hidden, "
               "last_timestamp, sort_timestamp, last_msg_type, last_msg_sender, "
               "last_sender_display_name FROM SessionTable")
        where, params = [], []
        if not include_hidden:
            where.append("is_hidden=0")
        if only_unread:
            where.append("unread_count>0")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY sort_timestamp DESC LIMIT ?"
        params.append(int(limit))

        out = []
        for row in con.execute(sql, params):
            (username, stype, unread, summary, hidden, last_ts, sort_ts,
             last_type, last_sender, last_sender_name) = row
            if not username:
                continue
            is_group = username.endswith("@chatroom")
            s = Session(
                username=username,
                display_name=self.display_name(username),
                is_group=is_group,
                unread_count=unread or 0,
                summary=(summary or None),
                last_timestamp=last_ts,
                sort_timestamp=sort_ts,
                last_msg_type=type_name(last_type) if last_type is not None else None,
                last_msg_sender=last_sender or None,
                last_sender_display_name=(last_sender_name
                                          or self.display_name(last_sender)),
                is_hidden=bool(hidden),
            )
            out.append(s.to_dict())
        # 群名兜底（有些群只在 chat_room 里）
        for item in out:
            if item.get("display_name") == item["username"] and item["is_group"]:
                item["display_name"] = self._group_name(item["username"]) or item["username"]
        return out

    def _group_name(self, username: str) -> str | None:
        con = self._conn("contact/contact.db")
        if con is None:
            return None
        row = con.execute("SELECT nick_name FROM chat_room cr "
                          "JOIN contact c ON c.id=cr.id WHERE cr.username=?",
                          (username,)).fetchone()
        return row[0] if row and row[0] else None

    # ---------- 能力 2: 消息查询 ----------
    def count_messages(self, chat: str, start_time: int | None = None,
                       end_time: int | None = None) -> int:
        """按时间范围数消息条数（用 SQL count，不取内容，快）。

        用于「对账」：拿它和实际取到的条数比对，验证没有丢数据。
        """
        con = self._conn("message/message_0.db")
        if con is None:
            return 0
        table = table_for_chat(chat)
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                           (table,)).fetchone():
            return 0
        where, params = [], []
        if start_time is not None:
            where.append("create_time>=?")
            params.append(int(start_time))
        if end_time is not None:
            where.append("create_time<=?")
            params.append(int(end_time))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return con.execute(f'SELECT count(*) FROM "{table}"{clause}',
                           params).fetchone()[0]

    def messages_range(self, chat: str, start_time: int | None = None,
                       end_time: int | None = None,
                       content_limit: int = 0) -> list[dict]:
        """按时间范围取**全部**消息，时间正序。

        与 messages() 的关键区别（这是"报表统计"与"聊天翻页"的分野）：

            messages()      —— 翻页语义：ORDER BY DESC LIMIT N，取最近的 N 条
            messages_range()—— 闭包语义：时间段内的**全部**消息，一条不落

        日报/总结必须用后者。用翻页语义会让"当天 1000 条"只拿到最新的
        几百条，早晨的消息被静默丢弃。

        **不设条数上限、不截断内容。** 范围内有多少条就给多少条，
        消息正文也不截断 —— 总结要基于完整信息，任何截断都是信息损失。
        """
        con = self._conn("message/message_0.db")
        if con is None:
            return []
        self._ensure_names()

        table = table_for_chat(chat)
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                           (table,)).fetchone():
            return []

        where, params = [], []
        if start_time is not None:
            where.append("m.create_time>=?")
            params.append(int(start_time))
        if end_time is not None:
            where.append("m.create_time<=?")
            params.append(int(end_time))
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        # 时间正序，全量。用 sort_seq 排序保证与微信自己的顺序一致
        sql = (f"SELECT m.local_id, m.create_time, m.local_type, m.real_sender_id, "
               f"m.message_content, m.WCDB_CT_message_content, n.user_name "
               f"FROM \"{table}\" m LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id"
               f"{clause} ORDER BY m.sort_seq ASC")
        rows = con.execute(sql, params).fetchall()

        out = []
        for (local_id, ctime, ltype, sender_id, content, ct, sender_wxid) in rows:
            out.append(self._build_message(
                local_id, ctime, ltype, content, ct, sender_wxid,
                chat, content_limit))
        return out

    def messages(self, chat: str, limit: int = 50, offset: int = 0,
                 start_time: int | None = None, end_time: int | None = None,
                 content_limit: int = DEFAULT_CONTENT_LIMIT) -> list[dict]:
        """按会话名查消息，默认返回最近 limit 条（时间升序）。"""
        con = self._conn("message/message_0.db")
        if con is None:
            return []
        self._ensure_names()

        table = table_for_chat(chat)
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        if not exists:
            return []

        where, params = [], []
        if start_time is not None:
            where.append("m.create_time>=?")
            params.append(int(start_time))
        if end_time is not None:
            where.append("m.create_time<=?")
            params.append(int(end_time))
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        # 取最近 limit 条（倒序），再翻回升序输出
        sql = (f"SELECT m.local_id, m.create_time, m.local_type, m.real_sender_id, "
               f"m.message_content, m.WCDB_CT_message_content, n.user_name "
               f"FROM \"{table}\" m LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id"
               f"{clause} ORDER BY m.sort_seq DESC LIMIT ? OFFSET ?")
        params += [int(limit), int(offset)]
        rows = con.execute(sql, params).fetchall()

        out = []
        for (local_id, ctime, ltype, sender_id, content, ct, sender_wxid) in reversed(rows):
            out.append(self._build_message(local_id, ctime, ltype, content, ct,
                                           sender_wxid, chat, content_limit))
        return out

    def _build_message(self, local_id, ctime, ltype, content, ct, sender_wxid,
                       chat: str, content_limit: int) -> dict:
        """把一行消息记录构造成 dict。

        content 处理的顺序承重：
          1. 按 CT 标记解压
          2. 剥群消息前缀（必须在解析 XML 之前，否则前缀会让
             内容看起来不像 XML，解析器直接放弃）
          3. 解析 XML 成可读文本
          4. 渲染 ${wxid} 占位符
        """
        base, sub = decode_local_type(ltype)
        tname = type_name(ltype)
        raw = _maybe_decompress(content, ct)
        if sender_wxid:
            raw = strip_group_prefix(raw, sender_wxid)
            parsed = parse_message_content(raw, tname, sub)
        else:
            # 无发送者（system 消息等），用兜底解析器剥任意前缀，
            # 避免 XML 泄漏到 content
            parsed = parse_embedded_xml(raw, tname, sub)
        text = self._render_names(parsed["text"], parsed.get("meta"))
        truncated = False
        if content_limit and len(text) > content_limit:
            text = text[:content_limit]
            truncated = True

        # 发送者兜底：群消息体里的 fromusername 可补上缺失的发送者
        sender = sender_wxid or _sender_from_meta(parsed.get("meta"))
        m = Message(
            local_id=local_id,
            create_time=ctime,
            type=tname,
            local_type=base,
            app_sub_type=sub,
            sender=sender,
            sender_name=self.display_name(sender),
            content=text or None,
            content_truncated=truncated,
            chat=chat,
        )
        d = m.to_dict()
        if parsed.get("meta"):
            d["meta"] = parsed["meta"]
        return d

    def recent_messages(self, since: int | None = None, limit: int = 50,
                        chat: str | None = None, unread_only: bool = False,
                        content_limit: int = DEFAULT_CONTENT_LIMIT) -> list[dict]:
        """跨会话取最近消息（按时间倒序）。

        无状态设计：调用方传 since，用返回里的 latest_time 作为下次游标。
        不写任何磁盘状态 —— 确定性、可重放、并发安全（替代 wechat-cli 的
        new-messages 磁盘状态文件方案）。

        Args:
            since: epoch 秒，只取该时刻之后的消息；None 表示不限
            limit: 返回条数
            chat: 限定单个会话（username）
            unread_only: 只扫有未读的会话
        """
        # 选定候选会话：默认按最后消息时间取最近的一批
        targets = self.sessions(limit=200, only_unread=unread_only,
                                include_hidden=False)
        if chat:
            targets = [t for t in targets if t["username"] == chat]
            if not targets:
                targets = [{"username": chat}]

        collected: list[dict] = []
        for t in targets:
            u = t["username"]
            if self.message_count(u) == 0:
                continue
            batch = self.messages(u, limit=min(limit, 100),
                                  start_time=since, content_limit=content_limit)
            for m in batch:
                m["chat_username"] = u
                m["chat_display_name"] = t.get("display_name") or self.display_name(u)
                m["chat_is_group"] = u.endswith("@chatroom")
            collected.extend(batch)
            # 已够量且已覆盖到 since 之前的消息，可以早停
            if len(collected) >= limit * 3:
                break

        collected.sort(key=lambda m: m.get("create_time") or 0, reverse=True)
        return collected[:limit]

    def _render_names(self, text: str | None, meta: dict | None) -> str:
        """把文本里的 ${wxid_xxx} 占位符渲染成显示名。

        拍一拍等消息的模板用 wxid 占位，直接展示对用户没有意义。
        """
        if not text:
            return ""
        if "${" not in text:
            return text
        import re as _re

        def repl(m):
            return self.display_name(m.group(1)) or m.group(1)

        return _re.sub(r"\$\{([^}]+)\}", repl, text)

    def message_count(self, chat: str) -> int:
        con = self._conn("message/message_0.db")
        if con is None:
            return 0
        table = table_for_chat(chat)
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                           (table,)).fetchone():
            return 0
        return con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]

    # ---------- 能力 3: 联系人解析 ----------
    def contacts(self, query: str | None = None, limit: int = 20,
                 groups_only: bool = False) -> list[dict]:
        """按关键字搜联系人/群。匹配 wxid、昵称、备注、别名。"""
        con = self._conn("contact/contact.db")
        if con is None:
            return []
        self._ensure_names()

        sql = ("SELECT username, nick_name, remark, alias, local_type, "
               "chat_room_type FROM contact")
        where, params = [], []
        if query:
            like = f"%{query}%"
            where.append("(username LIKE ? OR nick_name LIKE ? OR remark LIKE ? "
                         "OR alias LIKE ?)")
            params += [like, like, like, like]
        if groups_only:
            where.append("username LIKE '%@chatroom'")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " LIMIT ?"
        params.append(int(limit))

        out = []
        for (username, nick, remark, alias, ltype, room_type) in con.execute(sql, params):
            if not username:
                continue
            is_group = username.endswith("@chatroom")
            c = Contact(
                username=username,
                display_name=remark or nick or username,
                nickname=nick or None,
                remark=remark or None,
                alias=alias or None,
                local_type=ltype,
                is_group=is_group,
                is_official=username.startswith("gh_"),
                member_count=self.member_count(username) if is_group else None,
                owner=self.group_owner(username) if is_group else None,
            )
            out.append(c.to_dict())
        return out

    def contact(self, username: str) -> dict | None:
        """按 username 取单个联系人详情。"""
        con = self._conn("contact/contact.db")
        if con is None:
            return None
        row = con.execute(
            "SELECT username, nick_name, remark, alias, local_type "
            "FROM contact WHERE username=?", (username,)).fetchone()
        if not row:
            return None
        username_, nick, remark, alias, ltype = row
        is_group = username_.endswith("@chatroom")
        c = Contact(
            username=username_,
            display_name=remark or nick or username_,
            nickname=nick or None,
            remark=remark or None,
            alias=alias or None,
            local_type=ltype,
            is_group=is_group,
            is_official=username_.startswith("gh_"),
            member_count=self.member_count(username_) if is_group else None,
            owner=self.group_owner(username_) if is_group else None,
        )
        return c.to_dict()

    def member_count(self, chat_username: str) -> int | None:
        con = self._conn("contact/contact.db")
        if con is None:
            return None
        row = con.execute(
            "SELECT count(*) FROM chatroom_member cm "
            "JOIN chat_room cr ON cr.id=cm.room_id WHERE cr.username=?",
            (chat_username,)).fetchone()
        return row[0] if row else None

    def group_owner(self, chat_username: str) -> dict | None:
        """群主。数据来自 chat_room.owner（实测 21/21 有值）。"""
        con = self._conn("contact/contact.db")
        if con is None:
            return None
        row = con.execute("SELECT owner FROM chat_room WHERE username=?",
                          (chat_username,)).fetchone()
        if not row or not row[0]:
            return None
        owner = row[0]
        return {"username": owner, "display_name": self.display_name(owner)}

    def members(self, chat_username: str, limit: int = 500) -> list[dict]:
        """群成员列表。"""
        con = self._conn("contact/contact.db")
        if con is None:
            return []
        self._ensure_names()
        sql = ("SELECT c.username, c.nick_name, c.remark FROM chatroom_member cm "
               "JOIN chat_room cr ON cr.id=cm.room_id "
               "JOIN contact c ON c.id=cm.member_id "
               "WHERE cr.username=? LIMIT ?")
        out = []
        for username, nick, remark in con.execute(sql, (chat_username, limit)):
            if not username:
                continue
            out.append({
                "username": username,
                "display_name": remark or nick or username,
                "nickname": nick or None,
                "remark": remark or None,
            })
        return out

    def resolve(self, username: str) -> dict:
        """把一个 wxid/群名解析成显示信息。"""
        self._ensure_names()
        return {
            "username": username,
            "display_name": self.display_name(username),
            "is_group": username.endswith("@chatroom"),
        }
