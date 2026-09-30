#!/usr/bin/env python3
"""微信 4.1+ 数据库密钥提取 — 最小验证版

目标架构中的 key-provider 层。只做一件事：从运行中的 Weixin.exe 内存里
取出 SQLCipher4 数据库密钥，并用 HMAC 校验证明取到的是真钥。

主路径（微信 4.1+）：只读 runtime Config.Cipher 扫描
    1. 在进程内存里定位字面量 "com.Tencent.WCDB.Config.Cipher"
    2. 顺着 (ptr, len) 引用找到承载它的字符串对象
    3. 从对象里取出 Config 指针，再取它 +0x88 处的数据对象
    4. 读出 blob（微信做了 XOR 混淆，不能直接搜 x'...'）
    5. XOR 解混淆后提取 x'<64hex key><32hex salt>' 候选
    6. PBKDF2-HMAC-SHA512(256000) 派生每库专属密钥

校验（唯一判定标准）：用派生出的密钥对数据库第一页做 HMAC-SHA512，
与库里自存的 HMAC 对上才算数。

算法移植自 TANGandXUE/wcdb-key-tool (MIT)。
本文件不含该项目的 CLI/解密/导出逻辑，只保留取密钥最小路径。

用法（必须在 Windows 上以能读取微信进程内存的权限运行）：
    python extract_keys.py --db-dir <db_storage 路径> [--out keys.json]
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac as hmac_mod
import json
import os
import re
import struct
import subprocess
import sys
import time

# ============================================================
# 常量
# ============================================================
PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
RESERVE_SZ = 80  # IV(16) + HMAC(64)

CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"

# 微信对 Config blob 做的 XOR 混淆掩码
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)

MAX_USER_ADDRESS = 0x0000_8000_0000_0000
CONFIG_BLOB_MAX = 1024
CONFIG_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
LEGACY_HEX_RE = re.compile(rb"x'([0-9a-fA-F]{64,192})'")

MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ============================================================
# 数据库收集
# ============================================================
def collect_db_files(db_dir: str):
    """返回 (db_files, salt_to_dbs)。

    db_files: [(rel, path, size, salt_hex, page1)]
    """
    db_files = []
    salt_to_dbs: dict[str, list[str]] = {}
    for root, _dirs, files in os.walk(db_dir):
        for name in files:
            if not name.endswith(".db"):
                continue
            path = os.path.join(root, name)
            try:
                size = os.path.getsize(path)
                if size < PAGE_SZ:
                    continue
                with open(path, "rb") as fh:
                    page1 = fh.read(PAGE_SZ)
            except OSError:
                continue
            if len(page1) < PAGE_SZ:
                continue
            rel = os.path.relpath(path, db_dir)
            salt = page1[:SALT_SZ].hex()
            db_files.append((rel, path, size, salt, page1))
            salt_to_dbs.setdefault(salt, []).append(rel)
    return db_files, salt_to_dbs


# ============================================================
# HMAC 校验 —— 判定取到的密钥是否真的正确
# ============================================================
def verify_enc_key(enc_key: bytes, page1: bytes) -> bool:
    if not isinstance(enc_key, (bytes, bytearray)) or len(enc_key) != KEY_SZ:
        return False
    if len(page1) < PAGE_SZ:
        return False
    salt = page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    hm = hmac_mod.new(mac_key, page1[SALT_SZ:PAGE_SZ - RESERVE_SZ + 16], hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hmac_mod.compare_digest(hm.digest(), page1[PAGE_SZ - 64:PAGE_SZ])


# ============================================================
# 进程与内存读取
# ============================================================
class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wt.DWORD), ("_pad1", wt.DWORD),
        ("RegionSize", ctypes.c_uint64), ("State", wt.DWORD),
        ("Protect", wt.DWORD), ("Type", wt.DWORD), ("_pad2", wt.DWORD),
    ]


def get_weixin_pids():
    r = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    pids = []
    for line in r.stdout.strip().split("\n"):
        if not line.strip():
            continue
        p = line.strip('"').split('","')
        if len(p) >= 5:
            try:
                pid = int(p[1])
                mem = int(p[4].replace(",", "").replace(" K", "").strip() or "0")
            except ValueError:
                continue
            pids.append((pid, mem))
    pids.sort(key=lambda x: x[1], reverse=True)
    return pids


def enum_regions(kernel32, handle):
    regs = []
    addr = 0
    mbi = MBI()
    while addr < 0x7FFFFFFFFFFF:
        if kernel32.VirtualQueryEx(handle, ctypes.c_uint64(addr),
                                   ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
            break
        if (mbi.State == MEM_COMMIT and mbi.Protect in READABLE
                and 0 < mbi.RegionSize < 500 * 1024 * 1024):
            regs.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regs


def make_reader(kernel32, handle):
    def read_mem(addr, size):
        if size <= 0:
            return None
        buf = ctypes.create_string_buffer(size)
        n = ctypes.c_size_t(0)
        if kernel32.ReadProcessMemory(handle, ctypes.c_uint64(addr), buf, size,
                                      ctypes.byref(n)):
            return buf.raw[:n.value]
        return None
    return read_mem


# ============================================================
# Config.Cipher 扫描
# ============================================================
def xor_repeat(data: bytes, mask: bytes) -> bytes:
    return bytes(v ^ mask[i % len(mask)] for i, v in enumerate(data))


def u64_from(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 8 > len(data):
        return 0
    return struct.unpack_from("<Q", data, offset)[0]


def probable_32_byte_key(data: bytes) -> bool:
    return (len(data) == KEY_SZ and len(set(data)) >= 15
            and data not in (b"\x00" * KEY_SZ, b"\xff" * KEY_SZ))


def iter_region_chunks(regions, read_region, chunk_size=2 * 1024 * 1024, overlap=0):
    """按块读取可读内存区域，可选块间重叠（跨块匹配用）。"""
    for base, size in regions:
        offset = 0
        tail = b""
        tail_base = base
        while offset < size:
            cur = min(chunk_size, size - offset)
            chunk = read_region(base + offset, cur) or b""
            data_base = tail_base if tail else base + offset
            data = tail + chunk
            if data:
                yield data_base, data
                if overlap:
                    tail = data[-overlap:]
                    tail_base = data_base + max(0, len(data) - len(tail))
                else:
                    tail = b""
                    tail_base = base + offset + cur
            else:
                tail = b""
                tail_base = base + offset + cur
            offset += cur


def find_bytes_in_regions(regions, read_region, needle: bytes):
    addresses = set()
    overlap = max(0, len(needle) - 1)
    for data_base, haystack in iter_region_chunks(regions, read_region, overlap=overlap):
        pos = haystack.find(needle)
        while pos >= 0:
            addresses.add(data_base + pos)
            pos = haystack.find(needle, pos + 1)
    return addresses


def config_key_candidates(blob: bytes):
    """XOR 解混淆后提取 (key_hex, embedded_salt_hex) 候选。"""
    if not blob or len(blob) > CONFIG_BLOB_MAX:
        return []
    decoded = xor_repeat(blob, CONFIG_XOR_MASK)
    out, seen = [], set()
    for match in CONFIG_LITERAL_RE.finditer(decoded):
        run = match.group(1).decode("ascii").lower()
        starts = [0]
        if len(run) > 96:
            starts.extend(range(0, len(run) - 63, 32))
            starts.append(len(run) - 64)
        for start in dict.fromkeys(starts):
            if start < 0 or start + 64 > len(run):
                continue
            key_hex = run[start:start + 64]
            try:
                key = bytes.fromhex(key_hex)
            except ValueError:
                continue
            if not probable_32_byte_key(key):
                continue
            embedded = run[start + 64:start + 96] if start + 96 <= len(run) else None
            item = (key_hex, embedded)
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def scan_config_cipher(pid, regions, read_region, read_mem, db_files,
                       salt_to_dbs, key_map, remaining_salts):
    stats = {
        "regions": len(regions),
        "needle_occurrences": 0,
        "string_object_refs": 0,
        "node_candidates": 0,
        "config_ptr_candidates": 0,
        "blob_read": 0,
        "blob99_count": 0,
        "candidate_count": 0,
        "verified_candidates": 0,
        "matched_salts": 0,
    }

    needle_addresses = find_bytes_in_regions(regions, read_region, CONFIG_CIPHER_NAME)
    stats["needle_occurrences"] = len(needle_addresses)
    if not needle_addresses:
        return stats

    pair_patterns = [
        struct.pack("<Q", addr) + struct.pack("<Q", len(CONFIG_CIPHER_NAME))
        for addr in needle_addresses
    ]
    seen_config_ptrs, seen_candidates = set(), set()

    for base, data in iter_region_chunks(regions, read_region, overlap=0x80):
        if not remaining_salts:
            break
        for pattern in pair_patterns:
            pos = data.find(pattern)
            while pos >= 0:
                stats["string_object_refs"] += 1
                node = read_mem(base + pos - 0x10, 0x50)
                if not node or len(node) < 0x40:
                    pos = data.find(pattern, pos + 1)
                    continue
                if (u64_from(node, 0x10) not in needle_addresses
                        or u64_from(node, 0x18) != len(CONFIG_CIPHER_NAME)):
                    pos = data.find(pattern, pos + 1)
                    continue
                config_ptr = u64_from(node, 0x28)
                if not (0x10000 <= config_ptr < MAX_USER_ADDRESS):
                    pos = data.find(pattern, pos + 1)
                    continue
                stats["node_candidates"] += 1
                seen_config_ptrs.add(config_ptr)

                obj = read_mem(config_ptr + 0x88, 0x28)
                if not obj or len(obj) < 0x18:
                    pos = data.find(pattern, pos + 1)
                    continue
                data_ptr, data_len = u64_from(obj, 0x8), u64_from(obj, 0x10)
                if not (0 < data_len <= CONFIG_BLOB_MAX
                        and 0x10000 <= data_ptr < MAX_USER_ADDRESS):
                    pos = data.find(pattern, pos + 1)
                    continue
                blob = read_mem(data_ptr, int(data_len))
                if not blob or len(blob) != data_len:
                    pos = data.find(pattern, pos + 1)
                    continue
                stats["blob_read"] += 1
                if data_len == 99:
                    stats["blob99_count"] += 1

                for key_hex, embedded in config_key_candidates(blob):
                    cand = (key_hex, embedded)
                    if cand in seen_candidates:
                        continue
                    seen_candidates.add(cand)
                    stats["candidate_count"] += 1
                    matched = verify_candidate(
                        key_hex, embedded, db_files, key_map, remaining_salts)
                    if matched:
                        stats["verified_candidates"] += 1
                        stats["matched_salts"] += matched
                pos = data.find(pattern, pos + 1)

    stats["config_ptr_candidates"] = len(seen_config_ptrs)
    return stats


def verify_candidate(key_hex, embedded_salt, db_files, key_map, remaining_salts):
    if not remaining_salts:
        return 0
    try:
        key = bytes.fromhex(key_hex)
    except ValueError:
        return 0
    if not probable_32_byte_key(key):
        return 0
    targets = ([embedded_salt] if embedded_salt in remaining_salts
               else list(remaining_salts))
    matched = 0
    for salt_hex in targets:
        if salt_hex not in remaining_salts:
            continue
        for rel, _path, _sz, s, page1 in db_files:
            if s == salt_hex and verify_enc_key(key, page1):
                key_map[salt_hex] = key_hex
                remaining_salts.discard(salt_hex)
                matched += 1
                log(f"  [FOUND] salt={salt_hex} key={key_hex[:16]}... ({rel})")
                break
    return matched


# ============================================================
# 密钥缓存
# ============================================================
def _load_cached(path: str) -> dict[str, str]:
    """读已缓存的密钥 {salt: enc_key}。文件不存在或损坏时返回空。"""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict):
            salt, enc = v.get("salt") or k, v.get("enc_key")
        else:
            salt, enc = k, v
        if (isinstance(enc, str) and len(enc) == 64
                and isinstance(salt, str)):
            try:
                bytes.fromhex(enc)
            except ValueError:
                continue
            out[salt.lower()] = enc.lower()
    return out


# ============================================================
# 主流程
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-dir", required=True, help="db_storage 目录")
    ap.add_argument("--out", default=None,
                    help="密钥输出路径（默认存到用户级缓存 ~/.weixin-read/keys.json，"
                         "之后自动复用）")
    ap.add_argument("--max-pids", type=int, default=0, help="最多扫描几个进程（0=全部）")
    ap.add_argument("--force", action="store_true",
                    help="即使已有可用密钥也重新提取")
    args = ap.parse_args()

    # 默认存到用户级缓存，实现「首次提取、之后复用」
    if args.out is None:
        args.out = os.path.join(os.path.expanduser("~"), ".weixin-read", "keys.json")

    log("=" * 62)
    log(" 微信 4.1+ 数据库密钥提取 — 最小验证")
    log("=" * 62)

    if sys.platform != "win32":
        log(f"[FAIL] 需要在 Windows 上运行（当前 {sys.platform}）")
        return 2

    # --- 阶段 1: 收集数据库 ---
    log(f"\n[阶段 1] 收集数据库: {args.db_dir}")
    if not os.path.isdir(args.db_dir):
        log(f"[FAIL] 目录不存在: {args.db_dir}")
        return 2
    db_files, salt_to_dbs = collect_db_files(args.db_dir)
    log(f"  找到 {len(db_files)} 个 .db, {len(salt_to_dbs)} 个不同 salt")
    if not db_files:
        log("[FAIL] 未找到可解密的数据库")
        return 2

    # --- 复用检查: 已有可用密钥就不重复提取 ---
    if not args.force:
        cached = _load_cached(args.out)
        if cached:
            current = set(salt_to_dbs.keys())
            have = set(cached.keys())
            if current <= have:
                log(f"\n[复用] 已存在覆盖全部 {len(current)} 个数据库的密钥：{args.out}")
                log("       无需重新提取。若确实要重提取，加 --force。")
                return 0
            missing = current - have
            log(f"\n[补充] 已存密钥缺 {len(missing)} 个库，继续提取补齐…")

    # --- 阶段 2: 枚举微信进程 ---
    log("\n[阶段 2] 枚举 Weixin.exe 进程")
    pids = get_weixin_pids()
    if not pids:
        log("[FAIL] Weixin.exe 未运行（请确认微信已启动并登录）")
        return 2
    for pid, mem in pids:
        log(f"  PID={pid} ({mem // 1024}MB)")
    if args.max_pids:
        pids = pids[:args.max_pids]

    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010

    key_map: dict[str, str] = {}
    remaining = set(salt_to_dbs.keys())
    all_stats = []
    t0 = time.time()
    perm_denied = 0

    # --- 阶段 3: Config.Cipher 扫描 ---
    log("\n[阶段 3] Config.Cipher 只读扫描（4.1+ 主路径）")
    for pid, mem_kb in pids:
        if not remaining:
            break
        h = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
        if not h:
            log(f"  [WARN] PID={pid} OpenProcess 失败（err={ctypes.GetLastError()}）"
                "— 需要管理员权限")
            perm_denied += 1
            continue
        try:
            regions = enum_regions(kernel32, h)
            read_mem = make_reader(kernel32, h)
            total_mb = sum(s for _, s in regions) / 1024 / 1024
            log(f"  --- PID={pid}: {len(regions)} 个区域, {total_mb:.0f}MB")
            st = scan_config_cipher(
                pid, regions, lambda a, s: read_mem(a, s), read_mem,
                db_files, salt_to_dbs, key_map, remaining,
            )
            all_stats.append((pid, st))
            log(f"      needle(Config.Cipher) 命中: {st['needle_occurrences']}")
            log(f"      字符串对象引用: {st['string_object_refs']}, "
                f"节点候选: {st['node_candidates']}, "
                f"config 指针: {st['config_ptr_candidates']}")
            log(f"      blob 读取: {st['blob_read']} (len==99: {st['blob99_count']}), "
                f"key 候选: {st['candidate_count']}")
            log(f"      校验通过候选: {st['verified_candidates']}, "
                f"匹配 salt: {st['matched_salts']}")
        finally:
            kernel32.CloseHandle(h)

    elapsed = time.time() - t0

    # --- 结果 ---
    log("\n" + "=" * 62)
    log(f"结果: {len(key_map)}/{len(salt_to_dbs)} 个 salt 取到密钥 "
        f"（耗时 {elapsed:.1f}s）")
    log("=" * 62)

    if key_map:
        for salt_hex, key_hex in sorted(key_map.items()):
            names = ", ".join(salt_to_dbs[salt_hex])
            log(f"  {salt_hex}")
            log(f"      key  = {key_hex}")
            log(f"      dbs  = {names}")
        missing = sorted(remaining)
        if missing:
            log(f"\n  未解析 {len(missing)} 个 salt:")
            for s in missing:
                log(f"    {s}: {', '.join(salt_to_dbs[s])}")
        if args.out and key_map:
            # 合并旧密钥：补齐场景下不能丢掉原有的
            merged = _load_cached(args.out)
            merged.update(key_map)
            payload = {
                salt_hex: {"enc_key": key_hex, "salt": salt_hex,
                           "dbs": salt_to_dbs.get(salt_hex, [])}
                for salt_hex, key_hex in merged.items()
            }
            payload["_meta"] = {
                "_schema": 1,
                "extracted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "db_dir": args.db_dir,
                "count": len(merged),
                "total_salts": len(salt_to_dbs),
            }
            d = os.path.dirname(os.path.abspath(args.out))
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = args.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, args.out)          # 原子替换，避免写一半损坏
            try:
                os.chmod(args.out, 0o600)      # 含密钥，仅本人可读
            except OSError:
                pass
            log(f"\n  已写入: {args.out}")
            log(f"  （之后运行会自动复用这份密钥，无需重复提取）")

    # --- 诊断结论 ---
    log("\n[诊断]")
    if key_map:
        log("  主路径可用。")
        return 0
    if perm_denied and not all_stats:
        log("  所有进程 OpenProcess 都失败 → 权限不足。")
        log("  需要以管理员身份运行。")
        return 3
    if all_stats:
        any_needle = any(st["needle_occurrences"] for _, st in all_stats)
        if not any_needle:
            log("  内存里找不到 'com.Tencent.WCDB.Config.Cipher' 字面量。")
            log("  → 该微信版本可能不走 Config.Cipher，或微信未登录。")
        elif not any(st["node_candidates"] for _, st in all_stats):
            log("  找到字面量但没能定位字符串对象 → 结构布局与预期不符。")
        elif not any(st["blob_read"] for _, st in all_stats):
            log("  定位到节点但读不到 blob → 指针链偏移与预期不符。")
        elif not any(st["candidate_count"] for _, st in all_stats):
            log("  读到 blob 但解不出 key 候选 → XOR 掩码或 blob 布局不符。")
        else:
            log("  有 key 候选但 HMAC 校验全部不通过。")
            log("  → 候选不是真钥，或数据库与当前登录账号不匹配。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
