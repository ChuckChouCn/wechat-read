"""消息内容规整 — 把原始 XML/二进制体转成可读文本。

微信的非文本消息，`message_content` 往往是整段 XML（图片、链接、引用、
接龙、合并转发、引用回复…）。直接把 XML 丢给下游既冗长又难用，这里
按类型提取出人可读的摘要，并保留关键元数据。

设计原则：**永不因为解析失败而丢消息** —— 兜底返回原文（截断）。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

# appmsg 子类型 -> 中文标签
_APP_TYPE_LABEL = {
    "5": "链接",
    "4": "链接",
    "6": "文件",
    "8": "文件",
    "19": "合并转发",
    "24": "转发记录",
    "33": "小程序",
    "36": "小程序",
    "44": "视频号",
    "51": "视频号直播",
    "53": "接龙",
    "57": "引用回复",
    "62": "引用回复",
    "87": "位置分享",
    "2000": "转账",
    "2001": "红包",
}

_XML_HEAD = re.compile(r"^\s*(<\?xml[^>]*\?>\s*)?<")
_TAG = re.compile(r"<[^>]+>")


def _text(el, *paths: str) -> str | None:
    """按路径取第一个非空文本。"""
    for path in paths:
        node = el.find(path)
        if node is not None and node.text and node.text.strip():
            return node.text.strip()
    return None


def _attr(el, name: str) -> str | None:
    if el is None:
        return None
    v = el.get(name)
    return v.strip() if v and v.strip() else None


def looks_like_xml(s: str) -> bool:
    return bool(s) and bool(_XML_HEAD.match(s))


def parse_message_content(content: str, mtype: str, sub_type: int | None = None) -> dict:
    """把消息体规整为 {text, meta}。

    Args:
        content: 已解压的原始消息体
        mtype: 类型名（models.type_name 的结果）
        sub_type: app 子类型

    Returns:
        {"text": 可读文本, "meta": 附加信息或 None}
    """
    if not content:
        return {"text": "", "meta": None}

    if not looks_like_xml(content):
        return {"text": content, "meta": None}

    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        # XML 残缺（被截断等），退化为标签剥离
        return {"text": _strip_tags(content), "meta": None}

    tag = root.tag.lower()

    try:
        if tag == "msg":
            return _parse_msg(root, mtype)
        if tag == "emoji":
            return _parse_emoji(root)
        if tag == "sysmsg":
            return _parse_sysmsg(root)
    except Exception:  # noqa: BLE001
        pass

    return {"text": _strip_tags(content), "meta": None}


def parse_embedded_xml(content: str, mtype: str,
                       sub_type: int | None = None) -> dict:
    """处理「前缀 + XML」的消息体。

    群里的 system 消息常形如 `wxid_xxx:\\n<sysmsg ...>`，而 real_sender_id
    为 0（sender 拿不到），调用方无法用 strip_group_prefix 剥离前缀。
    这里兜底：剥掉任意 `xxx:` 前缀后再尝试 XML 解析，避免 XML 泄漏给用户。
    """
    if not content:
        return {"text": "", "meta": None}
    s = content.lstrip()
    if not s or s.startswith("<"):
        return parse_message_content(content, mtype, sub_type)

    esc = content.replace("\r", "")
    nl = esc.find("\n")
    if nl > 0 and nl < 200:
        head, rest = esc[:nl], esc[nl + 1:].lstrip()
        # 前缀要像 wxid/显示名（不含空格和 <），后面才是 XML
        if rest.startswith("<") and "<" not in head and len(head) <= 64:
            return parse_message_content(rest, mtype, sub_type)
    return parse_message_content(content, mtype, sub_type)


def _parse_msg(root: ET.Element, mtype: str) -> dict:
    """<msg>...</msg> 容器：可能是 appmsg / img / video / location 等。"""
    # 表情：<msg><emoji .../></msg>，内容全在属性上，无文本节点
    emoji = root.find("emoji")
    if emoji is not None:
        return _parse_emoji(emoji)

    # 图片
    img = root.find("img")
    if img is not None:
        return {"text": "[图片]", "meta": {
            k: v for k, v in {
                "md5": _attr(img, "md5"),
                "cdn_thumb_url": _attr(img, "cdnthumburl"),
                "file_name": _attr(img, "img_file_name"),
            }.items() if v}}

    # 语音
    voice = root.find("voicemsg")
    if voice is not None:
        length = _attr(voice, "voicelength")
        dur = f"{int(int(length) / 1000)}秒" if length and length.isdigit() else ""
        return {"text": f"[语音 {dur}]".strip(), "meta": None}

    # 视频
    video = root.find("videomsg")
    if video is not None:
        return {"text": "[视频]", "meta": None}

    # 位置
    loc = root.find("location")
    if loc is not None:
        lat, lng = _attr(loc, "latitude"), _attr(loc, "longitude")
        label = _attr(loc, "label")
        return {"text": f"[位置] {label or ''}".strip(),
                "meta": {"latitude": lat, "longitude": lng} if lat else None}

    # 名片：所有信息都在 <msg> 根节点属性上，没有子元素
    if _attr(root, "nickname") or _attr(root, "username"):
        nick = _attr(root, "nickname") or _attr(root, "username")
        return {"text": f"[名片] {nick}", "meta": {
            "username": _attr(root, "username"),
            "nickname": _attr(root, "nickname"),
            "alias": _attr(root, "alias"),
        }}

    # appmsg（链接/文件/引用/接龙/合并转发…）
    app = root.find("appmsg")
    if app is not None:
        return _parse_appmsg(app)

    return {"text": _strip_tags(ET.tostring(root, encoding="unicode")), "meta": None}


def _parse_appmsg(app: ET.Element) -> dict:
    """解析 <appmsg>。

    分支顺序很重要：微信的 appmsg 里存在大量**空壳节点**
    （如 <appattach><totallen>0</totallen></appattach> 出现在拍一拍里），
    所以必须先判断"有实质内容"的分支，空壳兜底放最后。
    """
    # 注意：appmsg 的 type 是**子元素** <type>62</type>，不是属性
    app_type = _text(app, "type") or _attr(app, "type") or ""
    label = _APP_TYPE_LABEL.get(app_type, "应用消息")

    title = _text(app, "title")
    des = _text(app, "des")
    url = _text(app, "url")
    meta: dict = {"app_type": app_type}

    # 拍一拍（有 patinfo 且 template 有内容）—— 要早于 appattach 判断
    pat = app.find("patinfo")
    if pat is not None:
        tmpl = _text(pat, "template") or title
        if tmpl:
            meta = {
                "from_user": _text(pat, "fromusername"),
                "patted": _text(pat, "pattedusername"),
                "suffix": _text(pat, "patsuffix"),
            }
            # 模板形如 "${wxid_a}" 拍了拍 "${wxid_b}"，占位符留给上层渲染
            return {"text": f"[拍了拍] {tmpl}", "meta": meta}

    # 引用回复：正文在 title，被引用的原文在 refermsg
    refer = app.find("refermsg")
    if refer is not None:
        ref_content = _text(refer, "content") or ""
        ref_name = _text(refer, "displayname") or _text(refer, "fromusr")
        meta["quote"] = {"from": ref_name, "content": _clean_ref(ref_content)[:200]}
        return {"text": title or "[引用回复]", "meta": meta}

    # 合并转发 / 收藏记录：recorditem 里套一层 XML。
    # 两种编码都要处理：CDATA 段，以及被实体转义的文本（&lt;recordinfo&gt;）。
    record = app.find("recorditem")
    if record is not None:
        inner = _record_text(record)
        if inner:
            n = inner.count("<recordinfo>") or inner.count("<dataitem ")
            item_title, item_desc = _record_summary(inner)
            label_txt = title or item_title or des
            text = f"[合并转发] {label_txt or ''}".strip()
            m2 = {**meta, "title": label_txt, "item_count": n or None}
            if item_desc:
                m2["preview"] = item_desc[:150]
            return {"text": text, "meta": {k: v for k, v in m2.items() if v}}

    # 接龙
    if app_type == "53":
        return {"text": f"[接龙] {title or ''}".strip(),
                "meta": {**meta, "title": title}}

    # 文件（totallen>0 才算真文件，避免空壳）
    fnode = app.find("appattach")
    if fnode is not None:
        fsize = _text(fnode, "totallen")
        fname = _text(fnode, "filename")
        total = int(fsize) if fsize and fsize.isdigit() else 0
        if total > 0 or fname:
            meta["file"] = {"name": fname, "size": total or None}
            return {"text": f"[文件] {fname or ''}".strip(), "meta": meta}

    # 链接/普通分享
    if title:
        parts = [f"[{label}]", title]
        if des:
            parts.append(f"— {des}")
        if url:
            meta["url"] = url
        return {"text": " ".join(parts), "meta": {k: v for k, v in meta.items() if v}}

    return {"text": f"[{label}]", "meta": meta}


_SYSMSG_PLACEHOLDER = re.compile(r"\$(\w+)\$")


def _fill_sysmsg_placeholders(text: str, root: ET.Element) -> str:
    """把 $username$ / $names$ 等占位符替换成 <link_list> 里的真实昵称。

    结构：<link name="username"><memberlist><member>
             <username>wxid_…</username><nickname>张三</nickname>
          </member></memberlist></link>
    多个成员用 <separator> 连接（如「、」）。
    """
    values: dict[str, str] = {}
    for link in root.iter("link"):
        name = link.get("name")
        if not name:
            continue
        sep_node = link.find("separator")
        sep = (sep_node.text or "、") if sep_node is not None else "、"
        names = []
        for member in link.iter("member"):
            nick = member.findtext("nickname") or member.findtext("username")
            if nick and nick.strip():
                names.append(nick.strip())
        if names:
            values[name] = sep.join(names)

    if not values:
        return text
    return _SYSMSG_PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def _record_text(record: ET.Element) -> str:
    """取出 recorditem 里的内层 XML 文本。

    ElementTree 会把 CDATA 直接并进 .text，而实体转义的文本则保持
    &lt; 形式，这里两种情况都还原。
    """
    parts = [record.text or ""]
    parts.extend(child.tail or "" for child in record)
    raw = "".join(parts)
    if "&lt;" in raw:
        raw = (raw.replace("&lt;", "<").replace("&gt;", ">")
                  .replace("&quot;", '"').replace("&amp;", "&"))
    return raw.strip()


def _record_summary(inner: str) -> tuple[str | None, str | None]:
    """从合并转发内容里提取标题与首条预览。"""
    title = None
    desc = None
    try:
        root = ET.fromstring(inner)
        info = root.find(".//info")
        if info is not None and info.text and info.text.strip():
            title = info.text.strip()
        first = root.find(".//dataitem")
        if first is not None:
            t = first.get("datatitle") or first.get("title")
            d = first.get("datadesc") or first.get("desc")
            title = title or (t.strip() if t else None)
            desc = d.strip() if d else None
    except ET.ParseError:
        pass
    if not title:
        # 退化为纯文本首段
        txt = _strip_tags(inner)
        if txt:
            title = txt[:80]
    return title, desc


def _parse_emoji(root: ET.Element) -> dict:
    md5 = _attr(root, "md5")
    meta = {"from_user": _attr(root, "fromusername"), "md5": md5}
    return {"text": "[表情]", "meta": {k: v for k, v in meta.items() if v} or None}


def _parse_sysmsg(root: ET.Element) -> dict:
    """系统消息：撤回、入群提示、群公告变更等。"""
    # 模板型（入群/退群/群名变更）：文案在 <template> 的 CDATA 里，
    # 里面的 $username$ / $names$ 占位符由同级的 <link_list> 给出取值。
    tmpl = root.find(".//template")
    if tmpl is not None and tmpl.text and tmpl.text.strip():
        text = _fill_sysmsg_placeholders(tmpl.text.strip(), root)
        return {"text": text, "meta": {"sys_type": "template"}}

    plain = root.find(".//plain")
    if plain is not None and plain.text and plain.text.strip():
        return {"text": plain.text.strip(), "meta": {"sys_type": "plain"}}

    # 普通系统消息：取任意有文字的节点
    for child in root.iter():
        txt = (child.text or "").strip()
        if txt and not txt.startswith("<"):
            return {"text": txt, "meta": {"sys_type": child.tag}}

    return {"text": "[系统消息]", "meta": None}


def _strip_tags(s: str) -> str:
    """剥掉 XML/HTML 标签，保留可读文字。"""
    if not s:
        return ""
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    s = re.sub(r"<\?xml[^>]*\?>", " ", s)
    s = _TAG.sub(" ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _clean_ref(s: str) -> str:
    """引用内容若本身是 XML，提取其中文本。"""
    if not s:
        return ""
    if looks_like_xml(s):
        return _strip_tags(s)
    return s
