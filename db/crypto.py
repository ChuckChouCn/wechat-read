"""SQLCipher 4 页级解密 + WAL 重放。

页布局已由 scratch_probe/test_cipher_layout.py 以真实 SQLCipher 4.12 构建
作为 oracle 验证通过（quick_check=ok，400/400 行完整）：

  - 密钥是 raw 32 字节，**不经 PBKDF2 派生**
  - IV 位于每页 [PAGE_SZ-RESERVE_SZ : +16]
  - 第 1 页正文从偏移 16 开始（前 16 字节是 salt），长度 4000
  - 其余页正文为 [0 : PAGE_SZ-RESERVE_SZ]，长度 4016
  - 解密后 reserve 区（80 字节）补零，保持每页 4096 对齐

AES-256-CBC 后端按可用性自动选择：优先 pycryptodome（开发机），
退到 Windows 自带 bcrypt.dll（目标机零依赖）。
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac as hmac_mod
import struct
import sys

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80  # IV(16) + HMAC(64)
SQLITE_HDR = b"SQLite format 3\x00"

WAL_HDR_SZ = 32
WAL_FRAME_HDR_SZ = 24


# ============================================================
# AES-256-CBC 后端
# ============================================================
class _PyCryptodomeBackend:
    name = "pycryptodome"

    def __init__(self):
        from Crypto.Cipher import AES  # noqa: PLC0415
        self._AES = AES

    def decrypt(self, key: bytes, iv: bytes, data: bytes) -> bytes:
        return self._AES.new(key, self._AES.MODE_CBC, iv=iv).decrypt(data)


class _WindowsCNGBackend:
    """Windows CNG（bcrypt.dll），系统自带，无需第三方库。"""

    name = "windows-cng"

    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("CNG 后端仅在 Windows 可用")
        import ctypes.wintypes as wt

        self._wt = wt
        b = ctypes.WinDLL("bcrypt")
        b.BCryptOpenAlgorithmProvider.argtypes = [
            ctypes.POINTER(wt.HANDLE), ctypes.c_wchar_p, ctypes.c_wchar_p,
            ctypes.c_ulong,
        ]
        b.BCryptSetProperty.argtypes = [
            wt.HANDLE, ctypes.c_wchar_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_ulong,
        ]
        b.BCryptGenerateSymmetricKey.argtypes = [
            wt.HANDLE, ctypes.POINTER(wt.HANDLE), ctypes.c_char_p, ctypes.c_ulong,
            ctypes.c_char_p, ctypes.c_ulong, ctypes.c_ulong,
        ]
        b.BCryptDecrypt.argtypes = [
            wt.HANDLE, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p,
            ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong,
        ]
        self._b = b

    def decrypt(self, key: bytes, iv: bytes, data: bytes) -> bytes:
        wt = self._wt
        b = self._b
        if not data:
            return b""
        h_alg = wt.HANDLE()
        if b.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0) != 0:
            raise RuntimeError("BCryptOpenAlgorithmProvider failed")
        try:
            mode = "ChainingModeCBC\x00".encode("utf-16-le")
            if b.BCryptSetProperty(h_alg, "ChainingMode", mode, len(mode), 0) != 0:
                raise RuntimeError("BCryptSetProperty failed")
            h_key = wt.HANDLE()
            if b.BCryptGenerateSymmetricKey(h_alg, ctypes.byref(h_key), None, 0,
                                            key, len(key), 0) != 0:
                raise RuntimeError("BCryptGenerateSymmetricKey failed")
            try:
                iv_buf = ctypes.create_string_buffer(iv, len(iv))
                out = ctypes.create_string_buffer(len(data))
                n = ctypes.c_ulong(0)
                if b.BCryptDecrypt(h_key, data, len(data), None, iv_buf, len(iv),
                                   out, len(out), ctypes.byref(n), 0) != 0:
                    raise RuntimeError("BCryptDecrypt failed")
                return out.raw[:n.value]
            finally:
                b.BCryptDestroyKey(h_key)
        finally:
            b.BCryptCloseAlgorithmProvider(h_alg, 0)


_BACKENDS = (_PyCryptodomeBackend, _WindowsCNGBackend)
_backend = None


def get_backend():
    """返回可用的 AES 后端（进程内缓存）。"""
    global _backend
    if _backend is None:
        errors = []
        for cls in _BACKENDS:
            try:
                _backend = cls()
                break
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{cls.name}: {exc}")
        if _backend is None:
            raise RuntimeError("没有可用的 AES 后端: " + "; ".join(errors))
    return _backend


# ============================================================
# 页解密
# ============================================================
def decrypt_page(key: bytes, page: bytes, pgno: int) -> bytes:
    """解密单页（pgno 从 1 开始）。输出恒为 PAGE_SZ 字节。"""
    if len(page) < PAGE_SZ:
        page = page + b"\x00" * (PAGE_SZ - len(page))
    iv = page[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    aes = get_backend()
    if pgno == 1:
        body = aes.decrypt(key, iv, page[SALT_SZ:PAGE_SZ - RESERVE_SZ])
        return SQLITE_HDR + body + b"\x00" * RESERVE_SZ
    body = aes.decrypt(key, iv, page[:PAGE_SZ - RESERVE_SZ])
    return body + b"\x00" * RESERVE_SZ


def verify_enc_key(key: bytes, page1: bytes) -> bool:
    """用 HMAC-SHA512 校验密钥是否真的属于该数据库。"""
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_SZ:
        return False
    if len(page1) < PAGE_SZ:
        return False
    salt = page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=KEY_SZ)
    hm = hmac_mod.new(mac_key, page1[SALT_SZ:PAGE_SZ - RESERVE_SZ + 16], hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hmac_mod.compare_digest(hm.digest(), page1[PAGE_SZ - 64:PAGE_SZ])


def decrypt_database_bytes(key: bytes, data: bytes) -> bytes:
    """整库解密，返回标准 SQLite 文件字节。"""
    pages = len(data) // PAGE_SZ
    out = bytearray()
    for i in range(pages):
        out += decrypt_page(key, data[i * PAGE_SZ:(i + 1) * PAGE_SZ], i + 1)
    return bytes(out)


# ============================================================
# WAL 重放
# ============================================================
def read_wal_frames(wal_data: bytes):
    """解析 WAL，返回 [(pgno, 加密页)]，只保留 salt 与头部匹配的帧。

    注意：实测微信把已检查点的陈旧 WAL 留在磁盘上，其帧 salt 与 WAL 头
    **不相等**（微信的 WAL 头记录的是「当前」salt，而残留帧来自更早的
    检查点周期）。按标准 SQLite 语义只接受 salt 匹配的帧，实测在所有
    20 个库上可用帧均为 0 —— 这是**正确**结果：主库已是权威状态。

    若不加此过滤而重放全部帧，会用旧页覆盖新页，导致 quick_check 报
    "invalid page number" / "2nd reference to page"。
    """
    if len(wal_data) <= WAL_HDR_SZ:
        return []
    wal_salt1 = struct.unpack(">I", wal_data[16:20])[0]
    wal_salt2 = struct.unpack(">I", wal_data[20:24])[0]
    frame_sz = WAL_FRAME_HDR_SZ + PAGE_SZ
    frames = []
    off = WAL_HDR_SZ
    while off + frame_sz <= len(wal_data):
        fh = wal_data[off:off + WAL_FRAME_HDR_SZ]
        pgno = struct.unpack(">I", fh[0:4])[0]
        f_salt1 = struct.unpack(">I", fh[8:12])[0]
        f_salt2 = struct.unpack(">I", fh[12:16])[0]
        page = wal_data[off + WAL_FRAME_HDR_SZ:off + frame_sz]
        off += frame_sz
        if pgno == 0 or pgno > 1_000_000:
            continue
        if f_salt1 != wal_salt1 or f_salt2 != wal_salt2:
            continue
        frames.append((pgno, page))
    return frames


def apply_wal(plain_db: bytearray, key: bytes, wal_data: bytes) -> int:
    """把 WAL 帧覆盖到已解密的库上（原地修改），返回应用的帧数。"""
    frames = read_wal_frames(wal_data)
    applied = 0
    for pgno, enc_page in frames:
        dec = decrypt_page(key, enc_page, pgno)
        off = (pgno - 1) * PAGE_SZ
        if off > len(plain_db):
            continue
        plain_db[off:off + PAGE_SZ] = dec
        applied += 1
    return applied
