"""摘要缓存 — 增量处理，避免每次日报都重读全部原始消息。

**要解决的问题**：日报若每次都把时间范围内所有消息交给模型，token 会
随历史线性增长，跑几周就撑爆上下文。实际上昨天的消息早就总结过了，
只有新消息需要处理。

**做法**：按会话保存「已处理到哪 + 已得出什么」：

    ~/.weixin-read/summaries.json
    {
      "<chat_username>": {
        "cursor": 1790678619,          # 处理到的时间戳（epoch 秒）
        "cursor_msg_id": "...:7484",   # 同时刻的最后一条，防边界重复
        "display_name": "...",
        "summary": {...},              # 累积的会话摘要（话题/决定/待办）
        "processed_ids": [...],        # 最近处理过的消息 id（去重）
        "updated_at": "..."
      }
    }

**更新策略**（重要）：
    - 读：只取 cursor 之后的新消息 → 交模型 → 合并进已有 summary
    - 写：**模型成功返回后才推进 cursor**。中途失败不推进，
      下次重试会重新拿到那批消息，不会丢。
    - 全量重建：用户显式要求时（--rebuild）忽略 cursor 重来。

**日报怎么用**：优先读各会话的 `summary`（结构化中间结果，体积小），
只有在需要展示/核对具体内容时才回查原始消息。

这不是为了省磁盘，是为了省 **token** —— 日报的输入从"几千条原始消息"
降到"几十条结构化摘要"。
"""
from __future__ import annotations

import json
import os
import time

CACHE_FILE = os.path.join(os.path.expanduser("~"), ".weixin-read",
                          "summaries.json")
SCHEMA_VERSION = 1

# processed_ids 保留条数（只用于去重，不必无限增长）
MAX_PROCESSED_IDS = 500


class SummaryCache:
    """按会话保存摘要与游标。"""

    def __init__(self, path: str = CACHE_FILE):
        self.path = path
        self._data: dict = {}
        self._dirty = False
        self._load()

    # ---------- 读写 ----------
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(raw, dict):
            self._data = {k: v for k, v in raw.items()
                          if not k.startswith("_") and isinstance(v, dict)}

    def save(self) -> str:
        if not self._dirty:
            return self.path
        d = os.path.dirname(os.path.abspath(self.path))
        if d:
            os.makedirs(d, exist_ok=True)
        payload = dict(self._data)
        payload["_meta"] = {
            "_schema": SCHEMA_VERSION,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "chats": len(self._data),
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)      # 原子替换
        self._dirty = False
        return self.path

    # ---------- 查询 ----------
    def __len__(self) -> int:
        return len(self._data)

    def has(self, chat_username: str) -> bool:
        return chat_username in self._data

    def cursor(self, chat_username: str) -> int:
        """返回该会话已处理到的时间戳（无则 0）。"""
        e = self._data.get(chat_username)
        return int(e.get("cursor") or 0) if e else 0

    def entry(self, chat_username: str) -> dict | None:
        return self._data.get(chat_username)

    def summary(self, chat_username: str) -> dict | None:
        e = self._data.get(chat_username)
        if not e:
            return None
        s = e.get("summary")
        # 读取时再精简一次：兼容早期写入的、含冗余字段的缓存
        return _slim(s) if s else None

    def processed_ids(self, chat_username: str) -> set[str]:
        e = self._data.get(chat_username)
        return set(e.get("processed_ids") or []) if e else set()

    def stats(self) -> dict:
        now = int(time.time())
        fresh, stale = 0, 0
        for e in self._data.values():
            age = now - int(e.get("cursor") or 0)
            if age <= 86400 * 2:
                fresh += 1
            else:
                stale += 1
        return {"chats": len(self._data), "recent": fresh, "stale": stale}

    # ---------- 写入 ----------
    def update(self, chat_username: str, summary: dict, entries: list[dict],
               display_name: str | None = None, advance: bool = True,
               truncated: bool = False) -> None:
        """写入/合并摘要，并推进游标。

        Args:
            entries: 本次参与摘要的**新**消息（用于推进游标与去重）
            advance: 是否推进游标。默认 True —— 只在模型成功返回后才调用本方法。
            truncated: 这批消息是否被字符预算截断过。**截断时不能推进游标**，
                否则被丢弃的中间消息会被永久跳过。
        """
        e = self._data.setdefault(chat_username, {})
        if display_name:
            e["display_name"] = display_name

        # 合并摘要：新话题追加，旧的保留（按标题去重）
        if summary:
            old = e.get("summary") or {}
            e["summary"] = _merge_summary(old, _slim(summary))

        # 记录处理过的 id（去重 + 限量）
        if entries and advance:
            if truncated:
                # 截断过 → 只记 id，不推进游标，下次重新拿到这批消息
                ids = list(e.get("processed_ids") or [])
                known = set(ids)
                for m in entries:
                    mid = m.get("id")
                    if mid and mid not in known:
                        ids.append(mid)
                        known.add(mid)
                e["processed_ids"] = ids[-MAX_PROCESSED_IDS:]
                e["truncated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                e["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self._dirty = True
                return
            ids = list(e.get("processed_ids") or [])
            known = set(ids)
            for m in entries:
                mid = m.get("id")
                if mid and mid not in known:
                    ids.append(mid)
                    known.add(mid)
            e["processed_ids"] = ids[-MAX_PROCESSED_IDS:]

            # 游标推进到这批消息里最大的时间戳，**但回退 1 秒**。
            #
            # 为什么不能直接用 newest：消息时间是秒级，同一秒内可能有多条。
            # 若游标 = newest，下次用 `create_time > cursor` 过滤，
            # 会把「同一秒但更晚出现」的消息永久漏掉。
            # 回退 1 秒的代价是可能重复处理 1 秒内的消息（可接受，
            # 摘要合取时按 id 去重），换来的是不漏。
            ts = [int(m["create_time"]) for m in entries
                  if m.get("create_time")]
            if ts:
                newest = max(ts)
                e["cursor"] = max(0, newest - 1)
                e["cursor_exact"] = newest
                # 记下最新那条的 id，供同秒消息的去重
                for m in reversed(entries):
                    if m.get("create_time") == newest:
                        e["cursor_msg_id"] = m.get("id")
                        break

        e["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._dirty = True

    def drop(self, chat_username: str) -> bool:
        if chat_username in self._data:
            del self._data[chat_username]
            self._dirty = True
            return True
        return False

    def clear(self) -> None:
        self._data.clear()
        self._dirty = True


def _merge_summary(old: dict, new: dict) -> dict:
    """把新的会话摘要合并进已存的。

    合并规则（保持"累积"语义，不是每次覆盖）：
      - overview：新的覆盖旧的（新的更能反映近期）
      - topics：按 title 去重，新的覆盖同名的旧的，超量的裁掉最老的
      - decisions / actions：同样按文本去重，保留最近的
    """
    if not old:
        return _slim(new)
    out = dict(old)
    if new.get("overview"):
        out["overview"] = new["overview"]

    for key, dedup in (("topics", "title"), ("decisions", "text"),
                       ("actions", "text")):
        merged = list(old.get(key) or [])
        seen = {_key_of(x, dedup) for x in merged}
        for item in new.get(key) or []:
            k = _key_of(item, dedup)
            if k in seen:
                # 同一条：用新的替换旧的位置
                merged = [x for x in merged if _key_of(x, dedup) != k]
            merged.append(item)
            seen.add(k)
        out[key] = merged[-40:]        # 上限，防止无限增长

    if new.get("stats"):
        out["stats"] = new["stats"]
    return out


def _slim(summary: dict) -> dict:
    """只保留摘要的必要字段。

    存进缓存的东西日后会被塞回输入包给模型看，所以要保持精简：
    去掉 kind/chat/stats 这类元信息、以及空数组 —— 它们对"接着总结"
    没有价值，只是白占 token。
    """
    if not isinstance(summary, dict):
        return {}
    out: dict = {}
    if summary.get("overview"):
        out["overview"] = summary["overview"]
    for key, fields in (
        ("topics", ("title", "summary", "key_points", "participants",
                    "source_message_ids", "inferred", "confidence")),
        ("decisions", ("text", "by", "source_message_ids", "inferred", "confidence")),
        ("actions", ("text", "owner", "due", "source_message_ids",
                     "inferred", "confidence")),
    ):
        items = []
        for item in summary.get(key) or []:
            if not isinstance(item, dict):
                continue
            slim = {f: item[f] for f in fields if item.get(f) not in (None, [], "")}
            if slim:
                items.append(slim)
        if items:
            out[key] = items
    return out


def _key_of(item, field: str) -> str:
    if not isinstance(item, dict):
        return str(item)
    v = item.get(field) or item.get("summary") or ""
    return str(v).strip()[:80]
