"""dataprice 命令行。"""

from __future__ import annotations

import argparse
import logging


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="dataprice", description="量化数据平面")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap", help="创建 PostgreSQL / ClickHouse 表和 ArcticDB library")
    sub.add_parser("health", help="检查三个存储是否可连接")
    sub.add_parser("smoke", help="对三个存储做一次读写验收")
    sub.add_parser("feed", help="订阅 OKX books5，把买卖一价写入 ClickHouse")
    args = parser.parse_args(argv)
    if args.command == "bootstrap":
        from dataprice.bootstrap import bootstrap

        bootstrap()
    elif args.command == "health":
        from dataprice.health import health

        health()
    elif args.command == "smoke":
        from dataprice.smoke import smoke

        smoke()
    elif args.command == "feed":
        from dataprice.feed.runner import main as run_feed

        run_feed()
    else:
        parser.error(args.command)
