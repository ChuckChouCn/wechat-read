"""密钥缓存 — 首次提取后持久化，之后复用，失效时提示重新初始化。

存放位置：`~/.weixin-read/keys.json`（用户级，不随项目目录走）。
这样无论在哪个目录运行 CLI，都能拿到同一份密钥。

失效检测（关键）：
    密钥与数据库**按 salt 一一绑定**（salt 是每个库文件的前 16 字节）。
    所以：
      - 微信换了账号 / 清了数据 → 库文件重建 → salt 变了 → 已存密钥全部失效
      - 微信新增了数据库        → 出现没有密钥的新 salt → 部分缺失
    两种都能通过「拿当前库的 salt 去比对缓存」检测出来，不需要真的解密。

校验分两级：
    check()      —— 轻量：只读每个库前 16 字节比对 salt（快，默认用这个）
    verify_deep()—— 逐库 HMAC 校验首个库（慢，仅怀疑异常时用）
"""
from __future__ import annotations

import json
import os
import time

from .crypto import KEY_SZ, PAGE_SZ, SALT_SZ, verify_enc_key
from .keys import find_db_files

# 用户级状态目录
STATE_DIR = os.path.expanduser("~/.weixin-read")
KEYS_FILE = os.path.join(STATE_DIR, "keys.json")
META_FILE = os.path.join(STATE_DIR, "meta.json")

# 缓存状态
STATUS_OK = "ok"                  # 密钥齐全且有效
STATUS_MISSING = "missing"        # 还没提取过
STATUS_STALE = "stale"            # salt 对不上，密钥已失效
STATUS_PARTIAL = "partial"        # 部分库缺密钥
STATUS_EMPTY = "empty_db"         # 目录里没有数据库
STATUS_INVALID = "invalid"        # 密钥文件损坏


def is_valid_key_hex(s: str) -> bool:
    if not isinstance(s, str) or len(s) != KEY_SZ * 2:
        return False
    try:
        bytes.fromhex(s)
        return True
    except ValueError:
        return False


def load_keys(path: str = KEYS_FILE) -> dict[str, str]:
    """读密钥文件，返回 {salt_hex: enc_key_hex}。

    兼容两种格式：
      - 详细：{salt: {enc_key, salt, dbs}}   （extract_keys.py --out 的产出）
      - 简表：{salt: enc_key}
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}

    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return {}
    for k, v in raw.items():
        if isinstance(v, dict):
            salt = v.get("salt") or k
            enc = v.get("enc_key")
        else:
            salt, enc = k, v
        if is_valid_key_hex(enc) and isinstance(salt, str):
            out[salt.lower()] = enc.lower()
    return out


def save_keys(salt_to_key: dict[str, str], path: str = KEYS_FILE,
              db_dir: str | None = None, extra: dict | None = None) -> str:
    """保存密钥，并在文件里附带元信息（便于诊断与失效检测）。

    写法：{salt: {enc_key, salt}}，另存一个 `_meta` 键记录来源与时间。
    读取时 `_meta` 会被忽略（load_keys 只认 32 字节 hex 的条目）。
    """
    path = path or KEYS_FILE
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)

    payload: dict = {
        salt: {"enc_key": k, "salt": salt}
        for salt, k in sorted(salt_to_key.items())
    }
    meta = {
        "_schema": 1,
        "extracted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "db_dir": db_dir,
        "count": len(salt_to_key),
    }
    if extra:
        meta.update(extra)
    payload["_meta"] = meta

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)          # 原子替换，避免写一半损坏

    try:
        os.chmod(path, 0o600)      # 含密钥，仅本人可读
    except OSError:
        pass
    return path


def read_meta(path: str = KEYS_FILE) -> dict:
    """读密钥文件的元信息（提取时间、来源目录等）。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    if isinstance(raw, dict):
        return raw.get("_meta") or {}
    return {}


def scan_db_salts(db_dir: str) -> dict[str, list[str]]:
    """扫描目录，返回 {salt_hex: [相对路径, ...]}。只读前 16 字节。"""
    out: dict[str, list[str]] = {}
    for path in find_db_files(db_dir, min_size=PAGE_SZ):
        try:
            with open(path, "rb") as fh:
                head = fh.read(SALT_SZ)
        except OSError:
            continue
        if len(head) < SALT_SZ:
            continue
        salt = head.hex()
        rel = os.path.relpath(path, db_dir)
        out.setdefault(salt, []).append(rel)
    return out


class KeyCacheStatus:
    """密钥缓存的状态快照。"""

    def __init__(self, status: str, keys_path: str, total: int = 0,
                 covered: int = 0, missing: list[str] | None = None,
                 stale: list[str] | None = None, meta: dict | None = None,
                 message: str = ""):
        self.status = status
        self.keys_path = keys_path
        self.total = total            # 当前目录里不同 salt 的个数
        self.covered = covered        # 有密钥的个数
        self.missing = missing or []  # 缺密钥的 salt（新库）
        self.stale = stale or []      # 缓存里有但目录里已不存在的 salt
        self.meta = meta or {}
        self.message = message

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def need_init(self) -> bool:
        return self.status in (STATUS_MISSING, STATUS_STALE, STATUS_INVALID)

    def to_dict(self) -> dict:
        d = {
            "status": self.status,
            "keys_file": self.keys_path,
            "keys_file_exists": os.path.exists(self.keys_path),
            "db_salts": self.total,
            "covered": self.covered,
        }
        if self.missing:
            d["missing_salts"] = len(self.missing)
            d["missing_examples"] = self.missing[:5]
        if self.stale:
            d["stale_salts"] = len(self.stale)
        if self.meta:
            d["extracted_at"] = self.meta.get("extracted_at")
            d["source_db_dir"] = self.meta.get("db_dir")
        if self.message:
            d["message"] = self.message
        return d


def check(db_dir: str, keys_path: str = KEYS_FILE) -> KeyCacheStatus:
    """检查密钥缓存是否可用（轻量：只比对 salt，不解密）。"""
    db_salts = scan_db_salts(db_dir)
    if not db_salts:
        return KeyCacheStatus(STATUS_EMPTY, keys_path,
                              message=f"目录里没有找到数据库: {db_dir}")

    keys = load_keys(keys_path)
    if not os.path.exists(keys_path):
        return KeyCacheStatus(STATUS_MISSING, keys_path, total=len(db_salts),
                              missing=list(db_salts),
                              message="还没提取过密钥，需要初始化")
    if not keys:
        return KeyCacheStatus(STATUS_INVALID, keys_path, total=len(db_salts),
                              message="密钥文件存在但内容无效（格式损坏或为空）")

    current = set(db_salts)
    cached = set(keys)
    missing = sorted(current - cached)      # 目录里有、密钥里没有
    covered = len(current & cached)

    if covered == 0:
        return KeyCacheStatus(
            STATUS_STALE, keys_path, total=len(current), covered=0,
            missing=missing, stale=sorted(cached),
            meta=read_meta(keys_path),
            message="已存密钥全部失效（数据库 salt 已变，通常因为换了微信账号"
                    "或清了数据），需要重新初始化")

    if missing:
        return KeyCacheStatus(
            STATUS_PARTIAL, keys_path, total=len(current), covered=covered,
            missing=missing, stale=sorted(cached - current),
            meta=read_meta(keys_path),
            message=f"{len(missing)} 个数据库没有密钥（可能是微信新增的库）")

    return KeyCacheStatus(STATUS_OK, keys_path, total=len(current),
                          covered=covered, meta=read_meta(keys_path),
                          message="密钥有效")


def verify_deep(db_dir: str, keys_path: str = KEYS_FILE,
                sample: int = 3) -> dict:
    """深度校验：抽若干库做 HMAC 校验，确认密钥真的能解密。

    比 check() 慢（要读整个首页并做 PBKDF2），仅在怀疑异常时用。
    """
    keys = load_keys(keys_path)
    if not keys:
        return {"verified": 0, "checked": 0, "failures": ["密钥为空"]}

    checked = verified = 0
    failures: list[str] = []
    for path in find_db_files(db_dir, min_size=PAGE_SZ)[:sample]:
        try:
            with open(path, "rb") as fh:
                page1 = fh.read(PAGE_SZ)
        except OSError:
            continue
        if len(page1) < PAGE_SZ:
            continue
        salt = page1[:SALT_SZ].hex()
        key_hex = keys.get(salt)
        if not key_hex:
            failures.append(f"{os.path.relpath(path, db_dir)}: 无密钥")
            continue
        checked += 1
        if verify_enc_key(bytes.fromhex(key_hex), page1):
            verified += 1
        else:
            failures.append(f"{os.path.relpath(path, db_dir)}: HMAC 校验失败")

    return {"verified": verified, "checked": checked, "failures": failures}


def resolve_keys_path(explicit: str | None = None) -> str:
    """决定用哪个密钥文件。

    优先级：显式参数 > 环境变量 WECHAT_KEYS > 用户级缓存。

    默认落到用户级缓存（~/.weixin-read/keys.json），这样在任何目录
    运行都能复用同一份密钥 —— 首次提取一次，之后一直用。
    """
    if explicit:
        return os.path.expanduser(explicit)
    env = os.environ.get("WECHAT_KEYS")
    if env:
        return os.path.expanduser(env)
    return KEYS_FILE
