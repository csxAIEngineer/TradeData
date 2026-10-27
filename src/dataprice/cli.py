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
    spot = sub.add_parser(
        "binance-backfill",
        help="从 data.binance.vision 拉现货 BTCUSDT 5m，并合成 UTC 对齐的 10m",
    )
    spot.add_argument("--symbol", default="BTCUSDT")
    spot.add_argument("--months", type=int, default=36, help="大约 30 天算一个月，默认 36")
    spot.add_argument("--freq", default="5m", help="只支持 5m。10m 由完整的两根 5m 合成后一起写入")
    backtest = sub.add_parser("backtest", help="用 market.bars 跑回测。ICT 用 30m/60m；--tpo-vwap 还可选 5m/10m")
    backtest.add_argument("--symbol", default="BTC-USDT")
    backtest.add_argument("--months", type=int, default=24, help="回测窗口，大约 30 天算一个月")
    backtest.add_argument(
        "--freq",
        default="30m",
        help="只和 --tpo-vwap 一起用：30m、5m 或 10m。默认 30m",
    )
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
        "--grid",
        action="store_true",
        help="只在 4 小时效率比低于 0.25 时做震荡网格，不跑趋势候选",
    )
    backtest.add_argument(
        "--long",
        action="store_true",
        help="只做多，并要求扫卖侧那根的收盘落在高低区间的 70%% 以上",
    )
    backtest.add_argument(
        "--buy-only",
        action="store_true",
        help="同一套规则只保留多头。可以和其它画像一起用，--long 本身已经只做多",
    )
    backtest.add_argument(
        "--short-regime",
        action="store_true",
        help="只对空头要求大约三天的 4 小时效率比至少 0.25，多头仍是评估候选",
    )
    backtest.add_argument(
        "--debate-daily",
        action="store_true",
        help="按已收盘日线在 long、short、grid、flat 里选一个，当天不再改",
    )
    backtest.add_argument(
        "--anti-sweep",
        action="store_true",
        help="扫完等高/等低后顺着扫的方向做。进场仍是评估候选，只对调方向，不改 evaluated_params()",
    )
    backtest.add_argument(
        "--tpo-vwap",
        action="store_true",
        help="上一完整 UTC 日的 TPO 价值区加当日 VWAP 收回。参数锁在 tpo_vwap.evaluated_params()，不搜索",
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
    elif args.command == "binance-backfill":
        from datetime import datetime

        from dataprice.feed.binance_history import backfill_spot_klines
        from dataprice.timeutil import UTC

        try:
            spot_result = backfill_spot_klines(
                symbol=args.symbol,
                months=args.months,
                freq=args.freq,
                now=datetime.now(UTC),
            )
        except (RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        logging.info(
            "%s 5m %s 根，10m %s 根，%s → %s",
            spot_result.symbol,
            spot_result.bars_5m,
            spot_result.bars_10m,
            spot_result.first_open,
            spot_result.last_open,
        )
        if spot_result.skipped:
            logging.info("跳过 %s 个缺失文件: %s", len(spot_result.skipped), ", ".join(spot_result.skipped))
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
            short_regime_params,
            trend_regime_params,
        )
        from dataprice.timeutil import UTC

        chosen = [
            name
            for name in (
                "baseline",
                "flow",
                "regime",
                "trend_regime",
                "grid",
                "long",
                "short_regime",
                "debate_daily",
                "anti_sweep",
                "tpo_vwap",
            )
            if getattr(args, name)
        ]
        if len(chosen) > 1:
            parser.error(
                "--baseline、--flow、--regime、--trend-regime、--grid、--long、"
                "--short-regime、--debate-daily、--anti-sweep、--tpo-vwap 只能选一个"
            )
        if args.freq != "30m" and not args.tpo_vwap:
            parser.error("--freq 只和 --tpo-vwap 一起用")
        end = datetime.now(UTC)
        start = end - timedelta(days=args.months * 30)
        if args.tpo_vwap:
            if args.buy_only:
                parser.error("--tpo-vwap 不和 --buy-only 一起用")
            from dataprice.strategy.tpo_vwap import (
                TPO_FREQ_MINUTES,
                evaluated_params as tpo_vwap_evaluated_params,
                format_tpo_vwap_report,
                run_stored_tpo_vwap,
            )

            tpo_freq = args.freq.strip().lower()
            if tpo_freq not in TPO_FREQ_MINUTES:
                parser.error(f"--tpo-vwap 只支持 5m、10m、30m，收到 {args.freq}")
            tpo_params = replace(
                tpo_vwap_evaluated_params(),
                fee_bps=args.fee_bps,
                slippage_bps=args.slippage_bps,
                bar_minutes=TPO_FREQ_MINUTES[tpo_freq],
            )
            try:
                tpo = run_stored_tpo_vwap(
                    symbol=args.symbol,
                    start=start,
                    end=end,
                    params=tpo_params,
                    freq=tpo_freq,
                )
            except (RuntimeError, ValueError) as exc:
                parser.error(str(exc))
            logging.info("%s %s %s 根", args.symbol, tpo_freq, tpo.bars)
            for line in format_tpo_vwap_report(tpo, tpo_params).splitlines():
                logging.info("%s", line)
            return
        if args.anti_sweep:
            if args.buy_only:
                parser.error("--anti-sweep 不和 --buy-only 一起用")
            from dataprice.strategy.backtest import format_anti_sweep_report, run_stored_anti_sweep

            try:
                anti = run_stored_anti_sweep(
                    symbol=args.symbol,
                    start=start,
                    end=end,
                    fee_bps=args.fee_bps,
                    slippage_bps=args.slippage_bps,
                )
            except RuntimeError as exc:
                parser.error(str(exc))
            logging.info("%s 30m %s 根，60m %s 根", args.symbol, anti.bars_30, anti.bars_60)
            for line in format_anti_sweep_report(
                anti,
                fee_bps=args.fee_bps,
                slippage_bps=args.slippage_bps,
            ).splitlines():
                logging.info("%s", line)
            return
        if args.debate_daily:
            if args.buy_only:
                parser.error("--debate-daily 不和 --buy-only 一起用")
            from dataprice.strategy.debate import format_debate_report, run_stored_debate

            try:
                debate_result, bars_30, bars_60 = run_stored_debate(
                    symbol=args.symbol,
                    start=start,
                    end=end,
                    fee_bps=args.fee_bps,
                    slippage_bps=args.slippage_bps,
                )
            except RuntimeError as exc:
                parser.error(str(exc))
            logging.info("%s 30m %s 根，60m %s 根", args.symbol, bars_30, bars_60)
            for line in format_debate_report(
                debate_result,
                fee_bps=args.fee_bps,
                slippage_bps=args.slippage_bps,
            ).splitlines():
                logging.info("%s", line)
            return
        if args.grid:
            from dataprice.strategy.grid import GridParams, format_grid_report, run_stored_grid

            grid_params = GridParams(fee_bps=args.fee_bps, slippage_bps=args.slippage_bps)
            try:
                grid_result, bars_30, bars_60 = run_stored_grid(
                    symbol=args.symbol,
                    start=start,
                    end=end,
                    params=grid_params,
                )
            except RuntimeError as exc:
                parser.error(str(exc))
            logging.info("%s 30m %s 根，60m %s 根", args.symbol, bars_30, bars_60)
            for line in format_grid_report(grid_result, grid_params).splitlines():
                logging.info("%s", line)
            return
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
        elif args.short_regime:
            params = short_regime_params()
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
