# 安装说明

一条命令：

```powershell
.\install.ps1
```

它把项目复制到**你 Agent 的 skills 目录**，并装好依赖。

**不碰 PATH、不写注册表** —— 卸载就是删文件夹，不留任何残留。

```
<skills 目录>\wechat-read\
├── SKILL.md          ← Agent 在这里发现 Skill
├── wechat-read.cmd   ← 调用入口，就在 SKILL.md 旁边
├── cli.py
├── guides\
├── requirements.txt
└── db\ parser\ service\ skills\ keyprovider\
```

## 怎么调用

Agent 用**已知道的 `SKILL.md` 路径**拼出 `.cmd` 的绝对路径：

```bat
& "<SKILL_DIR>\wechat-read.cmd" sessions --limit 20
```

`<SKILL_DIR>` = 读到 `SKILL.md` 时用的那个目录。**任何工作目录下都能跑**，
不用先 `cd`，也不用设环境变量。

## 装到哪

脚本会**自动探测**已有 skills 目录，顺序是 `.claude` → `.codex` → `.agents`
→ `config` → `.cursor`。找到哪个就用哪个，不凭空造目录。

**都没找到会停下并告诉你加 `-Dest`**：

```powershell
.\install.ps1 -Dest "D:\你的-agent\skills\wechat-read"
```

## 手动安装

不想跑脚本，就复制整个项目到 skills 目录，再装依赖：

```powershell
Copy-Item -Recurse -Force "<项目路径>" "<skills目录>\wechat-read"

# 把解释器路径记下来，wechat-read.cmd 就不用每次找 Python
"<python.exe 的完整路径>" | Out-File "<skills目录>\wechat-read\python-path.txt" -Encoding ascii

pip install -r "<skills目录>\wechat-read\requirements.txt"
```

> `cli.py` 用自身路径定位所有模块，**搬到哪都能跑**，不用改代码。

## 初始化

```powershell
& "<skills目录>\wechat-read\wechat-read.cmd" init
```

需要微信正在运行。**只做一次**，密钥存到 `%USERPROFILE%\.weixin-read\`。

## 验证

```powershell
& "<skills目录>\wechat-read\wechat-read.cmd" status   # ready 应该是 true
& "<skills目录>\wechat-read\wechat-read.cmd" sessions --limit 5
```

如果 Agent 支持列出已安装的 skill，也确认 `wechat-read` 出现在列表里。

## 卸载

```
删掉 <skills目录>\wechat-read 文件夹即可。
```

没有 PATH 条目、没有注册表项需要清理。

密钥和摘要缓存不在里面（在 `%USERPROFILE%\.weixin-read\`），
确认不再使用本工具时再一并删除 —— **删了密钥要重新 `init`**。

> 密钥能解密全部聊天记录，**不要分享、不要提交到 git、不要放进网盘同步目录**。
