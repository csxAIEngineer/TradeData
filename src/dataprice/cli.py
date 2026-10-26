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
    flow = sub.add_parser(
        "flow-backfill",
        help="从 Binance USD-M 拉 30m 主动买卖量写入 ClickHouse market.taker_flow",
    )
    flow.add_argument("--symbol", default="BTC-USDT")
    flow.add_argument("--months", type=int, default=24, help="大约 30 天算一个月，默认 24")
    backtest = sub.add_parser("backtest", help="用 market.bars 的 30m/60m 跑 ICT+SMC 回测")
    backtest.add_argument("--symbol", default="BTC-USDT")
    backtest.add_argument("--months", type=int, default=24, help="回测窗口，大约 30 天算一个月")
    backtest.add_argument("--fee-bps", type=float, default=5.0, help="每边手续费，基点。默认 5")
    backtest.add_argument("--slippage-bps", type=float, default=2.0, help="每边不利滑点，基点。默认 2")
    backtest.add_argument("--baseline", action="store_true", help="用最初的宽松规则，而不是评估候选")
    backtest.add_argument(
        "--flow",
        action="store_true",
        help="在评估候选上再要求扫流动性那根的主动成交同向",
    )
    backtest.add_argument(
        "--regime",
        action="store_true",
        help="在评估候选上再要求大约三天的 4 小时效率比至少 0.25",
    )
    backtest.add_argument(
        "--trend-regime",
        action="store_true",
        help="猴市不做；4 小时偏多只做多，偏空只做空，中性不做",
    )
    backtest.add_argument(
        "--long",
        action="store_true",
        help="只做多，并要求扫卖侧那根的收盘落在高低区间的 70% 以上",
    )
    backtest.add_argument(
        "--buy-only",
        action="store_true",
        help="同一套规则只保留多头。可以和其它画像一起用，--long 本身已经只做多",
    )
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
    elif args.command == "flow-backfill":
        from datetime import datetime

        from dataprice.feed.flow_history import backfill_taker_flow
        from dataprice.timeutil import UTC

        written = backfill_taker_flow(
            symbol=args.symbol,
            months=args.months,
            now=datetime.now(UTC),
        )
        logging.info("主动成交回补完成: %s", written)
    elif args.command == "backtest":
        from dataclasses import replace
        from datetime import datetime, timedelta

        from dataprice.strategy.backtest import format_report, run_stored_backtest
        from dataprice.strategy.signals import (
            StrategyParams,
            evaluated_params,
            flow_params,
            long_params,
            regime_params,
            trend_regime_params,
        )
        from dataprice.timeutil import UTC

        chosen = [
            name
            for name in ("baseline", "flow", "regime", "trend_regime", "long")
            if getattr(args, name)
        ]
        if len(chosen) > 1:
            parser.error("--baseline、--flow、--regime、--trend-regime、--long 只能选一个")
        end = datetime.now(UTC)
        start = end - timedelta(days=args.months * 30)
        if args.baseline:
            params = StrategyParams()
        elif args.flow:
            params = flow_params()
        elif args.regime:
            params = regime_params()
        elif args.trend_regime:
            params = trend_regime_params()
        elif args.long:
            params = long_params()
        else:
            params = evaluated_params()
        if args.buy_only:
            params = replace(params, sides=("buy",))
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
        split = datetime(2026, 3, 1, tzinfo=UTC)
        for line in format_report(result, split=split).splitlines():
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
