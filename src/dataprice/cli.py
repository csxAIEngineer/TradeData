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
    backfill = sub.add_parser("backfill", help="从 OKX 拉历史 K 线写入 ClickHouse market.bars")
    backfill.add_argument("--symbol", default="BTC-USDT")
    backfill.add_argument("--months", type=int, default=24, help="大约 30 天算一个月，默认 24")
    backfill.add_argument(
        "--freq",
        action="append",
        default=None,
        help="30m 或 60m（1h 会写成 60m）。可重复，默认两个都拉",
    )
    backtest = sub.add_parser("backtest", help="用 market.bars 的 30m/60m 跑 ICT+SMC 回测")
    backtest.add_argument("--symbol", default="BTC-USDT")
    backtest.add_argument("--months", type=int, default=24, help="回测窗口，大约 30 天算一个月")
    backtest.add_argument("--fee-bps", type=float, default=5.0, help="每边手续费，基点。默认 5")
    backtest.add_argument("--slippage-bps", type=float, default=2.0, help="每边不利滑点，基点。默认 2")
    backtest.add_argument("--baseline", action="store_true", help="用最初的宽松规则，而不是评估候选")
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
    elif args.command == "backfill":
        from datetime import datetime

        from dataprice.feed.history import backfill
        from dataprice.timeutil import UTC

        freqs = tuple(_freq(item) for item in args.freq) if args.freq else ("30m", "60m")
        written = backfill(
            symbol=args.symbol,
            freqs=freqs,
            months=args.months,
            now=datetime.now(UTC),
        )
        logging.info("回补完成: %s", written)
    elif args.command == "backtest":
        from datetime import datetime, timedelta

        from dataprice.strategy.backtest import format_report, run_stored_backtest
        from dataprice.strategy.signals import StrategyParams, evaluated_params
        from dataprice.timeutil import UTC

        end = datetime.now(UTC)
        start = end - timedelta(days=args.months * 30)
        params = StrategyParams() if args.baseline else evaluated_params()
        try:
            result, bars_30, bars_60 = run_stored_backtest(
                symbol=args.symbol,
                start=start,
                end=end,
                params=params,
                fee_bps=args.fee_bps,
                slippage_bps=args.slippage_bps,
            )
        except RuntimeError as exc:
            parser.error(str(exc))
        logging.info("%s 30m %s 根，60m %s 根", args.symbol, bars_30, bars_60)
        for line in format_report(result).splitlines():
            logging.info("%s", line)
    else:
        parser.error(args.command)


def _freq(value: str) -> str:
    key = value.strip().lower()
    if key in {"1h", "60m", "1hr"}:
        return "60m"
    if key == "30m":
        return "30m"
    raise SystemExit(f"只支持 30m 和 60m（1h），收到 {value}")
