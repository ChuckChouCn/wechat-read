# 总结单个会话

用户点名了某个群/某人，要求总结其聊天内容时走这条路。

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

**用 `--output` 直接写文件，不要用 shell 的 `>` 重定向。**

```bat
wechat-read summarize "群名" --since 2026-09-29 --until 2026-09-29 --output pack.json
```

> ⚠️ **为什么必须用 `--output` 而不是 `>`：** 输入包可能有几百 KB，
> 要落盘才能读。而 PowerShell 的 `>` 不可控 —— **5.1 写 UTF-16、
> 7.x 写带 BOM 的 UTF-8**，两者都会让后续读取报编码错，逼你额外写一个
> "清洗编码"的脚本。`--output` 由程序自己写标准 UTF-8，没有这个环节。

写完只回一个简短回执（不给 stdout 灌几百 KB）：

```json
{"written": "pack.json", "kind": "chat_summary_input",
 "messages": 317, "bytes": 61420, "hint": "把该文件路径连同提示词一起交给模型"}
```

| 参数 | 说明 |
|---|---|
| `chat` | 会话名或 wxid（必填；省略则走日报） |
| `--since` / `--until` | `YYYY-MM-DD` 或 epoch 秒；**都省略 = 今天** |
| `--all` | 不限时间，从最早一条开始取（"这个群从头到现在聊了什么"） |
| `--output` | 写输入包到文件（中间产物）；不带给则输出到 stdout |
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

**直接读 `pack.json`，不要写脚本去 dump 消息文本。**
`messages[]` 已经是干净的 JSON 数组，你的读文件能力就能直接看；
再写一个 `dump_xxx.py` 去提取文本是多余的一层。

基于 `messages[]`，按下面 schema 产出结论 JSON。**同样用你的写文件能力
直接写**，不要写脚本去"构造合法 JSON" —— 你写出来的就是 JSON：


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
rem 1. 取输入包（直接写 UTF-8 文件，不要用 > 重定向）
wechat-read summarize "示例群D" --since 2026-09-27 --output pack.json

rem 2. 你是模型 —— 读 pack.json，产出 summary.json（用你的写文件能力，直接写 UTF-8）

rem 3. 校验+存缓存（不产文件），然后在聊天框把内容回答给用户
wechat-read render summary.json --verify-against pack.json --format json
```

> 全程**不需要写任何过渡脚本**。第 1 步落盘、第 2 步你直接产出结论 JSON、
> 第 3 步校验 —— 三步就够。如果发现自己要写"清洗编码""导出消息文本"
> 这类辅助脚本，说明哪里用错了（多半是第 1 步用了 `>`）。
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
