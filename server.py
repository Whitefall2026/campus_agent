"""校园管家入口。

启动：python server.py [端口]
"""
from __future__ import annotations

import os
import sys
from http.server import ThreadingHTTPServer

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.web.handlers import Handler
from app.wechat.bridge import BRIDGE as WX_BRIDGE


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"校园管家 demo 已启动: http://127.0.0.1:{port}")
    print("按 Ctrl+C 停止服务")
    if WX_BRIDGE.config().get("auto_start"):
        WX_BRIDGE.start()
        print("微信监听：已开启，等待微信登录后自动连接（状态可查看 /api/wechat/status）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        WX_BRIDGE.stop()


if __name__ == "__main__":
    main()
