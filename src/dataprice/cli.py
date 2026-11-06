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
    backtest = sub.add_parser(
        "backtest",
        help="用 market.bars 跑 TPO 价值区加当日 VWAP 收回",
        description="用 market.bars 跑 TPO 价值区加当日 VWAP 收回。参数锁在 tpo_vwap.evaluated_params()。",
    )
    backtest.add_argument("--symbol", default="BTC-USDT")
    backtest.add_argument("--months", type=int, default=24, help="回测窗口，大约 30 天算一个月")
    backtest.add_argument(
        "--freq",
        default="30m",
        help="30m、5m 或 10m。默认 30m",
    )
    backtest.add_argument("--fee-bps", type=float, default=5.0, help="每边手续费，基点。默认 5")
    backtest.add_argument("--slippage-bps", type=float, default=2.0, help="每边不利滑点，基点。默认 2")
    sniper = sub.add_parser(
        "sniper-backtest",
        help="用 60m 合成 4H，跑一击必杀 v0",
        description=(
            "一击必杀 v0。宏观门、日线偏向、4H 回踩订单块或公允价值缺口。"
            "参数锁在 sniper.evaluated_params()。命令行只覆盖手续费和滑点。"
        ),
    )
    sniper.add_argument("--symbol", default="BTC-USDT")
    sniper.add_argument("--months", type=int, default=24, help="回测窗口，大约 30 天算一个月")
    sniper.add_argument("--fee-bps", type=float, default=5.0, help="每边手续费，基点。默认 5")
    sniper.add_argument("--slippage-bps", type=float, default=2.0, help="每边不利滑点，基点。默认 2")
    chart = sub.add_parser(
        "sniper-chart",
        help="把一击必杀的 4H / 日线和顺序单仓画成 HTML",
        description=(
            "用与 sniper-backtest 相同的顺序单仓画 4H K 线和 UTC 日线偏向。"
            "不另跑独立信号。策略参数仍锁在 sniper.evaluated_params()，命令行只覆盖手续费和滑点。"
        ),
    )
    chart.add_argument("--symbol", default="BTC-USDT")
    window = chart.add_mutually_exclusive_group()
    window.add_argument(
        "--months",
        type=int,
        default=None,
        help="回测窗口，大约 30 天算一个月。默认 24。和 --start 二选一",
    )
    window.add_argument(
        "--start",
        help="UTC 起始，包含该时刻。YYYY-MM-DD 或 ISO 时间。给出后不用 --months",
    )
    chart.add_argument("--end", help="UTC 结束，不包含该时刻。默认现在")
    chart.add_argument(
        "--limit",
        type=int,
        default=None,
        help="只画时间窗内最近的 N 根 4H。这些入场的出场若更晚，会把 K 线补到出场，补上的部分不再增加入场",
    )
    chart.add_argument(
        "--output",
        required=True,
        help="HTML 路径。不要写进仓库，例如 /tmp/btc.sniper.html",
    )
    chart.add_argument("--fee-bps", type=float, default=5.0, help="每边手续费，基点。默认 5")
    chart.add_argument("--slippage-bps", type=float, default=2.0, help="每边不利滑点，基点。默认 2")
    web = sub.add_parser(
        "sniper-web",
        help="在本机打开一击必杀图表页面",
        description=(
            "本机网页跑与 sniper-chart 相同的顺序单仓，并把每次结果写到输出目录。"
            "默认只监听 127.0.0.1。不开启 debug，也不自动重载。"
        ),
    )
    web.add_argument("--host", default="127.0.0.1", help="默认 127.0.0.1")
    web.add_argument("--port", type=int, default=8765, help="默认 8765")
    web.add_argument(
        "--output-dir",
        default="var/sniper-web",
        help="运行结果目录。默认 var/sniper-web，已在 .gitignore 的 var/ 里",
    )
    macro = sub.add_parser(
        "macro-backfill",
        help="写入 DXY、10Y TIPS、HY OAS、VIX，并合成 factor_macro_gate v0.1",
        description="把四条日频写入 macro_daily，并合成 factor_macro_gate v0.1。",
    )
    macro_source = macro.add_mutually_exclusive_group(required=True)
    macro_source.add_argument("--csv", help="series_id,obs_date,close。序列名是 dxy、tips_10y、hy_oas、vix")
    macro_source.add_argument(
        "--fetch",
        action="store_true",
        help="从 FRED 和 Yahoo 拉日频。拉不到就改用 --csv",
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

        from dataprice.strategy.tpo_vwap import (
            TPO_FREQ_MINUTES,
            evaluated_params as tpo_vwap_evaluated_params,
            format_tpo_vwap_report,
            run_stored_tpo_vwap,
        )
        from dataprice.timeutil import UTC

        tpo_freq = args.freq.strip().lower()
        if tpo_freq not in TPO_FREQ_MINUTES:
            parser.error(f"backtest 只支持 5m、10m、30m，收到 {args.freq}")
        end = datetime.now(UTC)
        start = end - timedelta(days=args.months * 30)
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
    elif args.command == "sniper-backtest":
        from dataclasses import replace
        from datetime import datetime, timedelta

        from dataprice.strategy.sniper.backtest import run_stored_sniper
        from dataprice.strategy.sniper.params import evaluated_params as sniper_evaluated_params
        from dataprice.strategy.sniper.report import format_sniper_report
        from dataprice.timeutil import UTC

        end = datetime.now(UTC)
        start = end - timedelta(days=args.months * 30)
        sniper_params = replace(
            sniper_evaluated_params(),
            fee_bps=args.fee_bps,
            slippage_bps=args.slippage_bps,
        )
        try:
            sniper_result = run_stored_sniper(
                symbol=args.symbol,
                start=start,
                end=end,
                params=sniper_params,
            )
        except (RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        for line in format_sniper_report(sniper_result, sniper_params).splitlines():
            logging.info("%s", line)
    elif args.command == "sniper-chart":
        from dataclasses import replace
        from datetime import datetime

        from dataprice.strategy.sniper.chart import (
            parse_chart_time,
            resolve_chart_window,
            write_sniper_chart_from_store,
        )
        from dataprice.strategy.sniper.params import evaluated_params as sniper_evaluated_params
        from dataprice.timeutil import UTC

        if args.limit is not None and args.limit < 1:
            parser.error("limit 至少为 1")
        now = datetime.now(UTC)
        try:
            start_at = parse_chart_time(args.start) if args.start else None
            end_at = parse_chart_time(args.end) if args.end else None
            start_at, end_at = resolve_chart_window(
                months=args.months,
                start=start_at,
                end=end_at,
                now=now,
            )
        except ValueError as exc:
            parser.error(str(exc))
        chart_params = replace(
            sniper_evaluated_params(),
            fee_bps=args.fee_bps,
            slippage_bps=args.slippage_bps,
        )
        try:
            chart_path = write_sniper_chart_from_store(
                symbol=args.symbol,
                start=start_at,
                end=end_at,
                output=args.output,
                limit=args.limit,
                params=chart_params,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        logging.info("图表已写入 %s", chart_path)
    elif args.command == "sniper-web":
        from pathlib import Path

        from dataprice.strategy.sniper.web import serve_sniper_web

        if not str(args.host).strip():
            parser.error("host 不能为空")
        if args.port < 1 or args.port > 65535:
            parser.error("port 必须在 1 到 65535")
        serve_sniper_web(host=args.host, port=args.port, output_dir=Path(args.output_dir))
    elif args.command == "macro-backfill":
        from dataprice.strategy.sniper.fetch import fetch_macro_book
        from dataprice.strategy.sniper.macro_gate import parse_macro_csv
        from dataprice.strategy.sniper.store import write_macro

        try:
            if args.csv:
                from pathlib import Path

                book = parse_macro_csv(Path(args.csv).read_text(encoding="utf-8"))
                source = "csv"
            else:
                book = fetch_macro_book()
                source = "live"
            daily_n, gate_n = write_macro(book, source)
        except (OSError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        logging.info("宏观日频 %s 行，factor_macro_gate v0.1 %s 行", daily_n, gate_n)
    else:
        parser.error(args.command)


def _freq(value: str) -> str:
    key = value.strip().lower()
    if key in {"1h", "60m", "1hr"}:
        return "60m"
    if key == "30m":
        return "30m"
    raise SystemExit(f"只支持 30m 和 60m（1h），收到 {value}")
