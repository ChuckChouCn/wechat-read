#!/usr/bin/env python3
"""端到端验证：key-provider → db → parser → service → CLI。

真实调用 CLI 子进程，检查输出 JSON 与退出码，模拟 Agent 的实际调用方式。

用法:
    python verify_e2e.py --db-dir <db_storage> --keys keys.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "cli.py")


def run_cli(args: list[str], env: dict) -> tuple[int, dict | None]:
    """调用 CLI，返回 (exit_code, parsed_json)。"""
    proc = subprocess.run(
        [sys.executable, CLI] + args,
        capture_output=True, text=True, env=env, timeout=300)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.returncode, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-dir", required=True)
    ap.add_argument("--keys", required=True)
    args = ap.parse_args()

    env = dict(os.environ)
    env["WECHAT_DB_DIR"] = args.db_dir
    env["WECHAT_KEYS"] = args.keys

    print("=" * 72)
    print(" 端到端验证: key-provider → db → parser → service → CLI")
    print("=" * 72)
    print(f" CLI: {CLI}")
    print(f" DB : {args.db_dir}")

    checks: list[tuple[str, bool, str]] = []
    t0 = time.time()

    def chk(name, ok, note=""):
        checks.append((name, ok, note))
        return ok

    # ---- 0. 握手：sessions ----
    print("\n--- 0. sessions（握手）")
    code, data = run_cli(["sessions", "--limit", "5"], env)
    ok = code == 0 and data and data.get("sessions")
    chk("CLI 可运行 + JSON 合法", ok, f"exit={code}")
    if not ok:
        print("  FAIL 无法继续:", data)
        return 1
    sessions = data["sessions"]
    print(f"  {len(sessions)} 个会话")
    for s in sessions[:3]:
        print(f"    {s['display_name'][:26]:28s} 未读{s['unread_count']:3d}  {s['last_time']}")

    # schema 稳定性
    req = {"username", "display_name", "is_group", "unread_count", "last_time"}
    chk("sessions schema 稳定", req <= set(sessions[0]), f"缺 {req - set(sessions[0])}")

    # ---- 选一个消息最多的会话 ----
    target = None
    for s in sessions[:15]:
        code, d = run_cli(["count", s["username"]], env)
        if code == 0 and d.get("count", 0) > 0:
            target = d["chat"]
            n = d["count"]
            break
    chk("count 可用且解析出会话", target is not None,
        f"{target['display_name'] if target else '?'} = {n if target else 0} 条")
    print(f"\n  选中会话: {target['display_name']} ({n} 条)")

    # ---- 1. messages ----
    print("\n--- 1. messages")
    code, d = run_cli(["messages", target["username"], "--limit", "5"], env)
    ok = code == 0 and d.get("messages")
    chk("messages 返回消息", ok, f"exit={code} count={d.get('count') if d else 0}")
    if ok:
        for m in d["messages"][:3]:
            print(f"    [{m['time']}] ({m['type']}) {m.get('sender_name')}: "
                  f"{str(m.get('content'))[:46]}")
        # sender_name 对 system 消息（real_sender_id=0）可能为空，
        # 所以只断言必然存在的字段
        mreq = {"local_id", "time", "type", "content"}
        chk("messages schema 稳定", mreq <= set(d["messages"][0]),
            f"缺 {mreq - set(d['messages'][0])}")
        ts = [m["create_time"] for m in d["messages"]]
        chk("messages 时间升序", all(ts[i] <= ts[i + 1] for i in range(len(ts) - 1)))

    # ---- 2. messages 类型过滤 ----
    print("\n--- 2. messages --type")
    code, d2 = run_cli(["messages", target["username"], "--limit", "5",
                        "--type", "text"], env)
    ok = code == 0 and all(m["type"] == "text" for m in d2.get("messages", []))
    chk("--type text 只返回 text", ok, f"{d2.get('count') if d2 else 0} 条")

    # ---- 3. recent（无状态增量） ----
    print("\n--- 3. recent")
    code, d3 = run_cli(["recent", "--limit", "10"], env)
    ok = code == 0 and d3.get("messages")
    chk("recent 跨会话返回", ok, f"exit={code} count={d3.get('count') if d3 else 0}")
    if ok:
        chats = {m["chat"]["username"] for m in d3["messages"]}
        chk("recent 跨会话合并", len(chats) >= 1, f"{len(chats)} 个会话")
        chk("recent 含 chat 子对象", "chat" in d3["messages"][0])
        ts = [m["create_time"] for m in d3["messages"]]
        chk("recent 时间倒序", all(ts[i] >= ts[i + 1] for i in range(len(ts) - 1)))
        print(f"    最新: {d3['latest_time']}  覆盖 {len(chats)} 个会话")
        for m in d3["messages"][:3]:
            print(f"    [{m['time']}] {m['chat']['display_name'][:16]} "
                  f"{str(m.get('sender_name'))[:10]}: {str(m.get('content'))[:34]}")

        # 游标推进
        code, d3b = run_cli(["recent", "--since", d3["latest_time"], "--limit", "10"], env)
        newer = [m for m in d3b.get("messages", [])
                 if m["create_time"] > d3["latest_timestamp"]]
        chk("recent --since 游标生效", len(newer) == 0,
            f"{len(d3b.get('messages', []))} 条（应无更新）")

    # ---- 4. contacts ----
    print("\n--- 4. contacts")
    code, d4 = run_cli(["contacts", "search", "a", "--limit", "5"], env)
    chk("contacts search 可用", code == 0 and "contacts" in d4,
        f"{d4.get('count') if d4 else 0} 项")
    code, d4b = run_cli(["contacts", "search", "群", "--type", "group", "--limit", "3"], env)
    grps = d4b.get("contacts", [])
    chk("--type group 只返回群", all(c["is_group"] for c in grps),
        f"{len(grps)} 个群")

    # ---- 5. get_contact ----
    if grps:
        print("\n--- 5. contacts get")
        code, d5 = run_cli(["contacts", "get", grps[0]["username"]], env)
        c = d5.get("contact", {})
        chk("contacts get 返回详情", code == 0 and c.get("username") == grps[0]["username"])
        chk("群带 owner 字段", c.get("owner") is not None,
            f"owner={c.get('owner', {}).get('display_name') if c.get('owner') else None}")

    # ---- 6. members ----
    print("\n--- 6. members")
    if grps:
        code, d6 = run_cli(["members", grps[0]["username"], "--limit", "5"], env)
        ok = code == 0 and d6.get("members")
        chk("members 返回成员", ok, f"{d6.get('count') if d6 else 0} 人")
        if ok:
            chk("members 含 owner", d6.get("owner") is not None)
            print(f"    群: {d6['group']['display_name'][:24]}  "
                  f"owner={d6['owner']['display_name'] if d6.get('owner') else '-'}")
            for m in d6["members"][:3]:
                print(f"      {m['display_name']}")

    # ---- 7. 错误码契约 ----
    print("\n--- 7. 错误码契约")
    cases = [
        (["messages", "不存在xyz123"], "CHAT_NOT_FOUND", 1),
        (["sessions", "--limit", "9999"], "INVALID_PARAM", 2),
        (["messages", target["username"], "--type", "bogus"], "INVALID_PARAM", 2),
    ]

    # 歧义用例：动态找一个「会匹配到多个会话」的关键词，
    # 避免硬编码某台机器上的群名（换台机器跑就会失败）。
    ambig_kw = None
    names = [s["display_name"] for s in sessions if s.get("display_name")]
    for kw_len in (2, 3, 1):
        for name in names:
            if len(name) >= kw_len:
                kw = name[:kw_len]
                if sum(1 for n in names if kw in n) > 1:
                    ambig_kw = kw
                    break
        if ambig_kw:
            break
    if ambig_kw:
        cases.append((["messages", ambig_kw], "AMBIGUOUS_NAME", 1))
    else:
        print("    （本机会话名无歧义前缀，跳过 AMBIGUOUS_NAME 用例）")

    for argv, want_code, want_exit in cases:
        code, d = run_cli(argv, env)
        got = (d or {}).get("error", {}).get("code")
        ok = code == want_exit and got == want_code
        chk(f"错误 {want_code}", ok, f"exit={code} code={got}")

    # 缺环境变量
    env2 = dict(env); env2.pop("WECHAT_DB_DIR", None)
    code, d = run_cli(["--db-dir", "", "sessions"], env2)
    chk("错误 KEYS_MISSING", code == 3 and (d or {}).get("error", {}).get("code") == "KEYS_MISSING",
        f"exit={code}")

    # ---- 汇总 ----
    elapsed = time.time() - t0
    print("\n" + "=" * 72)
    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, note in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:32s} {note}")
    print(f"\n  {passed}/{len(checks)} 项通过  (耗时 {elapsed:.1f}s)")
    print("=" * 72)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
