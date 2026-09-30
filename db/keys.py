"""密钥装载：把 key-provider 产出的 keys.json 映射到具体数据库文件。

keys.json 按 salt 索引（salt 是每个库文件前 16 字节，随机且唯一），
所以查某个库的密钥 = 读它前 16 字节得到 salt，再查表。
这样即使库文件被移动/改名也能命中。
"""
from __future__ import annotations

import json
import os
import warnings

from .crypto import SALT_SZ, KEY_SZ


class KeyStore:
    def __init__(self, salt_to_key: dict[str, str], source: str = ""):
        self._map = dict(salt_to_key)
        self.source = source

    @classmethod
    def load(cls, path: str) -> "KeyStore":
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        salt_to_key: dict[str, str] = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                # key-provider 格式: {salt: {enc_key, salt, dbs}}
                salt = value.get("salt") or key
                enc = value.get("enc_key")
            else:
                # 兼容 {salt: enc_key} 简表
                salt, enc = key, value
            if not enc:
                continue
            if len(enc) != KEY_SZ * 2:
                warnings.warn(f"跳过长度异常的密钥 (salt={salt}): {enc[:16]}...")
                continue
            salt_to_key[salt.lower()] = enc.lower()
        return cls(salt_to_key, source=path)

    def __len__(self) -> int:
        return len(self._map)

    def __contains__(self, salt_hex: str) -> bool:
        return salt_hex.lower() in self._map

    def salt_of(self, db_path: str) -> str:
        """读库文件前 16 字节作为 salt。"""
        with open(db_path, "rb") as fh:
            head = fh.read(SALT_SZ)
        if len(head) < SALT_SZ:
            raise ValueError(f"文件过小，不是有效数据库: {db_path}")
        return head.hex()

    def key_for_path(self, db_path: str) -> bytes | None:
        """按文件路径取密钥，没有则返回 None。"""
        salt = self.salt_of(db_path)
        hexkey = self._map.get(salt)
        return bytes.fromhex(hexkey) if hexkey else None

    def key_hex_for_path(self, db_path: str) -> str | None:
        return self._map.get(self.salt_of(db_path))

    def missing_salts(self, db_paths) -> list[str]:
        return [p for p in db_paths if self.salt_of(p) not in self._map]


def find_db_files(db_dir: str, min_size: int = 0) -> list[str]:
    """递归列出目录下所有 .db（排除 -wal/-shm 附属文件）。"""
    out = []
    for root, _dirs, files in os.walk(db_dir):
        for name in files:
            if name.endswith(("-wal", "-shm")):
                continue
            if not name.endswith(".db"):
                continue
            path = os.path.join(root, name)
            try:
                if os.path.getsize(path) >= min_size:
                    out.append(path)
            except OSError:
                continue
    return sorted(out)
