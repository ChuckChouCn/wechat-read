"""微信数据目录探测（Windows）。

**不假设任何固定路径。** 每个人的微信数据目录都是自己设的，
可能在任何盘符、任何目录名下，所以这里靠两条通用信号定位：

  1. 微信自己的配置文件 %APPDATA%\\Tencent\\xwechat\\config\\*.ini
     —— 里面记录着用户设的数据根目录。任何装了微信的机器都有，
        几秒读完，最准。

  2. 标志目录 `xwechat_files`
     —— 微信在任何人的机器上都会建这个**固定名称**的目录，
        结构恒为 <用户设的根目录>\\xwechat_files\\<wxid>_<hash>\\db_storage
        所以找它，而不是猜"根目录叫什么"。

调用顺序建议（快 → 慢）：

    detect()            → 配置 + 浅层找标志目录（通常 < 1 秒）
    若返回空，问用户     → 用户知道路径最省事
    用户也不知道        → detect(deep=True) 更深一层

每个候选都会验证是否真的是可用的 db_storage（含加密 .db 文件），
避免误报。
"""
from __future__ import annotations

import glob
import os
import string
import sys

# 微信数据目录的**固定标志**：任何人的安装里都是这个结构
#     <用户设的任意根目录>\xwechat_files\<wxid>_<hash>\db_storage
# 所以兜底靠找标志目录名，而不是猜「根目录叫什么」——根目录名因人而异。
_WECHAT_MARKER = "xwechat_files"
_ACCOUNT_DB_DIR = "db_storage"

# 兜底扫描时跳过这些明显无关的目录（提速度，不是靠它们定位）
_SKIP_DIRS = {
    "windows", "$recycle.bin", "system volume information", "programdata",
    "appdata", "node_modules", ".git", "__pycache__", ".venv", "venv",
    "site-packages", "winsxs", "assembly", "installer", "$windows.~bt",
    "onedrive", "temp", "tmp", "cache",
}


class Candidate:
    def __init__(self, path: str, source: str, db_count: int = 0,
                 account: str | None = None, mtime: float = 0.0):
        self.path = path          # db_storage 的绝对路径
        self.source = source      # 怎么找到的
        self.db_count = db_count  # 里面有多少个 .db
        self.account = account    # 账号目录名（wxid_xxx_hash）
        self.mtime = mtime        # 消息目录的修改时间（用于挑最新）

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "source": self.source,
            "db_count": self.db_count,
            "account": self.account,
        }


def _count_dbs(db_storage: str, limit: int = 3) -> int:
    """数一下这个 db_storage 里有多少个 .db（含子目录）。"""
    n = 0
    try:
        for root, _dirs, files in os.walk(db_storage):
            n += sum(1 for f in files if f.endswith(".db"))
            if n >= 100:
                break
    except OSError:
        return 0
    return n


def _looks_like_db_storage(path: str, min_dbs: int = 1) -> bool:
    """判断是否像可用的 db_storage。"""
    return os.path.isdir(path) and _count_dbs(path) >= min_dbs


def _mtime_of(path: str) -> float:
    msg = os.path.join(path, "message")
    target = msg if os.path.isdir(msg) else path
    try:
        return os.path.getmtime(target)
    except OSError:
        return 0.0


def _from_config(appdata: str) -> list[Candidate]:
    """从微信配置读数据根目录（最准）。"""
    out: list[Candidate] = []
    cfg_dir = os.path.join(appdata, "Tencent", "xwechat", "config")
    if not os.path.isdir(cfg_dir):
        return out

    roots: list[str] = []
    for ini in glob.glob(os.path.join(cfg_dir, "*.ini")):
        for enc in ("utf-8", "gbk"):
            try:
                with open(ini, "r", encoding=enc) as fh:
                    content = fh.read(1024).strip()
                break
            except (UnicodeDecodeError, OSError):
                content = ""
        # 配置里可能重复拼接（如 "D:\aD:\a"），取前半
        if not content or any(c in content for c in "\r\n\x00"):
            continue
        if len(content) % 2 == 0:
            half = content[:len(content) // 2]
            if half == content[len(content) // 2:]:
                content = half
        if os.path.isdir(content):
            roots.append(content)

    return _scan_roots(roots, "微信配置")


def _scan_roots(roots: list[str], source: str) -> list[Candidate]:
    """从一个或多个数据根目录里找出所有 db_storage。

    实测结构（微信 4.x）：
        <根目录>\\xwechat_files\\<wxid>_<hash>\\db_storage
    所以中间是两层，不是一层；但也兼容直接放在根目录下的情况。
    """
    out: list[Candidate] = []
    seen: set[str] = set()
    for root in roots:
        patterns = [
            os.path.join(root, "xwechat_files", "*", "db_storage"),
            os.path.join(root, "*", "db_storage"),          # 兜底：少一层
            os.path.join(root, "db_storage"),               # 兜底：就是它
        ]
        for pattern in patterns:
            for match in glob.glob(pattern):
                real = os.path.normcase(os.path.abspath(match))
                if real in seen:
                    continue
                if not _looks_like_db_storage(match):
                    continue
                seen.add(real)
                out.append(Candidate(
                    path=match, source=source,
                    db_count=_count_dbs(match),
                    account=os.path.basename(os.path.dirname(match)),
                    mtime=_mtime_of(match)))
    return out


def _drive_roots() -> list[str]:
    """Windows 上可用的盘符。"""
    if sys.platform != "win32":
        return []
    roots = []
    for letter in string.ascii_uppercase:
        d = f"{letter}:\\"
        if os.path.exists(d):
            roots.append(d)
    return roots


def _from_marker_scan(max_depth: int = 3, source: str = "目录扫描",
                      log=None) -> list[Candidate]:
    """在各盘符里找 `xwechat_files` 标志目录。

    这是**基于证据**的找法：不猜用户的根目录叫什么，而是找微信自己
    创建的那个固定名称的目录。

    深度按「目录名段数」算，max_depth=3 表示最多到
        D:\\  (0) → D:\\<a>  (1) → D:\\<a>\\<b>  (2) → D:\\<a>\\<b>\\<c>  (3)
    常见的 D:\\wechat_document\\xwechat_files 落在 2 层，所以要给到 3
    才不会被自己的边界条件排除掉。
    """
    out: list[Candidate] = []
    seen: set[str] = set()

    for drive in _drive_roots():
        base_depth = drive.rstrip("\\").count("\\")
        try:
            walker = os.walk(drive)
        except OSError:
            continue
        for root, dirs, _files in walker:
            depth = root.rstrip("\\").count("\\") - base_depth
            # 先判断当前目录是不是标志目录（不能先按深度剪枝，
            # 否则恰好落在边界层上的标志目录会被漏掉）
            if os.path.basename(root).lower() == _WECHAT_MARKER:
                dirs[:] = []          # 找到就不再深入它内部
                for cand in _scan_roots([os.path.dirname(root)], source):
                    real = os.path.normcase(os.path.abspath(cand.path))
                    if real not in seen:
                        seen.add(real)
                        out.append(cand)
                if log:
                    log(f"    在 {root} 发现微信数据")
                continue
            # 超过深度就停止下探
            if depth >= max_depth:
                dirs[:] = []
                continue
            # 跳过明显无关目录，提速
            dirs[:] = [d for d in dirs
                       if d.lower() not in _SKIP_DIRS
                       and not d.startswith("$")]
    return out


def _from_deep_scan(max_depth: int = 4, log=None) -> list[Candidate]:
    """更深一层（max_depth=4）的标志目录扫描。

    用于用户把数据放得很深的情况，比如
        D:\\我的文档\\备份\\微信\\xwechat_files\\...
    比默认扫描慢，所以单独作为最后兜底。
    """
    return _from_marker_scan(max_depth=max_depth, source="深度扫描", log=log)


def detect(deep: bool = False, log=None) -> list[Candidate]:
    """自动探测微信数据目录。

    策略（快 → 慢）：

        1. 读微信配置 %APPDATA%\\Tencent\\xwechat\\config\\*.ini
           配置里直接记录了用户设的数据根目录，几秒出结果，最准。

        2. 扫标志目录 `xwechat_files`（各盘符浅层，depth<=2）
           微信在任何人的机器上都会建这个固定名称的目录，
           所以找它、而不是猜用户的根目录叫什么。

        3. deep=True 时更深一层（depth<=4），覆盖放得很深的情况。

    **调用顺序建议**：本函数负责「自动」的部分。若返回空，
    应由上层（Agent）去问用户；用户也不知道，再用 deep=True 重试。

    Returns:
        候选列表，多个账号时按消息目录修改时间倒序（最近用的在前）
    """
    def _log(msg):
        if log:
            log(msg)

    found: list[Candidate] = []
    seen: set[str] = set()

    def _add(cands, label):
        added = 0
        for c in cands:
            real = os.path.normcase(os.path.abspath(c.path))
            if real in seen:
                continue
            seen.add(real)
            found.append(c)
            added += 1
        _log(f"  {label}: 找到 {added} 个")
        return added

    # 1) 微信配置（最快最准）
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        _log("[1/3] 读微信配置…")
        _add(_from_config(appdata), "微信配置")

    # 2) 找标志目录（浅层）
    if not found:
        _log("[2/3] 扫描各盘符的 xwechat_files 目录…")
        _add(_from_marker_scan(max_depth=3, source="目录扫描", log=_log), "目录扫描")

    # 3) 更深一层（可选）
    if not found and deep:
        _log("[3/3] 深度扫描（可能较慢，请稍候）…")
        _add(_from_deep_scan(max_depth=5, log=_log), "深度扫描")

    found.sort(key=lambda c: c.mtime, reverse=True)
    return found


def best(db_dir: str | None = None, deep: bool = False,
         log=None) -> Candidate | None:
    """返回最可能的数据目录。"""
    if db_dir:
        if _looks_like_db_storage(db_dir):
            return Candidate(path=db_dir, source="用户指定",
                             db_count=_count_dbs(db_dir))
        return None
    cands = detect(deep=deep, log=log)
    return cands[0] if cands else None
