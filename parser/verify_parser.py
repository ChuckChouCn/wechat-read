#!/usr/bin/env python3
"""parser/query 层端到端验证 — 用真实微信数据。

验证三个核心能力：会话列表、消息查询、联系人解析。
输出既是测试报告，也是 JSON 输出格式的实例。

用法:
    python -m parser.verify_parser --db-dir <db_storage> --keys keys.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import WeChatStore  # noqa: E402


def head(title: str):
    print("\n" + "=" * 74)
    print(f" {title}")
    print("=" * 74)


def show_json(obj, limit: int = 3, label: str = ""):
    """打印 JSON（列表只展示前 limit 项）。"""
    if isinstance(obj, list):
        print(f"  {label}返回 {len(obj)} 项，前 {min(limit, len(obj))} 项：")
        for item in obj[:limit]:
            print(json.dumps(item, ensure_ascii=False, indent=2))
            print()
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-dir", required=True)
    ap.add_argument("--keys", required=True)
    args = ap.parse_args()

    print("=" * 74)
    print(" parser/query 层验证 — 真实微信数据")
    print("=" * 74)
    print(f" 输入: db_dir={args.db_dir}")
    print(f"       keys={args.keys}")

    checks: list[tuple[str, bool, str]] = []
    t0 = time.time()

    with WeChatStore(args.db_dir, args.keys) as s:
        # ---------- 能力 1: 会话列表 ----------
        head("能力 1: 会话列表 sessions()")
        sessions = s.sessions(limit=8)
        ok = bool(sessions) and all(
            {"username", "display_name", "unread_count"} <= set(x) for x in sessions)
        checks.append(("会话列表", ok, f"{len(sessions)} 项"))
        print(f"  返回 {len(sessions)} 项")
        for x in sessions[:5]:
            flag = "群" if x.get("is_group") else "单"
            unread = f" [{x['unread_count']} 未读]" if x.get("unread_count") else ""
            print(f"    {flag} {x.get('display_name') or x['username']:<28s} "
                  f"{x.get('last_time',''):<20s}{unread}")
            print(f"       摘要: {str(x.get('summary'))[:60]}")

        # 未读筛选
        unread = s.sessions(limit=5, only_unread=True)
        checks.append(("未读筛选", all(x["unread_count"] > 0 for x in unread),
                       f"{len(unread)} 项"))
        print(f"\n  仅未读: {len(unread)} 项")

        # ---------- 能力 2: 消息查询 ----------
        head("能力 2: 消息查询 messages()")
        # 挑一个消息最多的会话
        best, best_n = None, 0
        for x in s.sessions(limit=30):
            n = s.message_count(x["username"])
            if n > best_n:
                best, best_n = x["username"], n
        print(f"  选取消息最多的会话: {s.display_name(best)} ({best}), {best_n} 条")

        msgs = s.messages(best, limit=8)
        ok_msg = bool(msgs) and all("content" in m and "type" in m for m in msgs)
        # 内容不应再是原始 XML
        no_xml = not any(str(m.get("content", "")).lstrip().startswith("<msg") for m in msgs)
        checks.append(("消息查询", ok_msg, f"{len(msgs)} 条"))
        checks.append(("内容已规整(无原始XML)", no_xml, ""))
        print(f"\n  最近 {len(msgs)} 条（时间升序）：")
        for m in msgs:
            who = m.get("sender_name") or m.get("sender") or "?"
            print(f"    [{m.get('time','?')}] ({m['type']}) {who}: "
                  f"{str(m.get('content'))[:70]}")

        # 类型分布
        all_msgs = s.messages(best, limit=200)
        types: dict[str, int] = {}
        for m in all_msgs:
            types[m["type"]] = types.get(m["type"], 0) + 1
        print(f"\n  近 {len(all_msgs)} 条的类型分布: "
              f"{dict(sorted(types.items(), key=lambda kv: -kv[1])[:8])}")

        # 时间范围筛选
        if msgs and msgs[-1].get("create_time"):
            t = msgs[-1]["create_time"]
            ranged = s.messages(best, limit=20, start_time=t)
            checks.append(("时间范围筛选", len(ranged) <= len(all_msgs),
                           f"{len(ranged)} 条"))
            print(f"  时间筛选 (>= {msgs[-1].get('time')}): {len(ranged)} 条")

        # 群消息发送者区分
        group = next((x["username"] for x in s.sessions(limit=30) if x.get("is_group")), None)
        if group:
            gm = s.messages(group, limit=5)
            senders = {m.get("sender") for m in gm if m.get("sender")}
            checks.append(("群消息发送者分辨", len(senders) > 0, f"{len(senders)} 个发送者"))
            print(f"\n  群 {s.display_name(group)} 的发送者区分: {len(senders)} 个不同 wxid")
            for m in gm[:3]:
                print(f"    {m.get('sender_name')}: {str(m.get('content'))[:55]}")

        # ---------- 能力 3: 联系人解析 ----------
        head("能力 3: 联系人解析 contacts() / contact() / members()")
        contacts = s.contacts(limit=10)
        ok_c = bool(contacts) and all("display_name" in c for c in contacts)
        checks.append(("联系人列表", ok_c, f"{len(contacts)} 项"))
        print(f"  返回 {len(contacts)} 项：")
        for c in contacts[:5]:
            kind = "群" if c.get("is_group") else ("公众号" if c.get("is_official") else "个人")
            mc = f" ({c['member_count']}人)" if c.get("member_count") else ""
            print(f"    [{kind}] {c.get('display_name'):<26s} {c['username']}{mc}")

        # 按关键字搜索
        kw = None
        for c in contacts:
            if c.get("remark"):
                kw = c["remark"][:2]
                break
        if kw:
            found = s.contacts(query=kw, limit=5)
            checks.append(("联系人搜索", len(found) > 0, f"关键字 {kw!r} → {len(found)} 项"))
            print(f"\n  搜索 {kw!r}: {len(found)} 项")
            for c in found[:3]:
                print(f"    {c.get('display_name')} ({c['username']}) "
                      f"备注={c.get('remark')} 昵称={c.get('nickname')}")

        # 单个联系人详情
        target = next((c["username"] for c in contacts if not c.get("is_group")), None)
        if target:
            detail = s.contact(target)
            checks.append(("联系人详情", bool(detail), target))
            print(f"\n  详情 {target}:")
            show_json(detail)

        # 群成员
        gw = next((c["username"] for c in s.contacts(groups_only=True, limit=5)), None)
        if gw:
            mem = s.members(gw, limit=6)
            checks.append(("群成员", len(mem) > 0, f"{len(mem)} 人"))
            print(f"\n  群 {s.display_name(gw)} 成员（前 {len(mem)}）：")
            for m in mem:
                print(f"    {m.get('display_name'):<26s} {m['username']}")

        # ---------- JSON 输出示例 ----------
        head("JSON 输出格式示例")
        print("  会话:")
        show_json(sessions[0] if sessions else {}, label="    ")
        if msgs:
            print("  消息:")
            show_json(msgs[-1])
        if contacts:
            print("  联系人:")
            show_json(contacts[0])

    elapsed = time.time() - t0

    head("验证结果")
    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, note in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<20s} {note}")
    print(f"\n  {passed}/{len(checks)} 项通过  (耗时 {elapsed:.1f}s)")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
