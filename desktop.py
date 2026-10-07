"""RUC Agent Windows / macOS 桌面入口。

启动本地 HTTP 服务、打开默认浏览器，并通过系统托盘管理应用生命周期。
安装版使用此入口；开发者仍可运行 ``python server.py``。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer

from app.paths import DATA_DIR, RESOURCE_ROOT
from app.version import __version__

HOST = "127.0.0.1"
DEFAULT_PORT = 8000
RUNTIME_FILE = os.path.join(DATA_DIR, "runtime.json")
MUTEX_NAME = "Local\\RUC-Agent-Desktop-9BFA4511"
TRAY_ICON_FILE = (
    os.path.join(RESOURCE_ROOT, "assets", "brand", "ruc-logo.png")
    if sys.platform == "darwin"
    else os.path.join(RESOURCE_ROOT, "packaging", "app_icon.ico")
)

_mutex_handle = None
_instance_lock = None
_log_stream = None


def _configure_output() -> None:
    """窗口版没有控制台，把诊断信息写到用户数据目录。"""
    global _log_stream
    os.makedirs(DATA_DIR, exist_ok=True)
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_dir = os.path.join(DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    _log_stream = open(
        os.path.join(log_dir, "desktop.log"), "a", encoding="utf-8", buffering=1
    )
    if sys.stdout is None:
        sys.stdout = _log_stream
    if sys.stderr is None:
        sys.stderr = _log_stream


def _read_running_url() -> str:
    try:
        with open(RUNTIME_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
        port = int(obj.get("port") or DEFAULT_PORT)
        if 1 <= port <= 65535:
            return f"http://{HOST}:{port}/"
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return f"http://{HOST}:{DEFAULT_PORT}/"


def _acquire_single_instance(no_browser: bool = False) -> bool:
    """同一用户只运行一个实例；重复启动时直接打开已有页面。"""
    global _mutex_handle, _instance_lock
    if sys.platform == "darwin" and os.name != "nt":
        import fcntl

        os.makedirs(DATA_DIR, exist_ok=True)
        lock = open(os.path.join(DATA_DIR, "desktop.lock"), "a+")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            if not no_browser:
                webbrowser.open(_read_running_url())
            return False
        except OSError:
            lock.close()
            raise
        _instance_lock = lock
        return True
    if os.name != "nt":
        return True
    # use_last_error=True 让 ctypes 在调用后立即捕获错误码，set/get_last_error
    # 操作的是同一份副本，才能可靠读到 ERROR_ALREADY_EXISTS。
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return True
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        if not no_browser:
            webbrowser.open(_read_running_url())
        return False
    _mutex_handle = handle
    return True


def _release_single_instance() -> None:
    global _mutex_handle, _instance_lock
    if _instance_lock is not None:
        # Closing releases flock. Keep the file: unlinking it would allow
        # another process to lock a new inode while an existing one is held.
        _instance_lock.close()
        _instance_lock = None
    if _mutex_handle and os.name == "nt":
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(_mutex_handle))
        _mutex_handle = None


def _write_runtime(port: int) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = RUNTIME_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {"pid": os.getpid(), "port": port, "started_at": time.time()}, f
        )
    os.replace(tmp, RUNTIME_FILE)


def _remove_runtime() -> None:
    try:
        with open(RUNTIME_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if int(obj.get("pid") or 0) == os.getpid():
            os.remove(RUNTIME_FILE)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass


def _make_server(preferred_port: int):
    # 延迟导入可确保窗口版的 stdout/stderr 已准备好。
    from app.web.handlers import Handler

    ThreadingHTTPServer.daemon_threads = True
    try:
        return ThreadingHTTPServer((HOST, preferred_port), Handler)
    except OSError:
        # 8000 被其他软件占用时使用系统分配端口，功能不因此失效。
        return ThreadingHTTPServer((HOST, 0), Handler)


def _start_wechat() -> None:
    if os.name != "nt":
        return
    from app.wechat.bridge import BRIDGE

    if BRIDGE.config().get("auto_start"):
        BRIDGE.start()


def _stop_wechat() -> None:
    if os.name != "nt":
        return
    try:
        from app.wechat.bridge import BRIDGE

        BRIDGE.stop()
    except Exception as exc:
        print(f"停止微信监听失败：{exc}", file=sys.stderr)


def _smoke_test(port: int) -> int:
    """验证冻结程序的静态资源、核心 HTTP、Excel 读取及平台依赖。"""
    try:
        from app.core import courses, excel_todos, xlsx  # noqa: F401
    except Exception as exc:
        print(f"冻结版 Excel 读取模块导入失败：{exc}", file=sys.stderr)
        return 5
    if os.name == "nt":
        if not _check_wechat_dependencies():
            return 4

    server = _make_server(port)
    actual_port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{HOST}:{actual_port}"
        with urllib.request.urlopen(base + "/", timeout=10) as response:
            page = response.read().decode("utf-8")
            if response.status != 200 or "RUC Agent" not in page:
                return 2
        for asset in ("/app.js", "/style.css"):
            with urllib.request.urlopen(base + asset, timeout=10) as response:
                if response.status != 200 or not response.read():
                    return 2
        with urllib.request.urlopen(base + "/api/state", timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if response.status != 200 or not payload.get("ok"):
                return 3
        return 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _check_wechat_dependencies() -> bool:
    try:
        # bridge 正常运行时使用的是这两个惰性导入。仅导入不会连接或读取微信，
        # 但能发现 PyInstaller 漏收包、DLL 或二进制扩展的问题。
        from wechatauto.db import Listener, WeChatDB  # noqa: F401
        import uiautomation

        arch = "X64" if sys.maxsize > 0xFFFFFFFF else "X86"
        uia_dll = os.path.join(
            os.path.dirname(uiautomation.__file__),
            "bin",
            f"UIAutomationClient_VC140_{arch}.dll",
        )
        if not os.path.isfile(uia_dll):
            raise FileNotFoundError(uia_dll)
        ctypes.CDLL(uia_dll)
    except Exception as exc:
        print(f"冻结版微信依赖导入失败：{exc}", file=sys.stderr)
        return False
    return True


def _open_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.run(["open", DATA_DIR], check=True)
    else:
        os.startfile(DATA_DIR)


def _run_tray(url: str) -> None:
    """在系统托盘中提供后台服务的打开与退出控制。"""
    import pystray
    from PIL import Image

    def open_app(icon=None, item=None):
        webbrowser.open(url)

    def open_data(icon=None, item=None):
        _open_data_dir()

    def exit_app(icon, item=None):
        icon.stop()

    with Image.open(TRAY_ICON_FILE) as source:
        tray_image = source.convert("RGBA")
    menu = pystray.Menu(
        pystray.MenuItem("打开应用", open_app, default=True),
        pystray.MenuItem("打开数据目录", open_data),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出 RUC Agent", exit_app),
    )
    tray = pystray.Icon(
        "ruc-agent",
        tray_image,
        f"RUC Agent 校园管家 {__version__}（双击打开）",
        menu,
    )
    tray.run()


def _run_gui(port: int, no_browser: bool) -> int:
    server = _make_server(port)
    actual_port = int(server.server_address[1])
    url = f"http://{HOST}:{actual_port}/"
    _write_runtime(actual_port)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        _start_wechat()
        if not no_browser:
            threading.Timer(0.45, lambda: webbrowser.open(url)).start()
        if os.name == "nt" or sys.platform == "darwin":
            # 托盘菜单在后台维持服务生命周期，避免原来的控制对话框在
            # 用户点击“打开应用”后立即再次出现。
            # pystray's Cocoa event loop must run on this main thread.
            _run_tray(url)
        else:
            # 方便源码在非 Windows 环境调试；安装产物不会走到这里。
            print(f"RUC Agent 正在运行：{url}")
            while True:
                time.sleep(1)
        return 0
    finally:
        _stop_wechat()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        _remove_runtime()
        _release_single_instance()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="RUC Agent desktop launcher")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    _configure_output()
    if args.smoke_test:
        return _smoke_test(0)
    if not _acquire_single_instance(args.no_browser):
        return 0
    try:
        return _run_gui(args.port, args.no_browser)
    except Exception as exc:
        print(f"RUC Agent 启动失败：{exc}", file=sys.stderr)
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                f"启动失败：{exc}\n\n日志目录：{DATA_DIR}",
                "RUC Agent",
                0x10,
            )
        except Exception:
            pass
        return 1
    finally:
        _release_single_instance()


if __name__ == "__main__":
    raise SystemExit(main())
