#!/usr/bin/env python3
"""Advanced Skill 端到端验证。

模拟宿主 Agent 的完整流程：
    CLI 取输入包 → (Agent 推理，这里用桩数据代替) → CLI 渲染

重点验证：
  1. 输入包结构正确、噪声已过滤、id 可引用
  2. 溯源硬校验：Agent 引用了不存在的 id 时必须被丢弃
  3. 渲染三种格式都保留溯源与推断标注
  4. 全程无任何 LLM SDK / API Key 依赖

用法:
    python verify_advanced.py --db-dir <db_storage> --keys keys.json
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


def run_cli(args: list[str], env: dict, stdin: str | None = None):
    p = subprocess.run([sys.executable, CLI] + args, capture_output=True,
                       text=True, env=env, input=stdin, timeout=300)
    return p.returncode, p.stdout, p.stderr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-dir", required=True)
    ap.add_argument("--keys", required=True)
    args = ap.parse_args()

    env = dict(os.environ)
    env["WECHAT_DB_DIR"] = args.db_dir
    env["WECHAT_KEYS"] = args.keys

    print("=" * 72)
    print(" Advanced Skill 端到端验证（Host Agent 推理模式）")
    print("=" * 72)

    checks: list[tuple[str, bool, str]] = []
    t0 = time.time()

    def chk(name, ok, note=""):
        checks.append((name, ok, note))
        return ok

    # ---- 0. 无 LLM 依赖 ----
    r = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util as u;"
         "mods=['anthropic','openai','google.generativeai'];"
         "print('|'.join(m for m in mods if u.find_spec(m)))"],
        capture_output=True, text=True, cwd=HERE)
    found = r.stdout.strip()
    chk("无任何 LLM SDK 依赖", found == "", f"发现: {found or '无'}")

    # ---- 1. status ----
    print("\n--- 1. status")
    code, out, _ = run_cli(["status"], env)
    st = json.loads(out) if out.strip() else {}
    chk("status 可运行", code == 0 and st.get("db_dir_exists"))
    print(f"    db_dir ok={st.get('db_dir_exists')} keys={st.get('keys_loaded')} 个")

    # ---- 2. summarize_chat 输入包 ----
    # 动态挑一个消息最多的会话，避免硬编码某台机器上的群名
    print("\n--- 2. summarize_chat 输入包")
    code, out, _ = run_cli(["sessions", "--limit", "30"], env)
    sess = json.loads(out).get("sessions", []) if out.strip() else []
    chat_name = None
    for s in sess:
        c, o, _ = run_cli(["count", s["username"]], env)
        if c == 0 and json.loads(o).get("count", 0) > 0:
            chat_name = s["username"]
            break
    if not chat_name:
        chk("会话输入包", False, "找不到任何有消息的会话")
        return 1
    print(f"    选中会话: {chat_name}")

    code, out, err = run_cli(["summarize", chat_name, "--since", "2026-09-27"], env)
    if code != 0:
        chk("会话输入包", False, f"exit={code} {out[:120]}")
        return 1
    pack = json.loads(out)
    chk("kind 正确", pack.get("kind") == "chat_summary_input", pack.get("kind"))
    chk("含 messages[]", bool(pack.get("messages")))
    chk("含 instructions", bool(pack.get("instructions", {}).get("output_schema")))
    chk("每条消息有稳定 id",
        all("id" in m and ":" in m["id"] for m in pack["messages"]))
    chk("已过滤低信息量",
        all(not m["low_value"] for m in pack["messages"]),
        f"排除 {pack.get('excluded_low_value')} 条")
    print(f"    {pack['chat']['display_name']}: {len(pack['messages'])} 条"
          f"（排除低信息量 {pack.get('excluded_low_value')} 条）")
    for m in pack["messages"][:3]:
        print(f"      {m['id']} {m['sender']}: {m['content'][:38]}")

    valid_ids = {m["id"] for m in pack["messages"]}

    # ---- 3. 桩：模拟 Agent 产出结论（含一个编造的 id）----
    print("\n--- 3. 模拟 Agent 结论 + 溯源校验")
    fake_id = "34567890123@chatroom:999999"     # 不存在，应被丢弃
    real_ids = list(valid_ids)[:2]
    agent_out = {
        "kind": "chat_summary",
        "chat": pack["chat"],
        "range": pack["range"],
        "overview": "桩数据：用于验证渲染与溯源管道。",
        "topics": [{
            "title": "桩话题",
            "category": "AI 工具",
            "summary": "桩摘要。",
            "key_points": ["桩要点"],
            "participants": ["桩参与者"],
            "source_message_ids": real_ids + [fake_id],
        }],
        "stats": pack["stats"],
    }
    with open("/tmp/_agent_chat.json", "w", encoding="utf-8") as fh:
        json.dump(agent_out, fh, ensure_ascii=False)
    with open("/tmp/_pack_chat.json", "w", encoding="utf-8") as fh:
        json.dump(pack, fh, ensure_ascii=False)

    # 渲染三种格式
    for fmt in ("html", "json"):
        code, out, _ = run_cli(["render", "/tmp/_agent_chat.json", "--format", fmt], env)
        ok = code == 0 and len(out) > 50
        chk(f"渲染 {fmt}", ok, f"{len(out)} 字符")

    # 溯源：带校验渲染，编造 id 必须被丢弃
    code, out, err = run_cli(["render", "/tmp/_agent_chat.json", "--format", "html",
                              "--verify-against", "/tmp/_pack_chat.json"], env)
    chk("溯源校验：编造 id 被丢弃", "999999" not in out,
        err.strip()[:60] if err.strip() else "")
    chk("溯源校验：真实 id 保留", real_ids[0] in out)

    # HTML 结构
    code, html, _ = run_cli(["render", "/tmp/_agent_chat.json", "--format", "html"], env)
    chk("HTML 结构完整",
        html.lstrip().startswith("<!DOCTYPE html") and "</html>" in html)

    # ---- 4. daily_digest 输入包 ----
    print("\n--- 4. daily_digest 输入包")
    today = time.strftime("%Y-%m-%d")
    code, out, err = run_cli(["summarize", "--since", today], env)
    if code != 0:
        chk("日报输入包", False, f"exit={code} {out[:120]}")
    else:
        dpack = json.loads(out)
        chk("kind 正确", dpack.get("kind") == "daily_digest_input")
        chk("含多个会话", len(dpack.get("chats", [])) >= 2,
            f"{len(dpack.get('chats', []))} 个")
        chk("含 categories 指引",
            bool(dpack.get("instructions", {}).get("categories")))
        all_ids = {m["id"] for c in dpack["chats"] for m in c["messages"]}
        chk("跨会话 id 唯一且带会话前缀",
            len(all_ids) == sum(len(c["messages"]) for c in dpack["chats"]))
        print(f"    {len(dpack['chats'])} 个会话 · "
              f"{dpack['stats']['message_count']} 条消息 · "
              f"低信息量 {dpack['stats']['low_value_count']} 条已过滤")
        for c in dpack["chats"][:4]:
            print(f"      {str(c['display_name'])[:22]:24s} {c['message_count']:4d} 条"
                  f"（排除 {c['excluded_low_value']}）")

        # 桩日报结论
        some = list(all_ids)[:2]
        digest_out = {
            "kind": "daily_digest",
            "range": dpack["range"],
            "overview": "桩数据：日报渲染验证。",
            "topics": [
                {"title": "桩话题一", "category": "AI 工具", "summary": "桩摘要一",
                 "chat": dpack["chats"][0]["display_name"],
                 "source_message_ids": some},
                {"title": "桩话题二", "category": "自媒体运营", "summary": "桩摘要二",
                 "chat": dpack["chats"][0]["display_name"],
                 "source_message_ids": some[:1]},
            ],
            "chats": dpack["chats"],
            "stats": dpack["stats"],
        }
        with open("/tmp/_agent_digest.json", "w", encoding="utf-8") as fh:
            json.dump(digest_out, fh, ensure_ascii=False)

        for fmt in ("html", "json"):
            code, out, _ = run_cli(["render", "/tmp/_agent_digest.json",
                                    "--format", fmt], env)
            chk(f"日报渲染 {fmt}", code == 0 and len(out) > 50, f"{len(out)} 字符")

        code, out, _ = run_cli(["render", "/tmp/_agent_digest.json",
                                "--format", "html"], env)
        chk("日报含分类标签", "AI 工具" in out and "自媒体" in out)
        chk("日报含来源会话", dpack["chats"][0]["display_name"] in out)

        # HTML 写文件
        code, out, _ = run_cli(["render", "/tmp/_agent_digest.json", "--format",
                                "html", "--output", "/tmp/_digest.html"], env)
        res = json.loads(out) if out.strip().startswith("{") else {}
        chk("HTML 可写文件", code == 0 and os.path.exists("/tmp/_digest.html"),
            f"{res.get('bytes', 0)} 字节")

    # ---- 5. render 走 stdin ----
    print("\n--- 5. render 支持 stdin")
    with open("/tmp/_agent_chat.json", encoding="utf-8") as fh:
        code, out, _ = run_cli(["render", "-", "--format", "html"], env,
                               stdin=fh.read())
    chk("render 可从 stdin 读", code == 0 and "<!DOCTYPE html" in out)

    # ---- 6. 错误处理 ----
    print("\n--- 6. 错误处理")
    code, out, _ = run_cli(["render", "/tmp/不存在的文件.json"], env)
    chk("render 文件不存在", code == 2 and "INVALID_PARAM" in out, f"exit={code}")

    code, out, _ = run_cli(["summarize", "不存在xyz123"], env)
    chk("summarize 会话不存在", code == 1 and "CHAT_NOT_FOUND" in out, f"exit={code}")

    # ---- 汇总 ----
    elapsed = time.time() - t0
    print("\n" + "=" * 72)
    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, note in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:34s} {note}")
    print(f"\n  {passed}/{len(checks)} 项通过  (耗时 {elapsed:.1f}s)")
    print("=" * 72)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
