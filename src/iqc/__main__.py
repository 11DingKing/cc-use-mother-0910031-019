"""``python -m iqc``：启动到货检验隔离 HTTP 服务。"""
from __future__ import annotations

import argparse

from .api import run


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="iqc", description="到货检验隔离后端服务")
    parser.add_argument("--db", default="iqc.db", help="SQLite 数据库文件路径（默认 iqc.db）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    args = parser.parse_args(argv)
    run(host=args.host, port=args.port, db_path=args.db)


if __name__ == "__main__":
    main()
