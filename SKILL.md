---
name: wechat-read
description: >-
  读取和总结本机微信聊天记录（Windows）。查询会话列表、读某会话消息、取最近消息、
  搜联系人、看群成员、数消息条数；总结某个群/会话的内容；生成全部会话的微信日报。
  当用户问「谁给我发了消息」「某群最近聊了什么」「搜一下某个联系人」「总结今天微信」
  「微信日报」「XX 群在聊什么」「今天有什么重要的」时使用。
metadata:
  short-description: 微信聊天记录读取与总结（Windows）
  version: "0.5.0"
---

# wechat-read

读取本机微信聊天数据并做归纳。**只读**，不发送、不修改任何微信数据。

## 怎么用

**命令就在本文件旁边** —— 用你已知的 `SKILL.md` 绝对路径拼出来：

```bat
& "<SKILL_DIR>\wechat-read.cmd" sessions --limit 20
& "<SKILL_DIR>\wechat-read.cmd" contacts search "群名"
& "<SKILL_DIR>\wechat-read.cmd" summarize "群名" --output pack.json
```

`<SKILL_DIR>` = **你读到本 `SKILL.md` 时用的那个目录**，直接用，不用找。

> 下文为简洁，命令都写成 `wechat-read <参数>`。
> 实际执行时替换为 `& "<SKILL_DIR>\wechat-read.cmd" <参数>`。

**为什么走这个 `.cmd` 而不是直接 `python cli.py`**：Windows 上 `python`
经常指向一个存根（`WindowsApps` 里的 0 字节假 exe），跑起来静默失败。
`.cmd` 会自己找到真 Python。用它对你有好处，不用你操心 Python 在哪。

## 别把工作目录搞错

上面的命令用**绝对路径**指向 `.cmd`，所以**在任何目录下都能跑** ——
不用先 `cd`。`--output pack.json` 这种相对路径，落点是你当前的工作目录。

## 交付形态：聊天框还是文件

**大部分情况在聊天框回答。只有两种情况产出文件。**

| 用户说的话 | 交付 |
|---|---|
| 「XX 群在聊什么」「总结一下这个群」 | 💬 聊天框 |
| 「今天有什么重要的」「今天谁找我了」 | 💬 聊天框 |
| 「总结今天微信」 | 💬 聊天框 |
| **「日报」** —— 「微信日报」「今天的日报」「来个日报」 | 📄 **HTML 文件** |
| 「存成网页」「导出」「保存成文件」 | 📄 文件 |

**「日报」这个词本身就是交付意图** —— 用户说日报，就是要一份能存下来、
能翻看的文档，默认产出 HTML。

注意和它相邻的说法区分开：

- **「总结今天微信」** → 只是**问**，在聊天框答
- **「微信日报」** → 是**要一份文档**，产出 HTML

差别只在"日报"两个字，但这正是用户的意图信号。

## 选哪条路

| 用户想要 | 怎么做 |
|---|---|
| 查会话 / 读消息 / 搜人 / 看群成员 / 数条数 | 直接用查询命令 |
| 「总结一下某个群」 | 读 `guides/summarize-chat.md` |
| 「总结今天微信」「微信日报」 | 读 `guides/daily-digest.md` |
| 第一次用、报 `KEYS_MISSING` | 读 `guides/setup.md` |

**命令参数不确定就跑 `--help`**（如 `wechat-read summarize --help`），
或看 `guides/commands.md`。**不要读程序源码。**

## 查询命令

```bat
wechat-read sessions --limit 20          rem 会话列表
wechat-read messages "群名" --limit 50   rem 某会话消息（翻页）
wechat-read recent --limit 50            rem 跨会话最近消息
wechat-read contacts search "关键词"      rem 搜联系人
wechat-read contacts get "群名"           rem 联系人详情
wechat-read members "群名"                rem 群成员
wechat-read count "群名"                  rem 消息计数
wechat-read status                        rem 环境自检
```

> ⚠️ `messages` / `recent` 是**翻页接口**（有 `--limit` 上限）。
> **不要用它们做总结** —— 只会拿到一小段。总结一律用 `summarize`。

## 最快路径：问某个群在聊什么

```bat
wechat-read contacts search "群名"                       rem 1. 找到群
wechat-read summarize "群名" --format text --output chat.txt   rem 2. 取消息
```

然后**读 `chat.txt`**（一条消息一行），据此回答。三步结束 ——
不要读源码、不要 `--no-cache` 重跑、不要生成 HTML。

- **不用带 `--since`** —— 省略即今天（`00:00:00 ~ 23:59:59`）。
- **不用加 `--no-cache`** —— 缓存给的 `messages[]` 是新消息，
  加上 `previous_summary` 就是完整视图。看到 "新消息数" 比总数少是正常的。

### 怎么把 `chat.txt` 读进上下文（关键）

`--format text` 让 CLI 输出**一条消息一行**的紧凑文本，体积约为 JSON 的
**1/4**（实测 1115 条消息：JSON 296 KB / 11228 行 → 文本 76 KB / 1122 行）。
但**大群仍可能超过读文件工具的单次上限**，所以按下面来：

1. **先看回执里的 `lines`** —— 它告诉你这个文件有多少行，据此算要读几页
   （按每页 ~800 行估）。这一步是让你**不必试错**。
2. **按 `offset` / `limit` 分页读**，直到读完。文件头的 `#` 行是范围与
   对账信息，每页都会带上，方便你随时核对。
3. 也可以让 CLI 直接切片输出（不落盘、不读文件）：

```bat
wechat-read summarize "群名" --format text --limit 800             rem 第 1 段
wechat-read summarize "群名" --format text --limit 800 --offset 800  rem 第 2 段
```

> **为什么不用 `python -c "..."` 去打印消息**：Windows PowerShell 会把
> 传给可执行程序的参数里的双引号剥掉，`python -c "import json; ..."` 这种
> 单行命令**必报 SyntaxError**（只要命令里出现 `m["content"]` 这类字典取值）。
> 上面的 `--format text` 就是为替代它而加的。要用 Python 请写脚本文件，
> 别写单行 `-c`。

> **先看一眼返回值里的 `range`**：它表示实际取到的时间范围。
> 如果 `range.since` 明显早于你要的范围，说明时间参数没生效 ——
> 停下来核对，不要把不相干的历史消息混进结论。
>
> 想看**全部历史**（不是今天）要显式说：加 `--all`，或
> `--since 2026-09-23 --until 2026-09-30`。**省略时间参数永远是今天。**

## 铁律

1. **只读**。不发送、不修改、不删除任何微信数据。
2. **推理由你完成**。本项目不调用任何 LLM、不需要 API Key —— **你自己就是模型**。
3. **不直接查数据库**。一律通过 `wechat-read`。

## 错误处理

失败时 stdout 仍是 JSON，退出码非 0：

| code | 怎么办 |
|---|---|
| `CHAT_NOT_FOUND` | 换名字，或先 `contacts search` |
| `AMBIGUOUS_NAME` | 从 `candidates` 选一个，用 `username` 重调 |
| `NO_MESSAGE_DB` | 该会话没有消息表，换一个 |
| `INVALID_PARAM` | 按 `message` 修正参数 |
| `KEYS_MISSING` | 未初始化 → 读 `guides/setup.md` |
| `INTERNAL_ERROR` | 报给用户 |

## 取数据用 `--output`，不要用 `>`

PowerShell 的 `>` 写出来的编码不可控（5.1 写 UTF-16、7.x 写带 BOM 的 UTF-8），
后续读取会报编码错。`--output` 由程序自己写标准 UTF-8。

```bat
wechat-read summarize --output pack.json      rem 对
wechat-read summarize > pack.json             rem 错
```

**写结论 JSON 时**用你自己的写文件能力直接写 UTF-8，同样别经过 `>`。

## PowerShell 引号：别写 `python -c`

本 Skill 只在 Windows 上跑，而 PowerShell 调用外部程序时**会把参数里的
双引号剥掉**。所以这种写法 100% 失败：

```powershell
python -c "import json; d=json.load(open('pack.json')); print(d['messages'][0]['content'])"
# PowerShell 把内层双引号吃掉 → Python 收到残缺代码 → SyntaxError
```

**要跑 Python 就写脚本文件再执行**，不要用单行 `-c`：

```powershell
# 对：先写一个 read_pack.py，再执行
python read_pack.py
```

**但更常见的情况是你根本不需要 Python** —— 读消息用
`summarize --format text`（见上文「最快路径」），
它已经把内容整理成一条一行的纯文本了。

## 只支持 Windows

密钥提取需要读 Windows 微信进程的内存。看到平台相关报错就是环境不对。
