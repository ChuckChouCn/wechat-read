"""db 层：加密微信数据库 → 可读 SQLite。

对外主要入口：

    from db import KeyStore, decrypt_file, decrypt_dir

    ks = KeyStore.load("keys.json")
    result = decrypt_file("/path/to/session.db", ks.key_for_path(path))
    if result.ok:
        ...  # result.data 是标准 SQLite 文件字节

布局依据见 crypto.py 顶部注释（已用真实 SQLCipher 4.12 oracle 验证）。
"""
from .crypto import (
    PAGE_SZ,
    RESERVE_SZ,
    SALT_SZ,
    apply_wal,
    decrypt_database_bytes,
    decrypt_page,
    get_backend,
    read_wal_frames,
    verify_enc_key,
)
from . import detect
from .decrypt import DecryptResult, decrypt_dir, decrypt_file
from .keys import KeyStore, find_db_files
from .keystore import (
    KEYS_FILE,
    STATE_DIR,
    STATUS_EMPTY,
    STATUS_INVALID,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_STALE,
    KeyCacheStatus,
    check,
    load_keys,
    read_meta,
    resolve_keys_path,
    save_keys,
    scan_db_salts,
    verify_deep,
)

__all__ = [
    "detect",
    "PAGE_SZ", "SALT_SZ", "RESERVE_SZ",
    "KeyStore", "find_db_files",
    "DecryptResult", "decrypt_file", "decrypt_dir",
    "decrypt_page", "decrypt_database_bytes", "verify_enc_key",
    "read_wal_frames", "apply_wal", "get_backend",
    # 密钥缓存
    "KEYS_FILE", "STATE_DIR", "KeyCacheStatus",
    "check", "load_keys", "save_keys", "read_meta", "scan_db_salts",
    "verify_deep", "resolve_keys_path",
    "STATUS_OK", "STATUS_MISSING", "STATUS_STALE", "STATUS_PARTIAL",
    "STATUS_EMPTY", "STATUS_INVALID",
]
