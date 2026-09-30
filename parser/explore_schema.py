#!/usr/bin/env python3
"""探查真实微信数据库的 schema 与字段关系。只读，不落盘。"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import KeyStore, decrypt_file, find_db_files  # noqa: E402

# 数据目录与密钥路径从环境变量读取（避免在源码里写死个人路径）
BASE = os.environ.get("WECHAT_DB_DIR", "")
KEYS = os.environ.get("WECHAT_KEYS", "keys.json")
ks = KeyStore.load(KEYS)

if not BASE:
    raise SystemExit(
        "请先设置 WECHAT_DB_DIR 环境变量：\n"
        "  export WECHAT_DB_DIR=/path/to/xwechat_files/<wxid>_<hash>/db_storage")


def open_plain(rel):
    p = os.path.join(BASE, rel)
    r = decrypt_file(p, ks.key_for_path(p), with_wal=True)
    if not r.ok:
        raise RuntimeError(f"{rel}: {r.error}")
    f = tempfile.mktemp(suffix=".db")
    open(f, "wb").write(r.data)
    return sqlite3.connect(f"file:{f}?mode=ro", uri=True), f


def show(rel, tables=None, ddl_for=None):
    con, f = open_plain(rel)
    print("=" * 78)
    print(f"### {rel}")
    print("=" * 78)
    names = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print(f"表数: {len(names)}")
    if tables:
        names = [n for n in names if n in tables]
    for n in names:
        sql = con.execute("SELECT sql FROM sqlite_master WHERE name=?", (n,)).fetchone()
        cnt = con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0]
        print(f"\n-- {n}  ({cnt} 行)")
        if ddl_for is None or n in ddl_for:
            print("   DDL:", (sql[0] or "").replace("\n", " ")[:400])
    con.close()
    os.remove(f)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"

    if what in ("all", "session"):
        show("session/session.db")
    if what in ("all", "contact"):
        show("contact/contact.db")
    if what in ("all", "msg"):
        show("message/message_0.db", tables=["Name2Id"], ddl_for=["Name2Id"])
        # 列出所有非 Msg_ 表
        con, f = open_plain("message/message_0.db")
        others = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'Msg\\_%' ESCAPE '\\' ORDER BY name")]
        msgs = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg\\_%' ESCAPE '\\'")]
        print(f"\n非 Msg_ 表 ({len(others)}): {others}")
        print(f"Msg_ 表数: {len(msgs)}, 样例: {msgs[:3]}")
        if msgs:
            t = msgs[0]
            print(f"\n-- {t} DDL:")
            print(con.execute("SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0])
            print(f"\n-- {t} 前 3 行:")
            cur = con.execute(f'SELECT * FROM "{t}" LIMIT 3')
            cols = [d[0] for d in cur.description]
            print("   列:", cols)
            for row in cur.fetchall():
                print("   ", [str(v)[:40] for v in row])
        con.close()
        os.remove(f)
