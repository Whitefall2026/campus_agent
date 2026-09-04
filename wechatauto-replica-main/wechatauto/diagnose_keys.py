# -*- coding: utf-8 -*-
"""密钥提取失败诊断脚本。

在微信【已登录】状态下运行（务必让微信窗口保持打开）：

    python -m wechatauto.diagnose_keys

或直接：

    python wechatauto/diagnose_keys.py

把输出完整发给维护者。
"""
import json
import os
import subprocess
import sys
import traceback

print("=" * 60)
print("wechatauto-replica key diagnostic")
print("=" * 60)
print("Python:", sys.version.split()[0])
print("Python bits:", 64 if sys.maxsize > 2**32 else 32)
print("OS:", sys.platform)

# 1. library version
try:
    import wechatauto
    from wechatauto import WeChatDB
    from wechatauto.db import _find_account_dirs
    print("lib version:", getattr(wechatauto, "__version__", "?"))
    import wechatauto.db as dbmod
    print("db.py:", dbmod.__file__)
except Exception as e:
    print("import error:", repr(e))
    traceback.print_exc()

# 2. Weixin processes
print("\n--- Weixin processes ---")
weixin_pids = []
try:
    r = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True)
    print("tasklist:\n%s" % (r.stdout.strip() or "(no Weixin.exe)"))
    for line in r.stdout.strip().splitlines():
        parts = line.strip('"').split('","')
        if len(parts) >= 2 and parts[1].isdigit():
            weixin_pids.append(int(parts[1]))
except Exception as e:
    print("tasklist failed:", repr(e))

# 2b. per-PID permission / read test (key root-cause for silent 0-key extraction)
print("\n--- per-PID access test ---")
if not weixin_pids:
    print("(no Weixin.exe running - open WeChat and log in first)")
else:
    import ctypes
    from ctypes import wintypes
    from wechatauto.db import _MBI
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t)]
    k32.VirtualQueryEx.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(_MBI), ctypes.c_size_t]
    for pid in weixin_pids:
        h = k32.OpenProcess(0x0010 | 0x0400, False, pid)  # VM_READ | QUERY_INFORMATION
        if not h:
            err = ctypes.get_last_error()
            print("PID %d: OpenProcess FAILED (error %d - likely needs admin / same-elevation)" % (pid, err))
            continue
        ok = 0
        first_region = None
        addr = ctypes.c_void_p(0)
        for _ in range(4000):
            mbi = _MBI()
            n = k32.VirtualQueryEx(h, addr, ctypes.byref(mbi), ctypes.sizeof(_MBI))
            if n == 0:
                break
            if (mbi.State == 0x1000 and (mbi.Protect & 0xFF) & 0xE6
                    and not (mbi.Protect & 0x100) and 0 < mbi.RegionSize < 0x10000000):
                buf = ctypes.create_string_buffer(8)
                br = ctypes.c_size_t(0)
                if k32.ReadProcessMemory(h, ctypes.c_void_p(mbi.BaseAddress or 0), buf, 8, ctypes.byref(br)) and br.value == 8:
                    ok += 1
                    if first_region is None:
                        first_region = mbi.BaseAddress or 0
                    break
            addr = ctypes.c_void_p((mbi.BaseAddress or 0) + mbi.RegionSize)
        ctypes.windll.kernel32.CloseHandle(h)
        print("PID %d: OpenProcess OK, first readable region: %s (readable-region check %s)"
              % (pid, "0x%x" % first_region if first_region else "NONE", "OK" if ok else "FAILED"))

# 3. data dir detection
print("\n--- data dir ---")
try:
    from wechatauto.db import auto_detect_db_dir
    d = auto_detect_db_dir()
    print("auto_detect_db_dir:", d)
    if d and os.path.isdir(d):
        for x in os.listdir(d):
            if os.path.isdir(os.path.join(d, x, "db_storage")):
                print("  account dir:", x)
except Exception as e:
    print("dir detect error:", repr(e))

# 4. WeChatDB init (uses cached keys)
print("\n--- WeChatDB init ---")
db = None
try:
    db = WeChatDB()
    print("workdir:", db.workdir)
    print("keys_file:", db.keys_file, "exists:", os.path.exists(db.keys_file))
    print("account:", db.account)
    if os.path.exists(db.keys_file):
        with open(db.keys_file, encoding="utf-8") as f:
            keys = json.load(f)
        print("keys cached:", len(keys))
        for k in sorted(keys):
            print("   ", k)
    else:
        print("keys cached: (file not found)")
    print("db_files:", len(db._db_files))
    print("keys loaded:", len(db._keys))
    missing = [rel for rel, _, _ in db._db_files if rel not in db._keys]
    print("missing:", missing)
    print("unkeyed:", db.unkeyed)
    # which account dirs exist vs which one was picked
    print("accounts on disk:", sorted(
        os.path.basename(x) for x in _find_account_dirs(db.db_dir)))
    print("picked account:  ", db.account)
except Exception as e:
    print("init error:", repr(e))
    traceback.print_exc()

# 5. fresh key extraction from memory
print("\n--- extract_keys from Weixin.exe memory ---")
try:
    if db is None:
        db = WeChatDB()
    pids = db._find_weixin_pids()
    print("Weixin PIDs:", pids)
    if not pids:
        print("no Weixin.exe running - open WeChat and log in first")
    else:
        keys = db.extract_keys()
        print("extracted:", len(keys))
        for k in sorted(keys):
            print("   ", k)
        missing2 = [rel for rel, _, _ in db._db_files if rel not in keys]
        print("still missing:", missing2)
except Exception as e:
    print("extract error:", repr(e))
    traceback.print_exc()

# 6. verify cached keys actually work
print("\n--- verify cached keys ---")
try:
    if db is None:
        db = WeChatDB()
    works = 0
    for rel, _, _ in db._db_files:
        if db._key_works(rel):
            works += 1
    print("keys that verify: %d / %d" % (works, len(db._db_files)))
except Exception as e:
    print("verify error:", repr(e))

print("=" * 60)
print("done. send the full output to the maintainer.")
print("=" * 60)