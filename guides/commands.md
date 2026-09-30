# 命令参数表

命令就是**本 Skill 目录下的 `wechat-read.cmd`**：

```bat
& "<SKILL_DIR>\wechat-read.cmd" sessions --limit 10
```

`<SKILL_DIR>` = 你读到 `SKILL.md` 时用的那个目录。

下文为简洁，都写成 `wechat-read <参数>`，实际执行时换成上面那种绝对路径写法。

> **参数想确认就跑 `wechat-read <命令> --help`** —— 比读源码快。

---

## `sessions` — 会话列表

```bat
wechat-read sessions --limit 20
wechat-read sessions --unread-only --limit 10
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--limit` | 20 | 返回数量（1-500） |
| `--unread-only` | false | 仅未读会话 |
| `--include-hidden` | false | 含隐藏会话 |

返回 `{sessions[], count, has_more}`；每项含 `username`、`display_name`、
`is_group`、`unread_count`、`summary`、`last_message_type`、
`last_sender_name`、`last_time`。

---

## `messages` — 指定会话的消息（翻页）

> ⚠️ **翻页接口，不要用它做总结。** 总结用 `summarize`。

```bat
wechat-read messages "群名" --limit 50
wechat-read messages "群名" --start-time "2026-09-29" --type text
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `chat` | 必填 | 会话名 **或** wxid |
| `--limit` | 50 | 1-500 |
| `--offset` | 0 | 分页 |
| `--start-time` | — | `YYYY-MM-DD [HH:MM[:SS]]` 或 epoch 秒 |
| `--end-time` | — | 同上，**含当天**（自动补到 23:59:59） |
| `--type` | — | `text`/`image`/`voice`/`video`/`sticker`/`location`/`link`/`file`/`call`/`system`/`app` |
| `--content-limit` | 0 | 内容截断长度，0=不截断 |

返回 `{chat, messages[], count, offset, start_time, end_time, type, has_more}`，
按**时间升序**。

---

## `recent` — 跨会话最近消息（无状态）

> ⚠️ 同样有 `--limit` 上限，**不要用它做总结**。

```bat
wechat-read recent --limit 50
wechat-read recent --since "2026-09-29 16:00:00"
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--since` | — | 只取该时刻之后；省略 = 最新 N 条 |
| `--limit` | 50 | 1-500 |
| `--chat` | — | 限定单个会话 |
| `--unread-only` | false | 只扫有未读的会话 |
| `--content-limit` | 200 | 内容截断长度 |

返回 `{since, messages[], count, latest_time, latest_timestamp}`，
按**时间倒序**，含 `chat` 子对象。

**增量用法**：第一次不带 `--since`，把返回的 `latest_time` 存下来，
下次传 `--since "<上次的 latest_time>"`。游标由你维护，命令本身无状态。

---

## `contacts search` — 搜联系人

```bat
wechat-read contacts search "成员甲" --limit 10
wechat-read contacts search "项目" --type group
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `query` | 必填 | 匹配 wxid / 昵称 / 备注 / 别名 |
| `--limit` | 20 | 1-500 |
| `--type` | all | `all` / `person` / `group` |

---

## `contacts get` — 联系人详情

```bat
wechat-read contacts get "成员甲"
wechat-read contacts get "wxid_xxx"
```

返回 `{contact: {...}}`；群会带 `member_count` 与 `owner`。

---

## `members` — 群成员

```bat
wechat-read members "群名" --limit 500
```

返回 `{group{username, display_name, member_count}, owner, members[], count, has_more}`。

> `member_count` 是**本机已同步的成员数**，可能小于群里实际人数。

---

## `count` — 消息计数

```bat
wechat-read count "群名"
wechat-read count "群名" --start-time "2026-09-29"
```

| 参数 | 说明 |
|---|---|
| `--start-time` / `--end-time` | 同 `messages` |

---

## `summarize` — 准备摘要输入包

**只准备输入，不做总结。** 总结由你（Agent）完成。

```bat
rem 某个会话
wechat-read summarize "群名" --since 2026-09-29 --until 2026-09-29 --output pack.json
rem 全部会话（日报）
wechat-read summarize --since 2026-09-29 --until 2026-09-29 --output pack.json
```

| 参数 | 说明 |
|---|---|
| `chat` | 会话名或 wxid；**省略 = 日报** |
| `--since` / `--until` | `YYYY-MM-DD` 或 epoch 秒；**都省略 = 今天** |
| `--all` | 不限时间，从最早一条开始取（看全部历史时用） |
| `--output` | 写输入包到文件（中间产物，UTF-8 无 BOM）；不带给则输出到 stdout |
| `--include-low-value` | 保留低信息量消息（默认过滤） |
| `--no-cache` | 不用增量缓存，每次全量（费 token） |
| `--rebuild` | 忽略游标，从头处理 |

> ⚠️ **不要用 `>` 重定向代替 `--output`。** PowerShell 的 `>` 会写
> UTF-16（5.1）或带 BOM 的 UTF-8（7.x），后续读取必报编码错。
> `--output` 由程序自己写标准 UTF-8。

带 `--output` 时 stdout 只回一个回执：

```json
{"written": "pack.json", "kind": "daily_digest_input",
 "messages": 2369, "bytes": 803812, "hint": "把该文件路径连同提示词一起交给模型"}
```

流程见 `summarize-chat.md` 和 `daily-digest.md`。

---

## `render` — 校验结论 / 渲染成交付物

**一定要带 `--verify-against` 传回输入包。**

**默认用法（不产文件）** —— 只做校验和存缓存：

```bat
wechat-read render 结论.json --verify-against pack.json --format json
```

用户**明确要文件时**才加 `--output`：

```bat
wechat-read render 日报.json --verify-against pack.json --format html --output 日报.html
```

| 参数 | 说明 |
|---|---|
| `input` | 结论 JSON 文件（`-` = stdin） |
| `--format` | `json`（只校验，不产文件）/ `text` / `markdown` / `html` |
| `--output` | 写入文件 —— **只在用户要文件时给** |
| `--verify-against` | 用输入包校验 `source_message_ids`，丢弃编造的 id |
| `--save-summary` / `--no-save` | 是否把结论存进缓存（默认带 `--verify-against` 时存） |

`--verify-against` 做三件事：校验溯源、保存摘要供下次增量、让 HTML
支持点击回溯原文。

> 不带 `--output` 时结果写到 stdout，**不会产生任何文件**。
> 用户在聊天框问的问题，就在聊天框答 —— 别顺手生成 HTML。

---

## `status` — 环境自检

```bat
wechat-read status
wechat-read status --verify
```

看 `ready` 字段；`false` 时看 `hint`。`--verify` 额外做 HMAC 深度校验。

---

## `detect` — 查找微信数据目录

```bat
wechat-read detect
wechat-read detect --deep
```

`--deep` 会深扫各盘符，**可能要几分钟，先提醒用户**。

---

## `init` — 提取并保存密钥

```bat
wechat-read init
wechat-read init --force
```

首次使用只做一次。详见 `setup.md`。

---

## 消息类型

`type` 字段取值：

`text` · `image` · `voice` · `video` · `sticker` · `location` · `link` ·
`file` · `call` · `system` · `card` · `quote` · `solitaire` ·
`chat_history` · `record` · `transfer` · `red_packet`

`content` 已是**人可读文本**（如 `[图片]`、`[表情]`、`[拍了拍] A 拍了拍 B`），
不是原始 XML。部分类型 `meta` 里有附加信息（md5、引用原文、url 等）。

---

## 环境变量（一般不用设）

**所有命令都不需要 `--db-dir`。** 数据目录按这个顺序自动决定：

1. 显式 `--db-dir`（给了就用给的）
2. `WECHAT_DB_DIR` 环境变量
3. 密钥文件里 `_meta.db_dir`（`init` 时记下的目录）
4. 自动探测（读微信自己的配置）

所以 `sessions`、`recent`、`summarize` 这些业务命令**开箱即用**，
不用先设环境变量。探测成功时 stderr 会打印一行提示用了哪个目录。

| 变量 | 说明 |
|---|---|
| `WECHAT_DB_DIR` | 微信 `db_storage` 目录；不设则按上面的顺序自动决定 |
| `WECHAT_KEYS` | 密钥路径；不设则用 `%USERPROFILE%\.weixin-read\keys.json` |

> 自动探测只在 Windows 上生效（要读微信配置）。已经 `init` 过的机器会走
> 第 3 条，直接命中，不会每次重新探测。
