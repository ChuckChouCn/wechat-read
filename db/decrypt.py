"""解密驱动：加密 .db(+WAL) → 可读标准 SQLite 文件。

只读取，不修改微信的原始文件。WAL 在内存里重放，不落盘，
避免对正在运行的微信产生任何副作用。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .crypto import (
    PAGE_SZ,
    apply_wal,
    decrypt_database_bytes,
    verify_enc_key,
)
from .keys import KeyStore


@dataclass
class DecryptResult:
    db_path: str
    out_path: str | None = None
    ok: bool = False
    error: str | None = None
    pages: int = 0
    wal_frames: int = 0
    verified: bool = False
    data: bytes | None = field(default=None, repr=False)


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def decrypt_file(db_path: str, key: bytes, out_path: str | None = None,
                 with_wal: bool = True, verify: bool = True) -> DecryptResult:
    """解密单个数据库。

    Args:
        db_path: 加密数据库路径
        key: 32 字节 raw key
        out_path: 若给出则写入解密后的 SQLite 文件
        with_wal: 是否重放同名 .db-wal（近期数据通常只在 WAL 里）
        verify: 解密前先用 HMAC 校验密钥
    """
    res = DecryptResult(db_path=db_path, out_path=out_path)
    try:
        data = _read(db_path)
    except OSError as exc:
        res.error = f"读取失败: {exc}"
        return res

    if len(data) < PAGE_SZ:
        res.error = f"文件过小 ({len(data)} 字节)"
        return res

    if verify:
        if not verify_enc_key(key, data[:PAGE_SZ]):
            res.error = "HMAC 校验失败：密钥与该库不匹配"
            return res
        res.verified = True

    try:
        plain = bytearray(decrypt_database_bytes(key, data))
    except Exception as exc:  # noqa: BLE001
        res.error = f"解密失败: {type(exc).__name__}: {exc}"
        return res

    res.pages = len(data) // PAGE_SZ

    if with_wal:
        wal_path = db_path + "-wal"
        if os.path.exists(wal_path):
            try:
                res.wal_frames = apply_wal(plain, key, _read(wal_path))
            except Exception as exc:  # noqa: BLE001
                res.error = f"WAL 重放失败: {type(exc).__name__}: {exc}"
                return res

    if not plain.startswith(b"SQLite format 3\x00"):
        res.error = "解密结果缺少 SQLite 文件头"
        return res

    if out_path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
            with open(out_path, "wb") as fh:
                fh.write(plain)
        except OSError as exc:
            res.error = f"写出失败: {exc}"
            return res

    res.data = bytes(plain)
    res.ok = True
    return res


def decrypt_dir(db_dir: str, keystore: KeyStore, out_dir: str | None = None,
                with_wal: bool = True) -> list[DecryptResult]:
    """解密目录下所有能找到密钥的数据库。"""
    from .keys import find_db_files

    results = []
    for path in find_db_files(db_dir, min_size=PAGE_SZ):
        key = keystore.key_for_path(path)
        if key is None:
            r = DecryptResult(db_path=path)
            r.error = "没有对应的密钥"
            results.append(r)
            continue
        if out_dir:
            rel = os.path.relpath(path, db_dir)
            out_path = os.path.join(out_dir, rel)
        else:
            out_path = None
        results.append(decrypt_file(path, key, out_path, with_wal=with_wal))
    return results
