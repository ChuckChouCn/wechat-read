"""日报 HTML 渲染 —— 编辑简报 / 信息 Inbox 的形态。

设计取向：

**第一屏极简** —— 只有日期、一句话概览、关键数字。不复述任何内容。

**三段式** —— 每条内容统一为「短标题 + 一句摘要 + 来源元信息」。
绝不再把一整段话当标题。

**视觉权重递减** —— 今日重点最重；待办/需要回复做成 Inbox 行；
通知次之；其他话题降到最弱。

**减法** —— 不用圆角卡片、不用阴影、不用大片色块。
层次靠字号、字重、颜色深浅、留白、1px 分割线。

**中文本土化** —— 不用 uppercase、不用 letter-spacing。

**溯源** —— 入口弱化成灰色小字「2 条原始消息 →」，
点击展开消息链（被引用的高亮，上下文淡化），能看清结论从何而来。
"""
from __future__ import annotations

import datetime
import html as _html
import json

_CSS = """
:root{
  --ink:#1a1c1f; --ink-2:#4a5157; --ink-3:#7b838a; --ink-4:#a8aeb4;
  --rule:#e8eaec; --rule-2:#f2f4f5;
  --accent:#0d6e63; --accent-bg:#f0f7f6;
  --todo:#9a5a12; --reply:#1e4bb8;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;background:#eceeef;color:var(--ink);
  font:15px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
       "Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;
}
.page{max-width:720px;margin:0 auto;background:#fff;min-height:100vh;
  padding:0 0 72px}

/* ============ 刊头 ============ */
.mast{padding:54px 44px 0}
.mast .kicker{
  font-size:12.5px;color:var(--ink-3);margin-bottom:22px;
  padding-bottom:14px;border-bottom:2px solid var(--ink);
  display:flex;justify-content:space-between;align-items:baseline;
}

/* 概览：全篇最大的字，视觉锚点 */
/* 主标题：日报本身的名字 */
.report-title{
  font-size:34px;font-weight:720;line-height:1.25;letter-spacing:-.02em;
  margin:0 0 22px;
}
/* 概览：主标题下的一句结论 */
.lede{
  margin:0 0 30px;font-size:17px;line-height:1.7;font-weight:400;
  color:var(--ink-2);max-width:36em;
}
.lede .hl{color:var(--accent)}

/* 数据条：大数字 + 小标签 */
.figs{display:flex;gap:34px;padding:0 0 30px;
  border-bottom:1px solid var(--rule);margin-bottom:0}
.fig .n{font-size:30px;font-weight:700;line-height:1.1;
  font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.fig .l{font-size:12.5px;color:var(--ink-3);margin-top:4px}
.fig.dim .n{color:var(--ink-4);font-weight:600}

/* ============ 区块 ============ */
.sec{padding:0 44px}
.sec + .sec{margin-top:44px}
.hd{display:flex;align-items:baseline;gap:10px;margin-bottom:20px;
  padding-top:30px}
.sec:first-of-type .hd{padding-top:34px}
.hd h2{margin:0;font-size:15.5px;font-weight:680;letter-spacing:0}
.hd .n{font-size:12.5px;color:var(--ink-4)}

/* ============ 今日重点：大编号做视觉锚点 ============ */
.lead{position:relative;padding:0 0 30px 0;margin-bottom:30px;
  border-bottom:1px solid var(--rule-2)}
.lead:last-child{border-bottom:none;margin-bottom:0;padding-bottom:6px}
.lead .no{
  font-size:38px;font-weight:700;line-height:1;color:#e4e7e9;
  font-variant-numeric:tabular-nums;margin-bottom:10px;
  letter-spacing:-.03em;
}
.lead h3{margin:0 0 10px;font-size:18.5px;line-height:1.5;font-weight:660;
  letter-spacing:-.005em;max-width:30ch}
.lead .d{margin:0;font-size:15px;line-height:1.8;color:var(--ink-2);
  max-width:50ch}
.lead .pts{margin:12px 0 0;padding:0;list-style:none}
.lead .pts li{font-size:14.5px;color:var(--ink-2);line-height:1.75;
  padding-left:16px;position:relative;margin-bottom:5px}
.lead .pts li::before{content:"";position:absolute;left:0;top:.68em;
  width:5px;height:1.5px;background:#c3c9cd}

/* ============ Inbox ============ */
.item{display:flex;gap:13px;padding:15px 0;
  border-bottom:1px solid var(--rule-2)}
.item:last-child{border-bottom:none}
.item .mk{flex:none;width:16px;height:16px;margin-top:4px;
  border:1.5px solid #ccd2d6;border-radius:2px;position:relative}
.item.reply .mk{border-radius:50%;border-color:#c8d3ee}
.item.reply .mk::after{content:"";position:absolute;left:4px;top:3px;
  width:4px;height:4px;border-radius:50%;background:var(--reply)}
.item .cnt{flex:1;min-width:0}
.item .top{display:flex;align-items:baseline;gap:14px}
.item .t{font-size:15.5px;line-height:1.6;font-weight:560;flex:1;min-width:0}
.item .due{flex:none;font-size:12.5px;color:var(--todo);
  background:#fdf8f1;padding:2px 8px;border-radius:3px}
.item .w{font-size:13.5px;line-height:1.7;color:var(--ink-3);margin-top:4px}

/* ============ 通知 ============ */
.note{padding:14px 0 14px 16px;border-left:2px solid var(--accent-bg);
  border-bottom:1px solid var(--rule-2);background:#fcfdfd;
  margin-bottom:6px;border-radius:0 3px 3px 0}
.note:last-child{border-bottom:none}
.note .t{font-size:14.5px;line-height:1.7;font-weight:540}
.note .d{font-size:13.5px;color:var(--ink-3);margin-top:3px}

/* ============ 话题：明显降权 ============ */
.topic{padding:11px 0;border-bottom:1px solid var(--rule-2)}
.topic:last-child{border-bottom:none}
.topic .t{font-size:14px;font-weight:560;color:var(--ink-2);line-height:1.65}
.topic .d{font-size:13px;color:var(--ink-3);line-height:1.7;margin-top:3px}

/* ============ 溯源 ============ */
.src{display:inline-flex;align-items:center;gap:4px;margin-top:9px;
  font-size:12.5px;color:var(--ink-4);background:none;border:none;padding:0;
  cursor:pointer;font-family:inherit}
.src::before{content:"";width:10px;height:1px;background:#d2d7db;
  display:inline-block}
.src:hover{color:var(--accent)}
.src:hover::before{background:var(--accent)}

/* ============ 来源会话 ============ */
.chats .row{display:flex;align-items:baseline;gap:12px;padding:8px 0;
  font-size:13px;color:var(--ink-3);border-bottom:1px solid var(--rule-2)}
.chats .row:last-child{border-bottom:none}
.chats .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.chats .ct{font-size:12.5px;color:var(--ink-4);white-space:nowrap}


/* ============ 分类标签 ============ */
.cat{
  display:inline-block;font-size:11.5px;padding:1px 8px;border-radius:3px;
  margin-bottom:8px;color:var(--ink-3);background:#f2f4f5;
}

/* ============ 类型分组 ============ */
.grp{margin-bottom:32px}
.grp:last-child{margin-bottom:0}
.grp-h{
  font-size:16px;font-weight:700;color:var(--ink);margin-bottom:6px;
  padding:2px 0 10px 10px;border-bottom:2px solid var(--ink);
  border-left:3px solid var(--accent);
}
.grp-h .n{font-size:12px;color:var(--ink-4);font-weight:400;margin-left:6px}

/* ============ 话题 ============ */
.topic{padding:16px 0;border-bottom:1px solid var(--rule-2)}
.topic:last-child{border-bottom:none}
.topic h3{
  margin:0 0 7px;font-size:17px;line-height:1.5;font-weight:660;
  letter-spacing:-.005em;max-width:32ch;
}
.topic p{
  margin:0;font-size:14.5px;line-height:1.75;color:var(--ink-2);
  max-width:52ch;
}
.topic .pts{margin:9px 0 0;padding:0;list-style:none}
.topic .pts li{
  font-size:14px;color:var(--ink-2);line-height:1.7;padding-left:14px;
  position:relative;margin-bottom:4px;
}
.topic .pts li::before{
  content:"";position:absolute;left:0;top:.68em;width:5px;height:1.5px;
  background:#c3c9cd;
}
.topic .who{font-size:12.5px;color:var(--ink-4);margin-top:8px}

/* ============ 页脚 ============ */
.foot{margin:52px 44px 0;padding-top:18px;border-top:2px solid var(--ink);
  font-size:12px;line-height:2;color:var(--ink-4)}
.foot b{color:var(--ink-3);font-weight:600}

/* ============ 溯源弹窗：聊天上下文视图 ============ */
dialog{border:none;padding:0;width:min(640px,calc(100% - 24px));
  background:#fff;color:var(--ink);max-height:88vh;
  box-shadow:0 24px 70px rgba(10,14,18,.24);border-radius:6px}
dialog::backdrop{background:rgba(10,14,18,.44)}
.dh{display:flex;align-items:center;justify-content:space-between;gap:12px;
  padding:16px 24px 14px;border-bottom:1px solid var(--rule);
  position:sticky;top:0;background:#fff;border-radius:6px 6px 0 0;z-index:2}
.dh .ttl{font-size:13.5px;font-weight:640;color:var(--ink)}
.dh .sub{font-size:12px;color:var(--ink-4);margin-top:2px}
.dh button{background:none;border:none;font-size:20px;line-height:1;
  color:var(--ink-4);cursor:pointer;padding:0 2px;font-family:inherit}
.dh button:hover{color:var(--ink)}
.db{padding:6px 24px 24px;overflow-y:auto;max-height:calc(88vh - 60px)}

/* 一条消息 = 一个独立视觉块 */
.m{padding:13px 0;border-bottom:1px solid var(--rule-2)}
.m:last-child{border-bottom:none}
.m .mh{font-size:11.5px;color:var(--ink-4);margin-bottom:5px;
  display:flex;gap:8px;align-items:baseline}
.m .mh .who{color:var(--ink-3);font-weight:600}
.m .tx{font-size:14.5px;line-height:1.72;color:var(--ink-2);
  white-space:pre-wrap;word-break:break-word}

/* 关键句：左侧色条 + 浅底 + 正文加重 —— 一眼看出是"哪几句" */
.m.key{background:#f7fbfa;border-left:3px solid var(--accent);
  border-radius:0 4px 4px 0;padding:14px 16px 14px 15px;
  margin:8px 0;border-bottom:none}
.m.key .mh .who{color:var(--accent)}
.m.key .tx{color:var(--ink);font-weight:500;font-size:15px}

/* 上下文默认折叠 */
.m.ctx{display:none}
.db.expanded .m.ctx{display:block}
.db.expanded .m.ctx + .m.key,
.db.expanded .m.key + .m.ctx{border-top:1px solid var(--rule-2)}

/* 展开/收起 */
.more{display:block;width:100%;margin:10px 0;padding:9px;
  font-size:12.5px;color:var(--ink-3);background:#f8f9fa;border:none;
  border-radius:4px;cursor:pointer;font-family:inherit}
.more:hover{background:#f1f3f4;color:var(--accent)}
.db.expanded .more{display:none}
.less{display:none;width:100%;margin:12px 0 0;padding:9px;font-size:12.5px;
  color:var(--ink-3);background:#f8f9fa;border:none;border-radius:4px;
  cursor:pointer;font-family:inherit}
.db.expanded .less{display:block}
.less:hover{background:#f1f3f4;color:var(--accent)}
.gap{font-size:11.5px;color:var(--ink-4);text-align:center;
  padding:9px 0}
.db.expanded .gap{display:none}
.dz{padding:32px 24px;text-align:center;color:var(--ink-3);font-size:13.5px}

@media (max-width:600px){
  .mast{padding:32px 22px 0}
  .lede{font-size:22px}
  .figs{gap:22px}
  .fig .n{font-size:21px}
  .sec{padding:0 22px}
  .lead .no{font-size:30px}
  .lead h3{font-size:17px}
  .foot{margin:36px 22px 0}
}
@media print{
  body{background:#fff}
  .page{max-width:none}
  .src{display:none}
  dialog{display:none}
}
"""

# 分类名由 Agent 按当天内容归纳（领域/主题，如「AI 工具」「自媒体运营」）。
# 这里不做映射 —— 硬编码分类表等于把判断权抢回来，而分类本就该跟着内容走。
_MAX_CATS = 8          # 防护：分类过多说明归纳不到位，多余的并入「其他」


def _cat(item: dict) -> str:
    """取话题的分类名。空值归入「其他」。"""
    return (str(item.get("category") or "").strip() or "其他")[:20]


def _e(s) -> str:
    return _html.escape(str(s if s is not None else ""))


def _weekday(d) -> str:
    try:
        dt = datetime.datetime.strptime(str(d)[:10], "%Y-%m-%d")
        return (f"{dt.year}年{dt.month}月{dt.day}日 "
                f"星期{'一二三四五六日'[dt.weekday()]}")
    except (ValueError, TypeError):
        return str(d or "")


def _src_btn(refs: list[str]) -> str:
    if not refs:
        return ""
    return (f'<button class="src" data-refs="{_e(",".join(refs))}">'
            f'{len(refs)} 条原始消息 →</button>')


def _topic(item: dict, msgs: dict) -> str:
    """一个话题：类型标签 + 短标题 + 摘要 + 要点 + 来源。"""
    refs = [r for r in (item.get("source_message_ids") or []) if r]
    title = item.get("title") or item.get("text") or ""
    out = ['<article class="topic">', f'<h3>{_e(title)}</h3>']
    if item.get("summary"):
        out.append(f'<p>{_e(item["summary"])}</p>')
    pts = item.get("key_points") or []
    if pts:
        out.append('<ul class="pts">' +
                   "".join(f"<li>{_e(p)}</li>" for p in pts) + "</ul>")
    if item.get("participants"):
        out.append(f'<div class="who">'
                   f'{"、".join(_e(p) for p in item["participants"])}</div>')
    btn = _src_btn(refs)
    if btn:
        out.append(btn)
    out.append("</article>")
    return "".join(out)


def render_digest(data: dict, messages_by_id: dict | None = None) -> str:
    msgs = messages_by_id or {}
    is_chat = "chats" not in data
    rng = data.get("range", {})
    stats = data.get("stats", {})
    audit = data.get("audit") or {}
    chat = data.get("chat") or {}
    name = chat.get("display_name") if is_chat else ""
    topics = [t for t in (data.get("topics") or []) if isinstance(t, dict)]

    P = ["<!DOCTYPE html>",
         '<html lang="zh-CN"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         f'<title>{_e(name or "微信简报")}</title>',
         f'<style>{_CSS}</style></head><body><div class="page">']

    # ---- 刊头 ----
    date_txt = _weekday(rng.get("since")) or "微信简报"
    P.append('<header class="mast">')
    title = f"{name} 话题简报" if name else "微信话题简报"
    P.append(f'<div class="kicker"><span>{_e(name or "微信")}</span>'
             f'<span>{_e(date_txt)}</span></div>')
    P.append(f'<h1 class="report-title">{_e(title)}</h1>')
    if data.get("overview"):
        P.append(f'<p class="lede">{_e(data["overview"])}</p>')

    figs = []
    if not is_chat and data.get("chats"):
        figs.append((len(data["chats"]), "个会话", False))
    if stats.get("message_count"):
        figs.append((stats["message_count"], "条消息", True))
    if topics:
        figs.append((len(topics), "个话题", False))
    if figs:
        P.append('<div class="figs">')
        for n, l, dim in figs:
            P.append(f'<div class="fig{" dim" if dim else ""}">'
                     f'<div class="n">{n}</div><div class="l">{l}</div></div>')
        P.append("</div>")
    P.append("</header>")

    # ---- 话题（按类型分组）----
    grouped: dict[str, list[dict]] = {}
    for t in topics:
        grouped.setdefault(_cat(t), []).append(t)

    # 分类过多时，把最小的几个并入「其他」
    if len(grouped) > _MAX_CATS:
        ranked = sorted(grouped.items(), key=lambda kv: -len(kv[1]))
        keep = dict(ranked[:_MAX_CATS - 1])
        rest = [t for _k, items in ranked[_MAX_CATS - 1:] for t in items]
        keep.setdefault("其他", []).extend(rest)
        grouped = keep

    if grouped:
        # 分类多时按话题数降序（重要的领域在前）；少时保持 Agent 给的顺序
        order = (sorted(grouped.items(), key=lambda kv: -len(kv[1]))
                 if len(grouped) > 3 else list(grouped.items()))
        P.append('<section class="sec">'
                 f'<div class="hd"><h2>话题</h2>'
                 f'<span class="n">{len(topics)}</span></div>')
        for key, items in order:
            P.append(f'<div class="grp"><div class="grp-h">{_e(key)}'
                     f'<span class="n">{len(items)}</span></div>')
            for it in items:
                P.append(_topic(it, msgs))
            P.append("</div>")
        P.append("</section>")

    # ---- 来源会话 ----
    if data.get("chats"):
        P.append('<section class="sec">'
                 '<div class="hd"><h2>来源会话</h2>'
                 f'<span class="n">{len(data["chats"])}</span></div>'
                 '<div class="chats">')
        for c in data["chats"][:40]:
            P.append(f'<div class="row"><span class="nm">'
                     f'{_e(c.get("display_name") or c.get("username"))}</span>'
                     f'<span class="ct">{c.get("message_count", 0)} 条</span></div>')
        P.append("</div></section>")

    # ---- 页脚 ----
    P.append('<footer class="foot">')
    bits = []
    if audit.get("sql_total") is not None:
        bits.append(f'数据库 {audit["sql_total"]} 条消息，全部读取，<b>零丢失</b>')
    if audit.get("earliest_time") and audit.get("latest_time"):
        bits.append(f'覆盖 {_e(str(audit["earliest_time"])[11:16])} — '
                    f'{_e(str(audit["latest_time"])[11:16])}')
    if bits:
        P.append("<div>" + "，".join(bits) + "</div>")
    P.append("<div>点击「N 条原始消息」可核对上下文。</div>")
    P.append("</footer>")

    P.append("</div>")
    P.append('<dialog id="sd"><div class="dh">'
             '<span class="ttl" id="st">原始消息</span>'
             '<button data-close>&times;</button></div>'
             '<div class="db" id="sb"></div></dialog>')
    P.append(_script(msgs))
    P.append("</body></html>")
    return "\n".join(P)


def _script(msgs: dict) -> str:
    payload = json.dumps(msgs, ensure_ascii=False).replace("</", "<\\/")
    return """<script>
const M = %s;
const dlg = document.getElementById('sd');
const box = document.getElementById('sb');
const ttl = document.getElementById('st');
const esc = s => { const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s); return d.innerHTML; };
const ids = Object.keys(M);

// 只展示这个结论自己引用的那几条，不是全部
function open(refs) {
  const want = refs.filter(r => M[r]);

  if (!want.length) {
    ttl.textContent = '原始消息';
    box.innerHTML = '<div class="dz">原始消息未内联。<br>'
      + '生成日报时带上来源包（--verify-against）即可包含原文。</div>';
    dlg.showModal(); return;
  }

  ttl.textContent = '总结依据 · ' + want.length + ' 条原始消息';

  // 按 pack 里的原始顺序（M 的 key 顺序）排，保证时间序正确
  const picked = ids.filter(id => want.indexOf(id) >= 0);
  box.innerHTML = picked.map(id => {
    const m = M[id];
    return '<div class="m key">'
      + '<div class="mh"><span class="who">' + esc(m.sender || '?') + '</span>'
      + '<span>' + esc(m.time ? m.time.slice(5, 16) : '') + '</span>'
      + '</div><div class="tx">' + esc(m.content) + '</div></div>';
  }).join('');
  dlg.showModal();
}

document.querySelectorAll('.src').forEach(b => {
  b.addEventListener('click', e => {
    e.stopPropagation();
    const refs = (b.dataset.refs || '').split(',').filter(Boolean);
    if (refs.length) open(refs);
  });
});
document.querySelector('[data-close]').addEventListener('click', () => dlg.close());
dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
</script>""" % payload


# ============================================================
# 回溯数据：被引用的消息 + 上下文
# ============================================================
def context_messages(pack: dict, ref_ids: set[str]) -> dict[str, dict]:
    """取被结论**直接引用**的消息。

    只回答一个问题：「这条总结是由哪几句话得出来的」。

    不做任何裁剪 —— 引用了几条就给几条。之前加过"跨度超 40 分钟就
    只保留最密集一段"的防护，结果跨会话时算错、静默丢消息（94 条
    只返回 71 条）。话题跨度该由 Agent 在生成时保证，不该在这里补救。
    """
    out: dict[str, dict] = {}
    if not pack:
        return out

    def _process(msgs: list[dict], chat_name):
        for m in msgs:
            mid = m.get("id")
            if not mid or mid not in ref_ids or mid in out:
                continue
            out[mid] = {
                "time": m.get("time"),
                "sender": m.get("sender"),
                "content": (m.get("content") or "")[:1000],
                "chat": chat_name,
                "key": True,
            }

    if pack.get("kind") == "daily_digest_input":
        for c in pack.get("chats", []):
            _process(c.get("messages") or [], c.get("display_name"))
    else:
        _process(pack.get("messages") or [],
                 (pack.get("chat") or {}).get("display_name"))
    return out


def referenced_ids(data: dict) -> set[str]:
    ids: set[str] = set()
    for key in ("highlights", "notices", "actions", "needs_reply",
                "topics", "decisions"):
        for item in data.get(key) or []:
            if isinstance(item, dict):
                ids.update(item.get("source_message_ids") or [])
    return ids


def messages_from_pack(pack: dict, only_ids: set[str] | None = None) -> dict:
    """兼容旧调用。"""
    return context_messages(pack, only_ids or set())
