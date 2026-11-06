"""一击必杀图表。不连接数据库，也不改 v0.1 的成交。"""

from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from dataprice.cli import main
from dataprice.strategy.candles import Candle
from dataprice.strategy.sniper.backtest import (
    SniperResult,
    SniperTrade,
    _gate_map,
    run_sniper,
    sniper_intraday_view,
)
from dataprice.strategy.sniper.chart import (
    GATE_FILL,
    LONG_COLOR,
    SHORT_COLOR,
    build_sniper_figure,
    parse_chart_time,
    resolve_chart_window,
    session_gate_map,
    write_sniper_html,
)
from dataprice.strategy.sniper.entry import find_retest
from dataprice.strategy.sniper.resample import to_4h, to_daily
from dataprice.timeutil import UTC

_FIXTURES = None


def test_paths_keep_each_trade_and_use_different_colors():
    bars = [_bar(index, 50 + index) for index in range(8)]
    daily = [_day(date(2026, 3, 2), 20), _day(date(2026, 3, 3), 21)]
    long = _trade(bars, "buy", 1, 2, "100", "110", "90", "130", "target", "long", "3")
    short = _trade(bars, "sell", 4, 6, "140", "120", "160", "80", "stop", "short", "-2")
    figure, notes = build_sniper_figure(
        bars,
        daily,
        _result(bars, (long, short)),
        params=_fx()._params(),
    )
    paths = _by_role(figure, "path")
    assert len(paths) == 2
    by_side = {trace.meta["side"]: trace for trace in paths}
    assert _as_utc(by_side["buy"].x[0]) == long.entry_ts
    assert _as_utc(by_side["buy"].x[1]) == long.exit_ts
    assert _as_utc(by_side["sell"].x[0]) == short.entry_ts
    assert _as_utc(by_side["sell"].x[1]) == short.exit_ts
    assert by_side["buy"].y[0] == pytest.approx(float(long.entry))
    assert by_side["buy"].y[1] == pytest.approx(float(long.exit))
    assert by_side["sell"].y[0] == pytest.approx(float(short.entry))
    assert by_side["sell"].y[1] == pytest.approx(float(short.exit))
    assert by_side["buy"].line.color == LONG_COLOR
    assert by_side["sell"].line.color == SHORT_COLOR
    assert LONG_COLOR != SHORT_COLOR
    assert _by_role(figure, "entry", "buy")[0].marker.symbol == "triangle-up"
    assert _by_role(figure, "entry", "sell")[0].marker.symbol == "triangle-down"
    assert _by_role(figure, "stop", "buy")[0].y == (pytest.approx(90), pytest.approx(90)) or list(
        _by_role(figure, "stop", "buy")[0].y
    ) == pytest.approx([90, 90])
    assert list(_by_role(figure, "target", "sell")[0].y) == pytest.approx([80, 80])
    assert _by_role(figure, "stop", "buy")[0].meta["entry_ts"] == long.entry_ts.isoformat()
    assert _by_role(figure, "target", "sell")[0].meta["exit_ts"] == short.exit_ts.isoformat()
    assert "目标" in _by_role(figure, "entry", "buy")[0].hovertext
    assert "止损" in _by_role(figure, "exit", "sell")[0].hovertext
    assert "PnL" in _by_role(figure, "path", "buy")[0].hovertext
    assert "价格R" in _by_role(figure, "path", "buy")[0].hovertext
    assert "账户R" in _by_role(figure, "path", "sell")[0].hovertext
    _assert_trades_stay_on_4h(figure)
    assert notes.trades_drawn == 2
    assert notes.zones == ()
    assert "不画信号区" in notes.text
    assert "顺序单仓" in notes.text


def test_run_sniper_long_uses_that_trade_and_the_real_zone():
    fx = _fx()
    daily = fx._dailies([10, 20, 30, 40, 50])
    signal = fx._hour4(date(2026, 1, 12), 0, 102, 106, 98, 102)
    target = fx._hour4(date(2026, 1, 12), 4, 102, 150, 101, 148)
    bars = fx._long_path(signal, target)
    params = fx._params()
    macro = fx._book([5, 4, 3, 2, 1])
    result = run_sniper(bars, daily, params, macro=macro)
    trade = result.trades[0]
    figure, notes = build_sniper_figure(bars, daily, result, params=params, macro=macro)
    path = _by_role(figure, "path", "buy")[0]
    assert _as_utc(path.x[0]) == trade.entry_ts
    assert _as_utc(path.x[1]) == trade.exit_ts
    assert path.y[0] == pytest.approx(float(trade.entry))
    assert path.y[1] == pytest.approx(float(trade.exit))
    assert list(_by_role(figure, "stop", "buy")[0].y) == pytest.approx([float(trade.stop), float(trade.stop)])
    assert list(_by_role(figure, "target", "buy")[0].y) == pytest.approx(
        [float(trade.target), float(trade.target)]
    )
    assert path.line.color == LONG_COLOR
    assert "target" in path.hovertext
    assert "2026-01-09" in path.hovertext
    assert "不是入场当天" in path.hovertext
    assert "日线图没有这个 UTC 日的日K" in path.hovertext
    signal_zone = find_retest(bars, trade.entry_index)
    assert signal_zone is not None
    assert len(notes.zones) == 1
    labeled = notes.zones[0]
    assert labeled.kind == signal_zone.kind
    assert labeled.side == signal_zone.side
    assert labeled.low == signal_zone.zone_low
    assert labeled.high == signal_zone.zone_high
    zone_trace = _by_role(figure, "zone")[0]
    assert zone_trace.meta["panel"] == "4h"
    assert zone_trace.meta["low"] == signal_zone.zone_low
    assert zone_trace.meta["high"] == signal_zone.zone_high
    daily_trace = _by_role(figure, "daily")[0]
    daily_times = [_as_utc(value) for value in daily_trace.x]
    assert date(2026, 1, 12) not in {item.date() for item in daily_times}
    assert trade.exit_ts not in daily_times
    assert all(item.hour == 0 and item.minute == 0 for item in daily_times)
    _assert_trades_stay_on_4h(figure)
    assert float(trade.entry) not in _prices(daily_trace)
    assert "不画 L0" not in notes.text
    assert session_gate_map(bars, macro, None, params) == _gate_map(bars, macro, None, params)


def test_run_sniper_short_uses_the_short_color():
    fx = _fx()
    daily = fx._dailies([50, 40, 30, 20, 10])
    signal = fx._hour4(date(2026, 1, 12), 0, 97, 105, 90, 97)
    target = fx._hour4(date(2026, 1, 12), 4, 90, 96, 10, 20)
    bars = fx._short_path(signal, target)
    params = fx._params()
    result = run_sniper(bars, daily, params, macro=fx._book([5, 4, 3, 2, 1]))
    figure, _notes = build_sniper_figure(
        bars,
        daily,
        result,
        params=params,
        macro=fx._book([5, 4, 3, 2, 1]),
    )
    path = _by_role(figure, "path", "sell")[0]
    assert path.line.color == SHORT_COLOR
    assert _by_role(figure, "entry", "sell")[0].marker.symbol == "triangle-down"
    assert _as_utc(path.x[0]) == result.trades[0].entry_ts
    assert _as_utc(path.x[1]) == result.trades[0].exit_ts
    assert not _by_role(figure, "path", "buy")


def test_time_window_limit_and_exit_extension():
    bars = [_bar(index, 40) for index in range(10)]
    early = _trade(bars, "buy", 1, 2, "10", "11", "8", "16", "target", "long", "1")
    held = _trade(bars, "buy", 4, 8, "20", "21", "15", "30", "end", "long", "1")
    later = _trade(bars, "sell", 8, 9, "50", "40", "60", "20", "stop", "short", "-1")
    start = bars[2].open_ts
    end = bars[6].open_ts
    figure, notes = build_sniper_figure(
        bars,
        [],
        _result(bars, (early, held, later)),
        start=start,
        end=end,
        limit=2,
    )
    paths = _by_role(figure, "path")
    assert len(paths) == 1
    assert paths[0].meta["entry_ts"] == held.entry_ts.isoformat()
    assert paths[0].meta["exit_ts"] == held.exit_ts.isoformat()
    shown = {_as_utc(value) for value in _by_role(figure, "4h")[0].x}
    assert bars[4].open_ts in shown
    assert bars[5].open_ts in shown
    assert bars[8].open_ts in shown
    assert bars[9].open_ts not in shown
    assert bars[1].open_ts not in shown
    assert notes.extended
    assert "补到出场" in notes.text
    assert "没有传入日线" in notes.text

    limited, limited_notes = build_sniper_figure(
        bars,
        [],
        _result(bars, (early, held, later)),
        limit=3,
    )
    assert {_trace.meta["entry_ts"] for _trace in _by_role(limited, "path")} == {later.entry_ts.isoformat()}
    assert not limited_notes.extended


def test_unaligned_trade_is_dropped():
    bars = [_bar(index, 30) for index in range(4)]
    trade = _trade(bars, "buy", 1, 2, "10", "12", "8", "18", "target", "long", "1")
    broken = SniperTrade(
        side=trade.side,
        entry_index=trade.entry_index,
        exit_index=trade.exit_index,
        entry_ts=bars[0].open_ts,
        exit_ts=trade.exit_ts,
        entry=trade.entry,
        exit=trade.exit,
        qty=trade.qty,
        stop=trade.stop,
        target=trade.target,
        reason=trade.reason,
        pnl=trade.pnl,
        price_r=trade.price_r,
        account_r=trade.account_r,
        planned_price_r=trade.planned_price_r,
        planned_account_r=trade.planned_account_r,
        leverage_used=trade.leverage_used,
        bias=trade.bias,
    )
    figure, notes = build_sniper_figure(bars, [], _result(bars, (broken,)))
    assert _by_role(figure, "path") == []
    assert notes.trades_drawn == 0
    assert "对不上" in notes.text


def test_volume_follows_the_candle():
    quiet = [_bar(index, 10, volume=0) for index in range(3)]
    loud = [_bar(index, 10, volume=2) for index in range(3)]
    quiet_figure, quiet_notes = build_sniper_figure(quiet, [], _result(quiet, ()))
    loud_figure, loud_notes = build_sniper_figure(loud, [], _result(loud, ()))
    assert _by_role(quiet_figure, "volume") == []
    assert not quiet_notes.volume_shown
    assert "不画成交量" in quiet_notes.text
    assert _by_role(loud_figure, "volume")[0].y == pytest.approx([2, 2, 2])
    assert loud_notes.volume_shown
    assert "成交量来自 K 线的 volume" in loud_notes.text


def test_gate_shade_uses_the_backtest_map_and_can_be_absent():
    fx = _fx()
    daily = fx._dailies([10, 20, 30, 40, 50])
    params = fx._params()
    signal = fx._hour4(date(2026, 1, 12), 0, 102, 106, 98, 102)
    bars = fx._long_path(
        signal,
        fx._hour4(date(2026, 1, 13), 0, 103, 107, 99, 104),
        fx._hour4(date(2026, 1, 14), 0, 104, 160, 100, 150),
    )
    macro = fx._book([5, 4, 3, 2, 1, 100])
    result = run_sniper(bars, daily, params, macro=macro)
    figure, notes = build_sniper_figure(bars, daily, result, params=params, macro=macro)
    assert notes.gate_available
    assert _covers(notes.gate_spans, datetime(2026, 1, 13, tzinfo=UTC))
    assert not _covers(notes.gate_spans, datetime(2026, 1, 12, tzinfo=UTC))
    assert any(shape.fillcolor == GATE_FILL for shape in figure.layout.shapes)
    assert "GATE=0" in notes.text
    legend = _by_role(figure, "gate-legend")[0]
    assert "GATE=0" in legend.name

    bare, bare_notes = build_sniper_figure(bars, daily, result, params=params)
    assert not bare_notes.gate_available
    assert bare_notes.gate_spans == ()
    assert list(bare.layout.shapes) == []
    assert "不画 L0 GATE=0" in bare_notes.text
    assert "未提供宏观门" in _by_role(bare, "gate-legend")[0].name


def test_html_file_embeds_plotly_and_the_trade(tmp_path: Path):
    bars = [_bar(index, 10, volume=1) for index in range(4)]
    trade = _trade(bars, "buy", 1, 2, "100", "110", "90", "130", "target", "long", "4.5")
    figure, notes = build_sniper_figure(bars, [_day(date(2026, 3, 2), 15)], _result(bars, (trade,)))
    output = tmp_path / "nested" / "btc.sniper.html"
    written = write_sniper_html(figure, notes, output)
    text = written.read_text(encoding="utf-8")
    assert written.is_file()
    assert len(text) > 100_000
    assert "Plotly.newPlot" in text
    assert '<script src="https://cdn.plot.ly' not in text
    assert "sniper-chart-legend" in text
    assert "100" in text
    assert "顺序单仓" in text
    assert "UTC" in text
    candles = _by_role(figure, "4h")[0]
    assert "开" in candles.hovertemplate
    assert "UTC" in candles.customdata[0]


def test_chart_does_not_run_its_own_backtest():
    source = Path(write_sniper_html.__globals__["__file__"]).read_text(encoding="utf-8")
    assert "run_sniper(" not in source
    assert "load_stored_sniper_view" in source


def test_intraday_view_is_the_same_run():
    fx = _fx()
    start = datetime(2026, 1, 5, tzinfo=UTC)
    hours = [
        Candle(start + timedelta(hours=hour), 10, 12, 9, 11, 1.5)
        for hour in range(48)
    ]
    params = fx._params()
    macro = fx._book([5, 4, 3, 2, 1])
    view = sniper_intraday_view(hours, params, bar_minutes=60, macro=macro, trade_start=start)
    direct = run_sniper(
        tuple(to_4h(hours, bar_minutes=60)),
        tuple(to_daily(hours, bar_minutes=60)),
        params,
        macro=macro,
        trade_start=start,
    )
    assert view.result == direct
    assert view.bars_4h == tuple(to_4h(hours, bar_minutes=60))
    assert view.daily == tuple(to_daily(hours, bar_minutes=60))
    assert view.macro is macro
    assert view.gates is None


def test_parse_window_and_cli_help(capsys):
    start = parse_chart_time("2026-01-12")
    end = parse_chart_time("2026-02-01T00:00:00Z")
    assert start == datetime(2026, 1, 12, tzinfo=UTC)
    assert end == datetime(2026, 2, 1, tzinfo=UTC)
    resolved_start, resolved_end = resolve_chart_window(
        months=None,
        start=start,
        end=end,
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert (resolved_start, resolved_end) == (start, end)
    default_start, default_end = resolve_chart_window(
        months=None,
        start=None,
        end=None,
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert default_end == datetime(2026, 5, 1, tzinfo=UTC)
    assert default_start == default_end - timedelta(days=24 * 30)
    with pytest.raises(ValueError):
        resolve_chart_window(months=0, start=None, end=end, now=end)
    with pytest.raises(SystemExit) as exc:
        main(["sniper-chart", "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "--output" in text
    assert "--limit" in text
    assert "--start" in text
    assert "--end" in text
    assert "--months" in text
    assert "顺序单仓" in text
    with pytest.raises(SystemExit) as bad:
        main(["sniper-chart", "--limit", "0", "--output", "/tmp/btc.sniper.html"])
    assert bad.value.code == 2


def _fx():
    global _FIXTURES
    if _FIXTURES is None:
        path = Path(__file__).with_name("test_sniper.py")
        spec = importlib.util.spec_from_file_location("sniper_test_fixtures", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _FIXTURES = module
    return _FIXTURES


def _bar(index: int, price: float, volume: float = 0.0) -> Candle:
    opened = datetime(2026, 3, 2, tzinfo=UTC) + timedelta(hours=4 * index)
    return Candle(opened, price, price + 2, price - 2, price, volume)


def _day(day: date, close: float) -> Candle:
    return Candle(datetime(day.year, day.month, day.day, tzinfo=UTC), close, close + 1, close - 1, close, 1)


def _trade(
    bars: list[Candle],
    side: str,
    entry_index: int,
    exit_index: int,
    entry: str,
    exit_px: str,
    stop: str,
    target: str,
    reason: str,
    bias: str,
    pnl: str,
) -> SniperTrade:
    return SniperTrade(
        side=side,  # type: ignore[arg-type]
        entry_index=entry_index,
        exit_index=exit_index,
        entry_ts=bars[entry_index].open_ts,
        exit_ts=bars[exit_index].open_ts,
        entry=Decimal(entry),
        exit=Decimal(exit_px),
        qty=Decimal("1"),
        stop=Decimal(stop),
        target=Decimal(target),
        reason=reason,  # type: ignore[arg-type]
        pnl=Decimal(pnl),
        price_r=1.0,
        account_r=-1.0 if pnl.startswith("-") else 1.0,
        planned_price_r=3.0,
        planned_account_r=3.0,
        leverage_used=20.0,
        bias=bias,  # type: ignore[arg-type]
    )


def _result(bars: list[Candle], trades: tuple[SniperTrade, ...]) -> SniperResult:
    return SniperResult(
        trades=trades,
        bars=len(bars),
        skipped=0,
        first_open=bars[0].open_ts,
        last_open=bars[-1].open_ts,
        first_close=bars[0].close,
        last_close=bars[-1].close,
    )


def _by_role(figure, role: str, side: str | None = None) -> list:
    found = []
    for trace in figure.data:
        meta = trace.meta or {}
        if not isinstance(meta, dict) or meta.get("role") != role:
            continue
        if side is not None and meta.get("side") != side:
            continue
        found.append(trace)
    return found


def _assert_trades_stay_on_4h(figure) -> None:
    trade_roles = {"path", "entry", "exit", "stop", "target", "zone"}
    trade_axes = set()
    daily_axes = set()
    for trace in figure.data:
        meta = trace.meta or {}
        if not isinstance(meta, dict):
            continue
        role = meta.get("role")
        if role in trade_roles:
            assert meta.get("panel") == "4h"
            trade_axes.add(trace.yaxis)
            if role == "entry":
                assert trace.marker.symbol in {"triangle-up", "triangle-down"}
        if meta.get("panel") == "daily":
            daily_axes.add(trace.yaxis)
            assert role not in trade_roles
            marker = getattr(trace, "marker", None)
            symbol = getattr(marker, "symbol", None) if marker is not None else None
            assert symbol not in {"triangle-up", "triangle-down", "x"}
    assert trade_axes
    assert daily_axes
    assert trade_axes.isdisjoint(daily_axes)


def _prices(trace) -> set[float]:
    values = []
    for name in ("open", "high", "low", "close", "y"):
        series = getattr(trace, name, None)
        if series is None:
            continue
        values.extend(float(item) for item in series if item is not None)
    return set(values)


def _as_utc(value) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _covers(spans, moment: datetime) -> bool:
    return any(start <= moment < end for start, end in spans)
