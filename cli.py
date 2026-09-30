#!/usr/bin/env python3
"""wechat CLI — 只读查询微信本地数据，恒 JSON 输出。

设计约束（见 docs/INTERFACE_DESIGN.md）：
  - **只输出 JSON**，不提供 --format text（输出必须稳定，Agent 才可信赖）
  - **不写任何磁盘状态**，游标由调用方持有
  - 不在本层做任何业务判断（总结、分类、重要消息识别）
  - 只调用 service 层，不直接碰 parser/db

用法::

    export WECHAT_DB_DIR="/path/to/xwechat_files/<wxid>_<hash>/db_storage"
    export WECHAT_KEYS="./keys.json"

    python cli.py sessions --limit 10
    python cli.py messages "群名" --limit 20
    python cli.py recent --since "2026-09-29 16:00:00"
    python cli.py contacts search "张三"
    python cli.py contacts get "张三"
    python cli.py members "群名"
    python cli.py count "群名"
"""
from __future__ import annotations

import argparse
import codecs
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from service import ServiceError, WeChatService  # noqa: E402
from service.errors import EXIT_INTERNAL, KEYS_MISSING  # noqa: E402


# ============================================================
# 编码
# ============================================================
def _force_utf8_stdio() -> None:
    """把 stdout/stderr 固定为 UTF-8。

    微信消息里有大量 emoji 和生僻字，而 Windows 控制台默认是 GBK(cp936)。
    不强制的话 `json.dump` 一遇到 emoji 就抛 UnicodeEncodeError，而且——
    输出被重定向到文件时（skill 里的 `> pack.json`）Python 默认
    errors='strict'，异常会让**整个文件一个字节都没写**。得到一个空文件，
    看起来像成功了，实际什么都没拿到。这是静默的数据丢失，比报错更糟。

    UTF-8 能编码任意 Unicode 字符，所以 errors='replace' 实际上不会触发，
    只是兜底防止任何编码把整次输出废掉。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # 已被替换成非 TextIOWrapper（如被测试框架捕获）时忽略
            pass


def _read_text_file(path: str) -> str:
    """读文本文件，容忍 BOM 与 UTF-16。

    Windows PowerShell 的 `>` 重定向会加 BOM：5.1 写 UTF-16LE+BOM，
    7.x 写 UTF-8+BOM。按朴素 utf-8 解码会分别报
    "Expecting property name" 和 "Unexpected UTF-8 BOM" ——
    都是让人摸不着头脑的错，而且看起来像内容本身有问题。
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")      # 有无 BOM 的 UTF-8 都能吃


# ============================================================
# 输出
# ============================================================
def emit(obj) -> None:
    """唯一输出口：JSON 到 stdout。"""
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def fail(err: ServiceError) -> int:
    json.dump(err.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return err.exit_code


# ============================================================
# 参数
# ============================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wechat",
        description="只读查询微信本地数据（恒 JSON 输出）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "首次使用:\n"
            "  python cli.py detect     # 自动查找微信数据目录\n"
            "  python cli.py init       # 提取并保存密钥（只做一次）\n"
            "  python cli.py status     # 检查是否就绪\n"
            "\n"
            "环境变量（可选，一般不用设）:\n"
            "  WECHAT_DB_DIR   微信 db_storage 目录（不设则自动探测）\n"
            "  WECHAT_KEYS     密钥路径（不设则用 ~/.weixin-read/keys.json）\n"
        ))
    p.add_argument("--db-dir", default=os.environ.get("WECHAT_DB_DIR", ""),
                   help="微信 db_storage 目录（不设则自动探测）")
    from db import KEYS_FILE

    p.add_argument("--keys", default=None,
                   help=f"密钥文件路径（默认用缓存 {KEYS_FILE}）")

    sub = p.add_subparsers(dest="cmd", required=True)

    # ---- sessions ----
    s = sub.add_parser("sessions", help="最近会话列表")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--unread-only", action="store_true", help="仅未读会话")
    s.add_argument("--include-hidden", action="store_true")

    # ---- messages ----
    m = sub.add_parser("messages", help="指定会话的消息记录")
    m.add_argument("chat", help="会话名或 wxid")
    m.add_argument("--limit", type=int, default=50)
    m.add_argument("--offset", type=int, default=0)
    m.add_argument("--start-time", default=None, help="YYYY-MM-DD [HH:MM[:SS]] 或 epoch")
    m.add_argument("--end-time", default=None, help="同上（含当天）")
    m.add_argument("--type", default=None, help="text/image/link/file…")
    m.add_argument("--content-limit", type=int, default=0,
                   help="内容截断长度；0=不截断（默认）")

    # ---- recent ----
    r = sub.add_parser("recent", help="跨会话最近消息（无状态）")
    r.add_argument("--since", default=None, help="只取该时刻之后；省略=最新 N 条")
    r.add_argument("--limit", type=int, default=50)
    r.add_argument("--chat", default=None, help="限定单个会话")
    r.add_argument("--unread-only", action="store_true")
    r.add_argument("--content-limit", type=int, default=200)

    # ---- contacts ----
    c = sub.add_parser("contacts", help="联系人")
    csub = c.add_subparsers(dest="csub", required=True)
    cs = csub.add_parser("search", help="搜索联系人")
    cs.add_argument("query")
    cs.add_argument("--limit", type=int, default=20)
    cs.add_argument("--type", default="all", choices=["all", "person", "group"])
    cg = csub.add_parser("get", help="联系人详情")
    cg.add_argument("username", help="wxid 或显示名")

    # ---- members ----
    g = sub.add_parser("members", help="群成员列表")
    g.add_argument("group", help="群名或群 wxid")
    g.add_argument("--limit", type=int, default=500)

    # ---- count ----
    n = sub.add_parser("count", help="消息计数")
    n.add_argument("chat", help="会话名或 wxid")
    n.add_argument("--start-time", default=None)
    n.add_argument("--end-time", default=None)

    # ---- summarize (advanced) ----
    # 不指定 chat -> digest pack；指定 chat -> chat pack
    # 输出的是「摘要输入包」，总结由宿主 Agent 完成
    z = sub.add_parser("summarize", help="准备摘要输入包（供宿主 Agent 推理）")
    z.add_argument("chat", nargs="?", default=None,
                   help="会话名或 wxid；省略则准备全部会话的日报输入包")
    z.add_argument("--since", default=None,
                   help="起始 YYYY-MM-DD 或 epoch 秒；省略=今天")
    z.add_argument("--until", default=None,
                   help="结束 YYYY-MM-DD 或 epoch 秒；省略=今天")
    z.add_argument("--all", action="store_true",
                   help="不限时间，从最早一条开始取（想看该会话/全部历史的全部消息时用）")
    z.add_argument("--include-low-value", action="store_true",
                   help="保留低信息量消息（默认过滤）")
    z.add_argument("--no-cache", action="store_true",
                   help="不用增量缓存，每次都给全部消息（费 token）")
    z.add_argument("--rebuild", action="store_true",
                   help="忽略已有游标，从头处理")
    z.add_argument("--output", default=None, metavar="PACK.json",
                   help="直接写入文件（UTF-8）。**强烈建议用它而不是 `>` 重定向** —— "
                        "PowerShell 的 `>` 会写成带 BOM 的 UTF-16，后续读取会报编码错")

    # ---- render (advanced) ----
    # 把 Agent 产出的结论 JSON 渲染成 text/markdown/html
    r = sub.add_parser("render", help="渲染 Agent 产出的摘要结论")
    r.add_argument("input", help="结论 JSON 文件（- 表示 stdin）")
    r.add_argument("--format", dest="render_format", default="text",
                   choices=["text", "markdown", "html", "json"])
    r.add_argument("--output", default=None, help="写入文件（HTML 日报常用）")
    r.add_argument("--verify-against", default=None, metavar="PACK.json",
                   help="用输入包校验 source_message_ids；"
                        "丢弃编造的 id，保证可溯源")
    r.add_argument("--save-summary", action="store_true",
                   help="把结论存入摘要缓存，供下次增量使用（省 token）")
    r.add_argument("--no-save", action="store_true",
                   help="不保存摘要（默认在有 --verify-against 时会保存）")

    # ---- status ----
    st = sub.add_parser("status", help="检查运行环境与密钥状态")
    st.add_argument("--verify", action="store_true",
                    help="额外做深度校验（抽库 HMAC 验证）")

    # ---- detect ----
    d = sub.add_parser("detect", help="自动查找微信数据目录（首次安装用）")
    d.add_argument("--deep", action="store_true",
                   help="没找到时做更深的扫描（较慢，可能几分钟）")

    # ---- init ----
    i = sub.add_parser("init", help="初始化：提取并保存密钥（首次用一次即可）")
    i.add_argument("--force", action="store_true",
                   help="已有密钥也重新提取")
    i.add_argument("--max-pids", type=int, default=0,
                   help="最多扫描几个微信进程（0=全部）")

    return p


# ============================================================
# 分发
# ============================================================
def run(svc: WeChatService, args) -> dict:
    if args.cmd == "summarize":
        # 只准备确定性输入包，不做任何总结（总结由宿主 Agent 完成）
        from skills import prepare

        # --all：不限起始（想看全部历史时用）。用 0 表示"没有下界"。
        since, until = args.since, args.until
        if args.all:
            since, until = prepare.ALL_TIME, None

        kw = {"since": since, "until": until,
              "include_low_value": args.include_low_value}
        # 默认启用增量缓存；--no-cache 时退回全量（费 token）
        if not args.no_cache:
            from skills.cache import SummaryCache
            kw["cache"] = SummaryCache()
        kw["rebuild"] = args.rebuild
        if args.chat:
            pack = prepare.chat_pack(svc, args.chat, **kw)
        else:
            pack = prepare.digest_pack(svc, **kw)

        # --output：直接由本程序写 UTF-8，避开 PowerShell 重定向的编码坑。
        # 写完只回一个简短回执 —— 输入包本身可能几百 KB，
        # 没必要再往 stdout 灌一遍（调用方要读的是文件）。
        if args.output:
            if _write_json(args.output, pack) != 0:
                return None
            msgs = (sum(len(c.get("messages") or []) for c in pack["chats"])
                    if pack.get("kind") == "daily_digest_input"
                    else len(pack.get("messages") or []))
            emit({"written": args.output,
                  "kind": pack.get("kind"),
                  "messages": msgs,
                  "bytes": os.path.getsize(args.output),
                  "hint": "把该文件路径连同提示词一起交给模型"})
            return None
        return pack

    if args.cmd == "status":
        return _status(args)

    if args.cmd == "sessions":
        return svc.list_sessions(limit=args.limit, unread_only=args.unread_only,
                                 include_hidden=args.include_hidden)
    if args.cmd == "messages":
        return svc.list_messages(
            args.chat, limit=args.limit, offset=args.offset,
            start_time=args.start_time, end_time=args.end_time,
            type=args.type, content_limit=args.content_limit)
    if args.cmd == "recent":
        return svc.list_recent_messages(
            since=args.since, limit=args.limit, chat=args.chat,
            unread_only=args.unread_only, content_limit=args.content_limit)
    if args.cmd == "contacts":
        if args.csub == "search":
            return svc.search_contacts(args.query, limit=args.limit, type=args.type)
        return svc.get_contact(args.username)
    if args.cmd == "members":
        return svc.list_group_members(args.group, limit=args.limit)
    if args.cmd == "count":
        return svc.get_message_count(args.chat, start_time=args.start_time,
                                     end_time=args.end_time)
    raise ServiceError("INVALID_PARAM", f"未知命令: {args.cmd}")


def _write_json(path: str, obj) -> int:
    """把 JSON 写成 UTF-8 文件（无 BOM）。

    存在的理由：`summarize` 的输出动辄几百 KB，必须落盘才能交给模型读。
    而 shell 的 `>` 在 Windows PowerShell 上不可控 —— 5.1 写 UTF-16、
    7.x 写带 BOM 的 UTF-8，两者都会让后续读取报编码错，
    逼得调用方写"清洗编码"的过渡脚本。直接由本程序写就没有这个环节。
    """
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except OSError as exc:
        emit({"error": {"code": "INVALID_PARAM",
                        "message": f"写入失败: {path} ({exc})"}})
        return 2
    return 0


def _status(args) -> dict:
    """检查运行环境与密钥缓存状态。"""
    from db import KEYS_FILE, check as check_keys
    from db.keystore import verify_deep

    info: dict = {
        "db_dir": args.db_dir or None,
        "db_dir_exists": os.path.isdir(args.db_dir or ""),
        "keys_file": args.keys,
        "keys_is_default_cache": args.keys == KEYS_FILE,
    }

    if not info["db_dir_exists"]:
        info["ready"] = False
        if not (args.db_dir or ""):
            info["hint"] = ("还没设置微信数据目录。自动查找：\n"
                            "  python cli.py detect\n"
                            "或直接指定：\n"
                            '  python cli.py init --db-dir "<路径>"')
        else:
            info["hint"] = (f"目录不存在：{args.db_dir}\n"
                            "自动查找正确的路径：\n"
                            "  python cli.py detect")
        return info

    st = check_keys(args.db_dir, args.keys)
    info["keys"] = st.to_dict()
    info["ready"] = st.ok

    if st.ok:
        info["hint"] = "一切就绪，可以直接使用"
        if args.verify:
            info["deep_check"] = verify_deep(args.db_dir, args.keys)
    else:
        info["hint"] = _init_hint(st, args)
    return info


def _autodetect_db_dir() -> str | None:
    """自动找微信数据目录（静默，找不到返回 None）。"""
    if sys.platform != "win32":
        return None
    try:
        from db.detect import best
        cand = best()
        return cand.path if cand else None
    except Exception:  # noqa: BLE001
        return None


def _db_dir_from_keys_meta(keys_path: str | None) -> str | None:
    """从密钥文件的 `_meta.db_dir` 取数据目录（探测失败时的回退）。

    `init` 会把当时用的数据目录记进 keys.json，所以这里能直接读回来 ——
    比重新探测快，也不受"微信配置被改过"影响。目录已不存在则不用。
    """
    if not keys_path or not os.path.exists(keys_path):
        return None
    try:
        with open(keys_path, encoding="utf-8") as fh:
            meta = json.load(fh).get("_meta") or {}
    except (OSError, ValueError):
        return None
    d = meta.get("db_dir")
    return d if d and os.path.isdir(d) else None


def _resolve_db_dir(args) -> str | None:
    """决定用哪个数据目录：显式参数 > 密钥里的记录 > 自动探测。

    所有命令都走这里 —— 不能让用户为每条命令都带 `--db-dir`。
    """
    if args.db_dir:
        return args.db_dir
    found = _db_dir_from_keys_meta(args.keys) or _autodetect_db_dir()
    if found:
        print(f"（自动找到微信数据目录：{found}）", file=sys.stderr)
    return found


def _detect(args) -> int:
    """查找微信数据目录。"""
    from db import detect as detect_mod

    if sys.platform != "win32":
        emit({"found": False,
              "error": {"code": "INVALID_PARAM",
                        "message": f"目录探测目前只支持 Windows（当前 {sys.platform}）。"
                                   "请手动指定 --db-dir。"}})
        return 2

    print("正在查找微信数据目录…", file=sys.stderr)
    cands = detect_mod.detect(deep=args.deep,
                              log=lambda m: print(m, file=sys.stderr))

    if not cands:
        emit({"found": False,
              "message": "没有自动找到微信数据目录",
              "next": "请手动确认路径后传给 --db-dir；或试 python cli.py detect --deep 做更深的扫描"})
        return 1

    out = {
        "found": True,
        "count": len(cands),
        "candidates": [c.to_dict() for c in cands],
        "recommended": cands[0].path,
    }
    if len(cands) > 1:
        out["note"] = (f"找到 {len(cands)} 个账号目录，已按最近使用排序；"
                       "recommended 是最近登录的那个")
    emit(out)
    return 0


def _init_hint(st, args) -> str:
    """根据密钥状态给出可照做的下一步。"""
    base = f'python cli.py init --db-dir "{args.db_dir}"'
    if st.status == "missing":
        return f"还没提取过密钥。首次初始化：\n  {base}"
    if st.status in ("stale", "invalid"):
        return f"密钥已失效，需要重新初始化：\n  {base} --force"
    if st.status == "partial":
        return f"部分数据库缺密钥，补齐即可：\n  {base}"
    return f"先初始化：\n  {base}"


def _init(args) -> int:
    """初始化：调用 keyprovider 提取密钥并存到标准缓存位置。"""
    from db import KEYS_FILE, check as check_keys

    current = check_keys(args.db_dir, args.keys)
    if current.ok and not args.force and args.keys == KEYS_FILE:
        emit({"status": "already_initialized",
              "message": "密钥已存在且有效，无需重新提取",
              "keys": current.to_dict(),
              "hint": "确实要重新提取请加 --force"})
        return 0

    script = os.path.join(HERE, "keyprovider", "extract_keys.py")
    if not os.path.exists(script):
        emit({"error": {"code": "INTERNAL_ERROR",
                        "message": f"找不到提取脚本: {script}"}})
        return 4

    # init 只负责 Windows 上的内存提取；其他平台直接说明
    if sys.platform != "win32":
        emit({"error": {
            "code": "LLM_UNAVAILABLE" if False else "INVALID_PARAM",
            "message": f"密钥提取目前只支持 Windows（当前 {sys.platform}）。"
                       "请在 Windows 上运行本命令。"}})
        return 2

    cmd = [sys.executable, script, "--db-dir", args.db_dir, "--out", args.keys]
    if args.force:
        cmd.append("--force")
    if args.max_pids:
        cmd += ["--max-pids", str(args.max_pids)]

    # 子进程是独立的 Python 进程，不会继承本进程对 sys.stdout 的 reconfigure，
    # 在 GBK 控制台上它会用自己的默认编码输出中文 → 乱码。
    # 用环境变量强制它走 UTF-8（PYTHONUTF8 覆盖 -X utf8 之外的一切场合）。
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    code = subprocess.call(cmd, env=env)
    if code != 0:
        emit({"error": {"code": "INTERNAL_ERROR",
                        "message": f"提取失败（退出码 {code}），"
                                   "请查看上方输出中的 [诊断] 部分"}})
        return code or 4

    after = check_keys(args.db_dir, args.keys)
    emit({"status": after.status,
          "message": after.message,
          "keys": after.to_dict()})
    return 0 if after.ok else 3


def _save_summaries(data: dict, pack: dict) -> int:
    """把 Agent 的结论存进摘要缓存，供下次增量处理。

    只保存**能对应到具体会话**的内容：
      - summarize_chat 的结论 → 存到该会话
      - daily_digest 的结论   → 按 chat 字段分派到各会话
    这样下次 summarize 时这些会话就不会再被整段重读。
    """
    from skills.cache import SummaryCache

    cache = SummaryCache()
    kind = data.get("kind") or ""

    # 建立 pack 里的 id -> 消息 索引，用于推进游标
    id_to_msg: dict[str, dict] = {}
    chat_of_id: dict[str, str] = {}
    if pack.get("kind") == "daily_digest_input":
        for c in pack.get("chats", []):
            for m in c.get("messages", []):
                id_to_msg[m.get("id")] = m
                chat_of_id[m.get("id")] = c.get("username")
    else:
        u = (pack.get("chat") or {}).get("username")
        for m in pack.get("messages", []):
            id_to_msg[m.get("id")] = m
            if u:
                chat_of_id[m.get("id")] = u

    def _advance(username: str, summary: dict, display_name: str | None):
        """按该会话在 pack 里出现过的消息推进游标。"""
        if pack.get("kind") == "daily_digest_input":
            for c in pack.get("chats", []):
                if c.get("username") == username:
                    cache.update(username, summary, c.get("messages") or [],
                                 display_name=c.get("display_name") or display_name)
                    return
        else:
            msgs = pack.get("messages") or []
            cache.update(username, summary, msgs,
                         display_name=(pack.get("chat") or {}).get("display_name"))

    saved = 0
    if kind == "chat_summary":
        u = (data.get("chat") or {}).get("username")
        if u:
            _advance(u, data, (data.get("chat") or {}).get("display_name"))
            saved = 1
    elif kind == "daily_digest":
        # 按 chat 字段把条目分派到各会话
        by_chat: dict[str, dict] = {}
        chat_name_to_username = {}
        if pack.get("kind") == "daily_digest_input":
            for c in pack.get("chats", []):
                chat_name_to_username[c.get("display_name")] = c.get("username")

        # 新结构：结论只有 topics[]，每条带 chat 字段标明来源会话
        for item in data.get("topics") or []:
            cname = item.get("chat")
            u = chat_name_to_username.get(cname)
            if not u:
                continue
            bucket = by_chat.setdefault(u, {"overview": "", "topics": []})
            bucket["topics"].append(item)

        for u, bucket in by_chat.items():
            _advance(u, bucket, None)
            saved += 1
    if saved:
        cache.save()
    return saved


def _render(args) -> int:
    """把 Agent 产出的结论 JSON 渲染成 text/markdown/html。"""
    from skills import render as renderer

    try:
        if args.input == "-":
            # stdin 也走同一套容错（`type x.json | python cli.py render -`）
            raw = sys.stdin.buffer.read()
            if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
                data = json.loads(raw.decode("utf-16"))
            else:
                data = json.loads(raw.decode("utf-8-sig"))
        else:
            data = json.loads(_read_text_file(args.input))
    except FileNotFoundError:
        emit({"error": {"code": "INVALID_PARAM",
                        "message": f"找不到文件: {args.input}"}})
        return 2
    except (UnicodeDecodeError, ValueError) as exc:
        emit({"error": {"code": "INVALID_PARAM",
                        "message": f"JSON 解析失败: {exc}",
                        "hint": "若该文件由 PowerShell 的 `>` 重定向产生，"
                                "可能带 BOM 或为 UTF-16；本命令已尝试自动识别，"
                                "仍失败请用 `-Encoding utf8` 重新导出"}})
        return 2

    # 溯源校验：用输入包校验结论里的 source_message_ids
    verify_note = None
    if args.verify_against:
        try:
            pack = json.loads(_read_text_file(args.verify_against))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            emit({"error": {"code": "INVALID_PARAM",
                            "message": f"读取输入包失败: {exc}"}})
            return 2
        dropped = renderer.verify_sources(data, pack)
        verify_note = {"dropped_ids": dropped}
        if dropped:
            print(f"[溯源校验] 丢弃 {dropped} 处不存在的 source_message_id",
                  file=sys.stderr)

        # 存摘要：默认在带 --verify-against 时保存，供下次增量复用
        if not args.no_save:
            saved = _save_summaries(data, pack)
            if saved:
                verify_note["saved_summaries"] = saved
                print(f"[缓存] 已保存 {saved} 个会话的摘要（下次增量使用）",
                      file=sys.stderr)

    # 回溯数据：从 pack 里取被引用的原始消息，内联进 HTML
    msgs_by_id = None
    if args.verify_against:
        try:
            from skills.html_report import context_messages, referenced_ids
            # 只内联被引用的消息 + 前后各 2 条上下文，
            # 既能看到"结论从何而来"，又不会把整包消息塞进 HTML
            msgs_by_id = context_messages(pack, referenced_ids(data))
        except Exception:  # noqa: BLE001
            msgs_by_id = None

    try:
        out = renderer.render(data, args.render_format, msgs_by_id)
    except ValueError as exc:
        emit({"error": {"code": "INVALID_PARAM", "message": str(exc)}})
        return 2

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out)
        res = {"written": args.output, "format": args.render_format,
               "bytes": len(out)}
        if verify_note:
            res.update(verify_note)
        emit(res)
        return 0

    sys.stdout.write(out + "\n")
    return 0


def main(argv=None) -> int:
    _force_utf8_stdio()          # 必须最先做，否则任何一次输出都可能炸
    args = build_parser().parse_args(argv)

    # 密钥路径优先级：显式 --keys > 环境变量 WECHAT_KEYS > 用户级缓存
    from db import resolve_keys_path
    args.keys = resolve_keys_path(args.keys)

    # 所有命令都不强求 --db-dir：显式参数 > 密钥里的记录 > 自动探测。
    # 只在需要连库的命令上做（render 是纯离线渲染，detect 自己在找目录）。
    if args.cmd not in ("detect", "render"):
        args.db_dir = _resolve_db_dir(args)

    if args.cmd == "status":
        # status 不强依赖 db_dir，便于排查环境
        emit(_status(args))
        return 0

    if args.cmd == "detect":
        return _detect(args)

    if args.cmd == "init":
        return _init(args)

    if args.cmd == "render":
        # 纯离线渲染：不连数据库，不需要密钥
        return _render(args)

    if not args.db_dir:
        emit({"error": {
            "code": "KEYS_MISSING",
            "message": "缺少数据库目录：请设置 WECHAT_DB_DIR 或用 --db-dir 指定"}})
        return 3
    if not os.path.isdir(args.db_dir):
        emit({"error": {"code": "KEYS_MISSING",
                        "message": f"数据库目录不存在: {args.db_dir}"}})
        return 3
    # 密钥检查：缺失或失效时给出可直接照做的指引
    from db import check as check_keys

    st = check_keys(args.db_dir, args.keys)
    if not st.ok:
        emit({"error": {
            "code": KEYS_MISSING,
            "status": st.status,
            "message": st.message,
            "hint": _init_hint(st, args),
        }})
        return 3

    try:
        with WeChatService(args.db_dir, args.keys) as svc:
            result = run(svc, args)
        # summarize 的 text/markdown/html 已在 run 内直接输出
        if result is not None:
            emit(result)
        return 0
    except ServiceError as e:
        return fail(e)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        emit({"error": {"code": "INTERNAL_ERROR",
                        "message": f"{type(e).__name__}: {e}"}})
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
