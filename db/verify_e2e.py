#!/usr/bin/env python3
"""端到端验证：真实密钥 + 真实微信数据库 → 可读 SQLite。

用 key-provider 提取的真实密钥，解密真实数据库（含 WAL 重放），
再用 SQLite 本身作为唯一裁判：quick_check + 表结构 + 实际数据行。

用法:
    python -m db.verify_e2e --db-dir <db_storage> --keys <keys.json> [--full]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import KeyStore, decrypt_file, find_db_files  # noqa: E402


MM_FTS_TOKENIZER = "MMFtsTokenizer"


def inspect(path: str, sample_tables: int = 3, sample_rows: int = 3) -> dict:
    """用 SQLite 打开解密结果，返回客观检查结果。

    微信的 FTS 库用自研分词器 `MMFtsTokenizer`，标准 SQLite 无法加载其
    虚拟表。这类库**解密是成功的**（文件头/页大小/schema 都正常），只是
    索引查不了 —— 单独标记为 unreadable_reason，不算解密失败。
    """
    info = {"quick_check": None, "tables": 0, "rows": {}, "samples": {},
            "error": None, "unreadable_reason": None}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        info["error"] = f"打开失败: {exc}"
        return info
    try:
        try:
            info["quick_check"] = con.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.OperationalError as exc:
            if MM_FTS_TOKENIZER in str(exc):
                info["unreadable_reason"] = f"微信自研分词器缺失 ({MM_FTS_TOKENIZER})"
                info["quick_check"] = "n/a (需微信分词器)"
            else:
                info["quick_check"] = f"ERR: {exc}"
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        info["tables"] = len(names)
        for t in names[:sample_tables]:
            try:
                n = con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                info["rows"][t] = n
                cur = con.execute(f'SELECT * FROM "{t}" LIMIT {sample_rows}')
                cols = [d[0] for d in cur.description]
                rows = []
                for row in cur.fetchall():
                    cells = []
                    for v in row:
                        s = repr(v)
                        cells.append(s if len(s) <= 60 else s[:57] + "...")
                    rows.append(dict(zip(cols, cells)))
                info["samples"][t] = {"columns": cols, "rows": rows}
            except sqlite3.Error as exc:
                info["rows"][t] = f"读取失败: {exc}"
    finally:
        con.close()
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-dir", required=True)
    ap.add_argument("--keys", required=True)
    ap.add_argument("--full", action="store_true", help="解密目录下全部数据库")
    ap.add_argument("--keep", default=None, help="把解密结果留在该目录")
    args = ap.parse_args()

    print("=" * 66)
    print(" 端到端验证: 真实密钥 + 真实微信数据库 → 可读 SQLite")
    print("=" * 66)

    ks = KeyStore.load(args.keys)
    print(f"\n[输入] 密钥文件: {args.keys}")
    print(f"       载入 {len(ks)} 个 salt 的密钥")
    print(f"[输入] 数据库目录: {args.db_dir}")

    all_dbs = find_db_files(args.db_dir, min_size=4096)
    print(f"       共 {len(all_dbs)} 个数据库")

    if args.full:
        targets = all_dbs
    else:
        # 默认挑代表性的: 会话、联系人、消息（最大）、以及最大的 WAL
        def pick(name_part):
            for p in all_dbs:
                if name_part in p.replace("\\", "/"):
                    return p
            return None

        targets = [t for t in (
            pick("session/session.db"),
            pick("contact/contact.db"),
            pick("message/message_0.db"),
        ) if t]
        if not targets:
            targets = all_dbs[:3]

    keep = args.keep
    tmpdir = None
    if keep:
        os.makedirs(keep, exist_ok=True)
    else:
        tmpdir = tempfile.mkdtemp(prefix="wxdec_")
        keep = tmpdir

    print(f"\n[输出] 解密结果目录: {keep}")
    print(f"       目标 {len(targets)} 个数据库\n")

    summary = []
    t0 = time.time()
    for path in targets:
        rel = os.path.relpath(path, args.db_dir)
        key = ks.key_for_path(path)
        if key is None:
            print(f"--- {rel}\n    [跳过] 没有对应密钥")
            summary.append((rel, False, "无密钥", {}))
            continue

        wal_path = path + "-wal"
        wal_sz = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
        out_path = os.path.join(keep, os.path.basename(path))

        r = decrypt_file(path, key, out_path, with_wal=True)
        if not r.ok:
            print(f"--- {rel}\n    [失败] {r.error}")
            summary.append((rel, False, r.error, {}))
            continue

        info = inspect(out_path)
        # 解密成功的判定：SQLite 认这个文件（quick_check ok），或只是缺微信
        # 自研分词器导致查不了 FTS。表数为 0 是合法的空库，不是失败。
        ok = info["quick_check"] == "ok" or info["unreadable_reason"] is not None

        print(f"--- {rel}")
        print(f"    源大小 {os.path.getsize(path)/1024:.0f}KB, "
              f"页数 {r.pages}, WAL {wal_sz/1024:.0f}KB → 重放 {r.wal_frames} 帧")
        print(f"    HMAC 校验: {'通过' if r.verified else '跳过'}  "
              f"quick_check: {info['quick_check']}  表数: {info['tables']}")
        if info["unreadable_reason"]:
            print(f"    注: {info['unreadable_reason']}（解密正常，仅索引不可查）")
        for t, n in info["rows"].items():
            print(f"      {t}: {n} 行")
        for t, d in info["samples"].items():
            print(f"      样例 [{t}] 列: {', '.join(d['columns'][:8])}")
            for row in d["rows"]:
                items = list(row.items())[:4]
                print("        " + " | ".join(f"{k}={v}" for k, v in items))
        summary.append((rel, ok, "ok" if ok else "校验未通过", info))

    elapsed = time.time() - t0

    print("\n" + "=" * 66)
    passed = sum(1 for _, ok, _, _ in summary if ok)
    print(f"结果: {passed}/{len(summary)} 个数据库解密并验证通过  (耗时 {elapsed:.1f}s)")
    print("=" * 66)
    for rel, ok, note, _ in summary:
        print(f"  {'PASS' if ok else 'FAIL'}  {rel}  {'' if ok else '(' + note + ')'}")

    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)
        print(f"\n(临时目录已清理: {tmpdir})")
    return 0 if passed == len(summary) and summary else 1


if __name__ == "__main__":
    sys.exit(main())
