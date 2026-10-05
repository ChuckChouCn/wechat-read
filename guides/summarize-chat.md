# 总结单个会话

用户点名了某个群/某人，要求总结其聊天内容时走这条路。

## 先看这条：话题要挖够

**一个活跃的群一天会聊十几件事，话题数量不设上限，都要挖出来。**
少于 5 个通常意味着挖得不够深 —— 别只挑最显眼的那几条。
详细的"该挖什么/该丢什么"见 `daily-digest.md` 的「深度挖掘」一节，
两边是同一套标准。

> 用户说「总结今天微信」这类**跨会话**需求 → 用 `daily-digest.md`。
>
> **命令参数不确定就跑 `wechat-read summarize --help`**，或看
> `commands.md`。**不要去读 `cli.py` 源码** —— 用法信息本文档加上
> `--help` 已经给全了。

## 谁来推理

本流程**不调用任何 LLM**。推理由**你（宿主 Agent）**完成：

```
你（Agent） → 读本文件 → 调 CLI 取输入包 → 你自己总结 → 调 CLI 渲染
```

项目不绑定任何 LLM SDK，也不需要额外 API Key。**你自己就是模型。**

## 三条铁律

1. **先聚类，再总结**。不要逐条复述消息。讨论同一件事的消息归成一个话题，
   给话题起一个能看懂的名字。
2. **可溯源**。每条结论都要带 `source_message_ids`，
   **只能引用输入包里真实存在的 id**，绝不编造。
3. **区分事实与推断**。消息里明说的 = 事实（`inferred: false`）；
   你的归纳推测 = 推断（`inferred: true`，`confidence` 降级）。

## 第 1 步：取输入包

先用 **`--format text`** 拿一条一行的紧凑文本，这是给你（Agent）读的形态：

```bat
wechat-read summarize "群名" --format text --output chat.txt
```

回执告诉你文件有多少行，据此决定读几页：

```json
{"written": "chat.txt", "kind": "chat_summary_input", "format": "text",
 "lines": 1122, "total_lines": 1122, "bytes": 76130,
 "hint": "用读文件工具按 offset/limit 分页读完这个文件"}
```

**然后按 `offset` / `limit` 分页读完它**（每页 ~800 行），不要一次读整个文件。
再大就用 CLI 切片，连文件都不用读：

```bat
wechat-read summarize "群名" --format text --limit 800 --offset 800
```

### 什么时候才要 `--format pack`（JSON，默认）

**只有第 3 步 `render --verify-against` 需要 JSON 形态**。要校验结论时再取一次：

```bat
wechat-read summarize "群名" --format pack --output pack.json
```

JSON 形态体积约为文本的 **4 倍**（1115 条 → 296 KB / 11228 行），
**不要用它来读消息** —— 那是文本形态的活。

> ⚠️ **两个形态都用 `--output` 落盘，不要用 shell 的 `>` 重定向。**
> PowerShell 的 `>` 不可控 —— **5.1 写 UTF-16、7.x 写带 BOM 的 UTF-8**，
> 两者都会让后续读取报编码错，逼你额外写一个"清洗编码"的脚本。
> `--output` 由程序自己写标准 UTF-8，没有这个环节。

行格式（每行一条，字段用 ` | ` 分隔，第 4 段起整段是内容）：

```
<消息id> | <MM-DD HH:MM> | <发送者> | <内容>
```

开头的 `#` 行是范围、对账（`loss` 必须为 0）、参与人统计 —— 先读它们。

| 参数 | 说明 |
|---|---|
| `chat` | 会话名或 wxid（必填；省略则走日报） |
| `--since` / `--until` | `YYYY-MM-DD` 或 epoch 秒；**都省略 = 今天** |
| `--all` | 不限时间，从最早一条开始取（"这个群从头到现在聊了什么"） |
| `--format` | `text`（读消息用）或 `pack`（默认，`render --verify-against` 用） |
| `--limit` / `--offset` | 仅 `text`：按行切片，用于把大包切成几段 |
| `--output` | 写文件（中间产物）；不带给则输出到 stdout |
| `--include-low-value` | 保留低信息量消息（默认过滤） |

返回结构：

```json
{
  "kind": "chat_summary_input",
  "chat": {"username": "...", "display_name": "群名", "is_group": true},
  "range": {"since": "...", "until": "..."},
  "messages": [
    {"id": "34567890123@chatroom:7484", "time": "...", "sender": "成员丙",
     "type": "text", "content": "...", "low_value": false, "reply_signal": false}
  ],
  "excluded_low_value": 43,
  "reply_signals": ["34567890123@chatroom:7501"],
  "stats": {"message_count": 200, "low_value_count": 43,
            "type_breakdown": {...}, "top_senders": [...]},
  "instructions": {...}
}
```

> `messages[]` 是该时间范围内的**全部消息**（不是抽样），只过滤了低信息量。
> `low_value` 字段仍保留，便于你复核。
> `instructions` 内嵌了同样的规则，包是自描述的。

## 第 2 步：你来做总结

**读第 1 步的 `chat.txt`**（已是一条一行的纯文本，用读文件工具分页读完），
基于它产出结论 JSON。**用你的写文件能力直接写** ——
你写出来的就是 JSON，不需要脚本去"构造合法 JSON"：


```json
{
  "kind": "chat_summary",
  "chat": {"username": "...", "display_name": "...", "is_group": true},
  "range": {"since": "...", "until": "..."},
  "overview": "一句话总览这个会话在聊什么",
  "topics": [{
    "title": "话题标题（聚类命名）",
    "summary": "该话题核心内容 2-4 句",
    "key_points": ["要点1", "要点2"],
    "participants": ["参与者显示名"],
    "source_message_ids": ["34567890123@chatroom:7484"],
    "inferred": false,
    "confidence": "high"
  }],
  "decisions": [{
    "text": "明确的结论/决定（讨论中、没定论的不要写成结论）",
    "by": ["谁"],
    "source_message_ids": ["..."],
    "inferred": false, "confidence": "high"
  }],
  "actions": [{
    "text": "待办/约定",
    "owner": "谁去做|null",
    "due": "什么时候|null",
    "source_message_ids": ["..."],
    "inferred": false, "confidence": "medium"
  }],
  "participants": [{"name": "张三", "message_count": 120}],
  "stats": {"message_count": 200, "low_value_count": 43}
}
```

**字段说明**

| 字段 | 说明 |
|---|---|
| `overview` | 一句话总览，不要复述 |
| `topics[].title` | 话题名，要让人看懂（不要"消息讨论"这种空话） |
| `topics[].key_points` | 该话题下的要点，2-5 条 |
| `inferred` | `true` = 你的归纳推测；`false` = 消息里明说的 |
| `confidence` | `high` / `medium` / `low`；`inferred: true` 时不要给 `high` |
| `source_message_ids` | 必须来自输入包的 `id` 字段 |

## 第 3 步：交付

**默认：直接在聊天框回答。不要生成 HTML 文件。**

> 想少写点字就直接渲染文本骨架：`wechat-read render summary.json --format text`
> —— 它把话题/决定/待办排成中文文本，你在聊天框里改改就能用。

除非用户明说了「存成网页」「生成 HTML」「导出」「保存成文件」——
那才走下面的可选渲染。

### 默认路径：聊天框回答

把你总结出的内容直接写出来，不用调 `render`：

```
【XX 群】2026-09-29
今天主要在聊三件事：

1. 纳指持仓策略 —— 讨论在高位分批建仓摊低成本，倾向小额分批、不择时。

2. B 站扶持计划 —— 有人分享了新号入驻的申请门槛（近三月发作品 ≤4 篇、
   粉丝 <1w），群里两人表示要试。

3. 团建安排 —— 定在周六森林公园，集合时间从 10 点提前到 9 点。
```

**但结论 JSON 仍然要写。** 第 2 步产出的 `结论.json` 是缓存所需的
中间产物，写它 + 跑一次校验，下次才不用重新分析：

```bat
wechat-read render 结论.json --verify-against pack.json --format json
```

`--format json` + 不带 `--output` 就只做校验和存缓存，不会给你生成文件。
**一定要带 `--verify-against`** —— 它校验你写的 `source_message_ids`
是否真实存在（丢弃编造的），并把结论存进缓存供下次增量使用。

若丢弃了 id，stderr 会提示条数 —— 说明你引用了不存在的消息，
**应该修正结论而不是忽略提示**。

### 可选：用户要文件时才渲染

用户明确要网页/文件时，才加 `--output`：

```bat
wechat-read render 结论.json --verify-against pack.json --format html --output 摘要.html
```

| 格式 | 用途 |
|---|---|
| `json` | 只校验+存缓存，不产文件（**默认路径用它**） |
| `text` | Agent 可读的紧凑文本 |
| `markdown` | 给人看，带 checkbox |
| `html` | 单文件网页，自带样式（用户要网页时才用） |

## 增量

输入包里的 `incremental` 字段会告诉你本次有多少新消息需要处理
（`new_message_count`）；`previous_summary` 是该会话已有的结论，
**在它基础上补充即可，不要重新分析已有的部分**。

## 规则

**必须**

- 先聚类话题，再总结
- 每条结论带 `source_message_ids`，且 id 真实存在
- 区分事实（`inferred: false`）与推断（`inferred: true`）
- 用中文输出

**禁止**

- 逐条复述消息
- 编造 `source_message_ids`
- 把推断写成事实
- 没有结论时硬凑 `decisions` / `actions`
- 让低信息量内容单独成话题

**判断尺度**

- `decisions`：只有明确达成一致才写；讨论中、设想中的不写
- `actions`：要有明确的动作和对象；"可以看看"不算待办
- `inferred: true` 的典型场景：你从多条消息归纳出的模式、
  你推测的意图、你补全的未明说信息

## 完整示例

```bat
rem 1. 取文本形态（读消息用；直接写 UTF-8 文件，不要用 > 重定向）
wechat-read summarize "示例群D" --since 2026-09-27 --format text --output chat.txt

rem 2. 你是模型 —— 分页读完 chat.txt，产出 summary.json（用你的写文件能力直接写）

rem 3. 校验要 JSON 形态，再取一次 pack（同一个范围）
wechat-read summarize "示例群D" --since 2026-09-27 --output pack.json

rem 4. 校验+存缓存（不产文件），然后在聊天框把内容回答给用户
wechat-read render summary.json --verify-against pack.json --format json
```

> 全程**不需要写任何过渡脚本**：内容由 `--format text` 直接给成一条一行的文本，
> 结论由你直接写成 JSON。如果你发现自己在写 `python -c` 去打印消息、
> 或写"清洗编码""导出文本"的脚本，说明用错了形态 ——
> 读消息应该用 `--format text`，而不是去啃 JSON 或手搓脚本。
>
> 用户在聊天框里问的，就在聊天框里答。**不要自作主张生成 HTML 文件。**

输出示例：

```
【示例群D】会话摘要
范围：2026-09-27 ~ 2026-09-29
消息 200 条（其中低信息量 43 条已弱化）

围绕美股定投与内容变现的日常交流。

── 话题（1 个）──
1. 纳指持仓策略
   讨论在高位分批建仓以摊低持仓成本。
   · 认同小额分批
   · 不看择时，定投为主
   参与者：成员庚、成员丙
  ← 34567890123@chatroom:7476, 34567890123@chatroom:7477

── 结论 / 决定 ──
· 采用小额分批而非一次性买入（成员庚） [推断·medium]
  ← 34567890123@chatroom:7477
```

## 失败处理

| 情况 | 表现 | 怎么办 |
|---|---|---|
| 会话不存在 | `CHAT_NOT_FOUND` | 先用 `contacts search` 找 |
| 名字有歧义 | `AMBIGUOUS_NAME` + `candidates` | 用 `username` 重调，不要猜 |
| 范围内无消息 | `NO_MESSAGE_DB` | 换时间范围或换会话 |
