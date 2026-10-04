"""开发服务器入口：python3 -m incoming_inspection [db_path] [host:port]"""
from __future__ import annotations

import sys
from wsgiref.simple_server import make_server

from .api import create_app


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/incoming.sqlite3"
    addr = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1:8000"
    host, port = addr.split(":")
    app = create_app(db_path)
    httpd = make_server(host, int(port), app)
    print(f"到货检验隔离服务已启动：http://{host}:{port}  数据库：{db_path}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
