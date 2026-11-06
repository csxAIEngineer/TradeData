"""把一击必杀的 K 线和顺序单仓画成独立 HTML。

成交只用已经跑完的 ``SniperResult``。读库时走 ``load_stored_sniper_view``，
和 ``sniper-backtest`` 是同一次 ``run_sniper``。这里不另造独立信号回测，
也不改止损、目标和宏观门。

订单块 / 公允价值缺口只在 ``find_retest`` 能对上这笔入场时才画。
L0 阴影只在调用方传入了宏观序列或已经算好的门时才画。没有这些输入就在图例里写明。
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from dataprice.strategy.candles import Candle, assert_sorted
from dataprice.strategy.sniper.backtest import SniperResult, SniperTrade
from dataprice.strategy.sniper.bias import DailyBias, bias_for_session, daily_bias
from dataprice.strategy.sniper.entry import (
    Zone,
    _KIND_RANK,
    _fresh,
    _holds,
    find_retest,
    liquidity_zones,
)
from dataprice.strategy.sniper.macro_gate import EffectiveGate, GateCalendar, MacroBook
from dataprice.strategy.sniper.params import SniperParams, evaluated_params
from dataprice.timeutil import UTC, ensure_aware

LONG_COLOR = "#1b8a5a"
SHORT_COLOR = "#d62728"
GATE_FILL = "rgba(90, 90, 90, 0.16)"
_FOUR_H = timedelta(hours=4)
_REASON = {"stop": "止损", "target": "目标", "end": "数据结束收尾"}
_ZONE_NAME = {
    ("order_block", "buy"): "看涨订单块",
    ("order_block", "sell"): "看跌订单块",
    ("fvg", "buy"): "看涨公允价值缺口",
    ("fvg", "sell"): "看跌公允价值缺口",
}
_ZONE_FILL = {
    ("order_block", "buy"): "rgba(27, 138, 90, 0.18)",
    ("order_block", "sell"): "rgba(214, 39, 40, 0.16)",
    ("fvg", "buy"): "rgba(46, 134, 193, 0.20)",
    ("fvg", "sell"): "rgba(230, 126, 34, 0.20)",
}
_ZONE_LINE = {
    ("order_block", "buy"): "#1b8a5a",
    ("order_block", "sell"): "#d62728",
    ("fvg", "buy"): "#2e86c1",
    ("fvg", "sell"): "#e67e22",
}


@dataclass(frozen=True)
class LabeledZone:
    """一笔入场对上的那一块。价格和时间都来自 ``find_retest`` 选中的区。"""

    kind: str
    side: str
    low: float
    high: float
    formed_ts: datetime
    entry_ts: datetime


@dataclass(frozen=True)
class ChartNotes:
    """图例正文，以及测试用来核对的结构化结果。"""

    text: str
    trades_drawn: int
    volume_shown: bool
    gate_available: bool
    gate_spans: tuple[tuple[datetime, datetime], ...]
    zones: tuple[LabeledZone, ...]
    extended: bool


def parse_chart_time(value: str) -> datetime:
    """解析 CLI 的日期或时间。没有时区就当作 UTC。"""
    text = value.strip()
    if not text:
        raise ValueError("时间不能为空")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) == 10:
        try:
            day = date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"无法解析日期 {value}") from exc
        return datetime(day.year, day.month, day.day, tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析时间 {value}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def resolve_chart_window(
    *,
    months: int | None,
    start: datetime | None,
    end: datetime | None,
    now: datetime,
) -> tuple[datetime, datetime]:
    """``--months`` 和 ``sniper-backtest`` 一样，大约 30 天算一个月。``end`` 不含。"""
    ensure_aware(now)
    end_at = now if end is None else end
    ensure_aware(end_at)
    if start is None:
        span = 24 if months is None else months
        if span < 1:
            raise ValueError("months 至少为 1")
        start_at = end_at - timedelta(days=span * 30)
    else:
        ensure_aware(start)
        start_at = start
    if start_at >= end_at:
        raise ValueError("start 必须早于 end")
    return start_at, end_at


def session_gate_map(
    bars: Sequence[Candle],
    macro: MacroBook | None,
    gates: Mapping[date, EffectiveGate] | None,
    params: SniperParams,
) -> dict[date, EffectiveGate] | None:
    """和回测 ``_gate_map`` 同一条规则。两个都没给就返回 None，图上不画门。"""
    if gates is not None:
        return dict(gates)
    if macro is None:
        return None
    calendar = GateCalendar(macro, params)
    found: dict[date, EffectiveGate] = {}
    for bar in bars:
        session = bar.open_ts.astimezone(UTC).date()
        if session in found:
            continue
        row = calendar.on(session)
        if row is not None:
            found[session] = row
    return found


def zone_for_trade(bars: Sequence[Candle], trade: SniperTrade) -> Zone | None:
    """返回这笔入场回踩的那一块。对不上就不编一块。"""
    if trade.entry_index < 0 or trade.entry_index >= len(bars):
        return None
    zones = liquidity_zones(bars)
    signal = find_retest(bars, trade.entry_index, zones)
    if signal is None or signal.side != trade.side:
        return None
    bar = bars[trade.entry_index]
    hits = [
        zone
        for zone in zones
        if zone.formed < trade.entry_index
        and zone.kind == signal.kind
        and zone.side == signal.side
        and zone.low == signal.zone_low
        and zone.high == signal.zone_high
        and _fresh(bars, zone, trade.entry_index)
        and _holds(bar, zone)
    ]
    if not hits:
        return None
    return max(hits, key=lambda zone: (zone.formed, -_KIND_RANK[zone.kind]))


def build_sniper_figure(
    bars_4h: Sequence[Candle],
    daily: Sequence[Candle],
    result: SniperResult,
    *,
    params: SniperParams | None = None,
    macro: MacroBook | None = None,
    gates: Mapping[date, EffectiveGate] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
    symbol: str = "BTC-USDT",
) -> tuple[go.Figure, ChartNotes]:
    """``bars_4h`` 必须是产生 ``result`` 的那一串，下标才对得上。"""
    params = params or evaluated_params()
    if start is not None:
        ensure_aware(start)
    if end is not None:
        ensure_aware(end)
    assert_sorted(bars_4h)
    if daily:
        assert_sorted(daily)
    seed = _window_indices(bars_4h, start, end, limit)
    drawn, skipped = _aligned_trades(bars_4h, result.trades, set(seed))
    visible = set(seed)
    for trade in drawn:
        visible.update(range(trade.entry_index, trade.exit_index + 1))
    visible_bars = [bars_4h[index] for index in sorted(visible)]
    extended = any(index not in seed for index in visible)
    gate_map = session_gate_map(bars_4h, macro, gates, params)
    spans = tuple(_closed_spans(visible_bars, gate_map)) if gate_map is not None else ()
    bias_rows = daily_bias(daily, params) if daily else ()
    shown_daily = _shown_daily(visible_bars, daily, bias_rows)
    labeled = _label_zones(bars_4h, drawn)
    figure = _figure(
        symbol=symbol,
        visible_bars=visible_bars,
        shown_daily=shown_daily,
        bias_rows=bias_rows,
        drawn=drawn,
        labeled=labeled,
        spans=spans,
        gate_available=gate_map is not None,
    )
    notes = ChartNotes(
        text=_notes_text(
            drawn=len(drawn),
            skipped=skipped,
            volume_shown=any(bar.volume > 0 for bar in visible_bars),
            gate_available=gate_map is not None,
            spans=spans,
            zones=labeled,
            extended=extended,
            has_daily=bool(shown_daily),
            start=start,
            end=end,
            limit=limit,
        ),
        trades_drawn=len(drawn),
        volume_shown=any(bar.volume > 0 for bar in visible_bars),
        gate_available=gate_map is not None,
        gate_spans=spans,
        zones=tuple(labeled),
        extended=extended,
    )
    return figure, notes


def render_sniper_html(figure: go.Figure, notes: ChartNotes) -> str:
    """独立 HTML。plotly.js 嵌在页面里，打开时不需要服务。"""
    page = figure.to_html(
        full_html=True,
        include_plotlyjs=True,
        config={
            "scrollZoom": True,
            "displayModeBar": True,
            "displaylogo": False,
            "doubleClick": "reset",
        },
    )
    block = (
        '<section id="sniper-chart-legend" style="font-family: sans-serif; '
        'max-width: 1080px; margin: 16px 24px 0; line-height: 1.55; white-space: pre-wrap;">'
        f"{html.escape(notes.text)}</section>"
    )
    if "<body>" in page:
        return page.replace("<body>", "<body>\n" + block, 1)
    return block + page


def write_sniper_html(figure: go.Figure, notes: ChartNotes, output: Path | str) -> Path:
    """写出不依赖服务器的 HTML。plotly.js 嵌在文件里。"""
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_sniper_html(figure, notes), encoding="utf-8")
    return path


def write_sniper_chart_from_store(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    output: Path | str,
    limit: int | None = None,
    params: SniperParams | None = None,
    settings: object | None = None,
) -> Path:
    """读 ClickHouse，用 ``load_stored_sniper_view`` 的那次顺序单仓画图。"""
    from dataprice.config import Settings
    from dataprice.strategy.sniper.backtest import load_stored_sniper_view

    if settings is not None and not isinstance(settings, Settings):
        raise TypeError("settings 必须是 Settings")
    params = params or evaluated_params()
    view = load_stored_sniper_view(
        symbol=symbol,
        start=start,
        end=end,
        params=params,
        settings=settings,
    )
    figure, notes = build_sniper_figure(
        view.bars_4h,
        view.daily,
        view.result,
        params=params,
        macro=view.macro,
        gates=view.gates,
        start=start,
        end=end,
        limit=limit,
        symbol=symbol,
    )
    return write_sniper_html(figure, notes, output)


def _window_indices(
    bars: Sequence[Candle],
    start: datetime | None,
    end: datetime | None,
    limit: int | None,
) -> list[int]:
    if limit is not None and limit < 1:
        raise ValueError("limit 至少为 1")
    chosen = [
        index
        for index, bar in enumerate(bars)
        if (start is None or bar.open_ts >= start) and (end is None or bar.open_ts < end)
    ]
    if limit is not None:
        chosen = chosen[-limit:]
    if not chosen:
        raise ValueError("时间窗内没有 4H K 线")
    return chosen


def _aligned_trades(
    bars: Sequence[Candle],
    trades: Sequence[SniperTrade],
    seed: set[int],
) -> tuple[list[SniperTrade], int]:
    drawn: list[SniperTrade] = []
    skipped = 0
    for trade in trades:
        if trade.entry_index not in seed:
            continue
        if not _aligned(bars, trade):
            skipped += 1
            continue
        drawn.append(trade)
    return drawn, skipped


def _aligned(bars: Sequence[Candle], trade: SniperTrade) -> bool:
    count = len(bars)
    if not (0 <= trade.entry_index < count and 0 <= trade.exit_index < count):
        return False
    if trade.exit_index < trade.entry_index:
        return False
    return (
        bars[trade.entry_index].open_ts == trade.entry_ts
        and bars[trade.exit_index].open_ts == trade.exit_ts
    )


def _closed_spans(
    bars: Sequence[Candle],
    gate_map: Mapping[date, EffectiveGate],
) -> list[tuple[datetime, datetime]]:
    spans: list[tuple[datetime, datetime]] = []
    current: tuple[datetime, datetime] | None = None
    for bar in bars:
        session = bar.open_ts.astimezone(UTC).date()
        row = gate_map.get(session)
        closed = row is None or row.gate == 0
        start = bar.open_ts
        end = bar.open_ts + _FOUR_H
        if not closed:
            if current is not None:
                spans.append(current)
                current = None
            continue
        if current is not None and current[1] == start:
            current = (current[0], end)
        else:
            if current is not None:
                spans.append(current)
            current = (start, end)
    if current is not None:
        spans.append(current)
    return spans


def _shown_daily(
    visible_bars: Sequence[Candle],
    daily: Sequence[Candle],
    bias_rows: Sequence[DailyBias],
) -> list[tuple[Candle, DailyBias]]:
    if not visible_bars or not daily or not bias_rows:
        return []
    sessions = {bar.open_ts.astimezone(UTC).date() for bar in visible_bars}
    first_session = min(sessions)
    last_session = max(sessions)
    selected: list[tuple[Candle, DailyBias]] = []
    prior: tuple[Candle, DailyBias] | None = None
    for bar, row in zip(daily, bias_rows, strict=True):
        if row.day < first_session:
            prior = (bar, row)
            continue
        if row.day > last_session:
            break
        if prior is not None:
            selected.append(prior)
            prior = None
        selected.append((bar, row))
    return selected


def _label_zones(bars: Sequence[Candle], trades: Sequence[SniperTrade]) -> list[LabeledZone]:
    labeled: list[LabeledZone] = []
    for trade in trades:
        zone = zone_for_trade(bars, trade)
        if zone is None or not (0 <= zone.formed < len(bars)):
            continue
        labeled.append(
            LabeledZone(
                kind=zone.kind,
                side=zone.side,
                low=zone.low,
                high=zone.high,
                formed_ts=bars[zone.formed].open_ts,
                entry_ts=trade.entry_ts,
            )
        )
    return labeled


def _figure(
    *,
    symbol: str,
    visible_bars: Sequence[Candle],
    shown_daily: Sequence[tuple[Candle, DailyBias]],
    bias_rows: Sequence[DailyBias],
    drawn: Sequence[SniperTrade],
    labeled: Sequence[LabeledZone],
    spans: Sequence[tuple[datetime, datetime]],
    gate_available: bool,
) -> go.Figure:
    volume_shown = any(bar.volume > 0 for bar in visible_bars)
    panels: list[str] = ["4h"]
    titles = ["4H"]
    heights = [0.62]
    if volume_shown:
        panels.append("volume")
        titles.append("成交量")
        heights = [0.52, 0.14]
    if shown_daily:
        panels.append("daily")
        titles.append("UTC 日线")
        heights = [0.50, 0.13, 0.37] if volume_shown else [0.64, 0.36]
    row_of = {name: index + 1 for index, name in enumerate(panels)}
    figure = make_subplots(
        rows=len(panels),
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=heights,
        subplot_titles=titles,
    )
    _add_candles(figure, visible_bars, row_of["4h"], "4H", "4h")
    if volume_shown:
        _add_volume(figure, visible_bars, row_of["volume"])
    if shown_daily:
        _add_daily(figure, shown_daily, bias_rows, visible_bars, row_of["daily"])
    _add_zones(figure, labeled, visible_bars[0].open_ts, row_of["4h"])
    _add_trades(figure, drawn, bias_rows, shown_daily, labeled, row_of["4h"])
    _add_gate(figure, spans, gate_available, row_of["4h"])
    _add_gate_shapes(figure, spans)
    x_start = visible_bars[0].open_ts
    if shown_daily:
        x_start = min(x_start, shown_daily[0][0].open_ts)
    x_end = visible_bars[-1].open_ts + _FOUR_H
    figure.update_xaxes(rangeslider_visible=False)
    figure.update_xaxes(
        range=[x_start, x_end],
        title_text="时间（悬停中的钟点是 UTC）",
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
    )
    figure.update_xaxes(
        rangeselector=dict(
            buttons=[
                dict(count=14, label="14天", step="day", stepmode="backward"),
                dict(count=3, label="3个月", step="month", stepmode="backward"),
                dict(step="all", label="全部"),
            ],
            font=dict(size=11),
        ),
        row=1,
        col=1,
    )
    figure.update_yaxes(title_text="价格", row=row_of["4h"], col=1)
    if volume_shown:
        figure.update_yaxes(title_text="量", row=row_of["volume"], col=1)
    if shown_daily:
        figure.update_yaxes(title_text="价格", row=row_of["daily"], col=1)
    figure.update_layout(
        title=f"{symbol} 一击必杀 v0.1 顺序单仓",
        height=920 if shown_daily else 680,
        margin=dict(l=64, r=200, t=96, b=48),
        legend=dict(orientation="v", y=1, x=1.01, xanchor="left"),
        hovermode="closest",
        dragmode="zoom",
        xaxis_rangeslider_visible=False,
    )
    return figure


def _add(figure: go.Figure, trace: go.BaseTraceType, row: int, meta: dict[str, object]) -> None:
    figure.add_trace(trace, row=row, col=1)
    figure.data[-1].meta = meta


def _add_candles(
    figure: go.Figure,
    bars: Sequence[Candle],
    row: int,
    name: str,
    panel: str,
) -> None:
    stamps = [_stamp(bar.open_ts) for bar in bars]
    _add(
        figure,
        go.Candlestick(
            x=[bar.open_ts for bar in bars],
            open=[bar.open for bar in bars],
            high=[bar.high for bar in bars],
            low=[bar.low for bar in bars],
            close=[bar.close for bar in bars],
            name=name,
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
            customdata=stamps,
            hovertemplate=(
                f"{name} %{{customdata}}<br>开 %{{open}}<br>高 %{{high}}"
                "<br>低 %{low}<br>收 %{close}<extra></extra>"
            ),
        ),
        row,
        {"role": panel, "panel": panel},
    )


def _add_volume(figure: go.Figure, bars: Sequence[Candle], row: int) -> None:
    colors = ["#26a69a" if bar.close >= bar.open else "#ef5350" for bar in bars]
    _add(
        figure,
        go.Bar(
            x=[bar.open_ts for bar in bars],
            y=[bar.volume for bar in bars],
            marker_color=colors,
            name="成交量",
            customdata=[_stamp(bar.open_ts) for bar in bars],
            hovertemplate="成交量 %{customdata}<br>%{y}<extra></extra>",
        ),
        row,
        {"role": "volume", "panel": "volume"},
    )


def _add_daily(
    figure: go.Figure,
    shown: Sequence[tuple[Candle, DailyBias]],
    bias_rows: Sequence[DailyBias],
    visible_bars: Sequence[Candle],
    row: int,
) -> None:
    bars = [bar for bar, _row in shown]
    _add_candles(figure, bars, row, "日线", "daily")
    ema_x: list[datetime] = []
    ema_y: list[float] = []
    ema_text: list[str] = []
    for bar, bias in shown:
        if bias.ema is None:
            continue
        ema_x.append(bar.open_ts)
        ema_y.append(bias.ema)
        ema_text.append(f"EMA {_stamp(bar.open_ts)} {bias.ema:.4f}")
    if ema_x:
        _add(
            figure,
            go.Scatter(
                x=ema_x,
                y=ema_y,
                mode="lines",
                line=dict(color="#5c6bc0", width=1.4),
                name="日线 EMA",
                hovertext=ema_text,
                hovertemplate="%{hovertext}<extra></extra>",
            ),
            row,
            {"role": "ema", "panel": "daily"},
        )
    sessions = {bar.open_ts.astimezone(UTC).date() for bar in visible_bars}
    _add_bias_markers(figure, shown, bias_rows, sessions, row)


def _add_bias_markers(
    figure: go.Figure,
    shown: Sequence[tuple[Candle, DailyBias]],
    bias_rows: Sequence[DailyBias],
    sessions: set[date],
    row: int,
) -> None:
    next_x: list[datetime] = []
    next_y: list[float] = []
    next_text: list[str] = []
    next_color: list[str] = []
    active_x: list[datetime] = []
    active_y: list[float] = []
    active_text: list[str] = []
    active_color: list[str] = []
    for bar, bias in shown:
        next_x.append(bar.open_ts)
        next_y.append(bar.high)
        next_text.append(
            f"{bias.day.isoformat()} 收完后的偏向是 {bias.bias}。"
            "从下一会话的 4H 才用。当天的 4H 不用这根收盘。"
            "圆点落在这根日K的最高价上，只为标出偏向，不是成交。"
        )
        next_color.append(_bias_color(bias.bias))
        if bias.day not in sessions:
            continue
        active = bias_for_session(bias_rows, bias.day)
        active_x.append(bar.open_ts)
        active_y.append(bar.low)
        if active is None:
            active_text.append(
                f"{bias.day.isoformat()} 没有更早的已收完日线。回测这一天不开新仓。"
                "空心菱形落在这根日K的最低价上，不是成交。"
            )
            active_color.append(_bias_color("flat"))
        else:
            active_text.append(
                f"{bias.day.isoformat()} 的 4H 生效偏向是 {active.bias}，"
                f"来自已收完的 {active.day.isoformat()}。"
                "不是这根日K当天的收盘。"
                "空心菱形落在这根日K的最低价上，不是成交。"
            )
            active_color.append(_bias_color(active.bias))
    _add(
        figure,
        go.Scatter(
            x=next_x,
            y=next_y,
            mode="markers",
            marker=dict(symbol="circle", size=9, color=next_color, line=dict(width=1, color="#ffffff")),
            name="日线收完后的偏向",
            hovertext=next_text,
            hovertemplate="%{hovertext}<extra></extra>",
        ),
        row,
        {"role": "bias-next", "panel": "daily"},
    )
    if active_x:
        _add(
            figure,
            go.Scatter(
                x=active_x,
                y=active_y,
                mode="markers",
                marker=dict(
                    symbol="diamond-open",
                    size=11,
                    color=active_color,
                    line=dict(width=2, color=active_color),
                ),
                name="当日 4H 生效偏向",
                hovertext=active_text,
                hovertemplate="%{hovertext}<extra></extra>",
            ),
            row,
            {"role": "bias-active", "panel": "daily"},
        )


def _add_zones(
    figure: go.Figure,
    labeled: Sequence[LabeledZone],
    visible_start: datetime,
    row: int,
) -> None:
    seen: set[str] = set()
    for zone in labeled:
        key = (zone.kind, zone.side)
        name = _ZONE_NAME[key]
        x0 = zone.formed_ts if zone.formed_ts >= visible_start else visible_start
        x1 = zone.entry_ts + _FOUR_H
        if x1 <= x0:
            x1 = x0 + _FOUR_H
        hover = (
            f"{name} {_num(zone.low)}–{_num(zone.high)}。"
            f"形成于 {_stamp(zone.formed_ts)}，入场 {_stamp(zone.entry_ts)}。"
            "这是 find_retest 对上的那一块，不是另行推测的区域。"
        )
        _add(
            figure,
            go.Scatter(
                x=[x0, x1, x1, x0, x0],
                y=[zone.low, zone.low, zone.high, zone.high, zone.low],
                mode="lines",
                fill="toself",
                fillcolor=_ZONE_FILL[key],
                line=dict(color=_ZONE_LINE[key], width=1),
                name=name,
                legendgroup=name,
                showlegend=name not in seen,
                hovertext=hover,
                hovertemplate="%{hovertext}<extra></extra>",
            ),
            row,
            {
                "role": "zone",
                "panel": "4h",
                "kind": zone.kind,
                "side": zone.side,
                "low": zone.low,
                "high": zone.high,
                "formed_ts": zone.formed_ts.isoformat(),
                "entry_ts": zone.entry_ts.isoformat(),
            },
        )
        seen.add(name)


def _add_trades(
    figure: go.Figure,
    trades: Sequence[SniperTrade],
    bias_rows: Sequence[DailyBias],
    shown_daily: Sequence[tuple[Candle, DailyBias]],
    labeled: Sequence[LabeledZone],
    row: int,
) -> None:
    daily_days = {bias.day for _bar, bias in shown_daily}
    seen_side: set[str] = set()
    zone_at = {(zone.entry_ts, zone.side): zone for zone in labeled}
    for index, trade in enumerate(trades, start=1):
        color = LONG_COLOR if trade.side == "buy" else SHORT_COLOR
        label = "多" if trade.side == "buy" else "空"
        show = trade.side not in seen_side
        seen_side.add(trade.side)
        hover = _trade_hover(trade, bias_rows, daily_days, zone_at.get((trade.entry_ts, trade.side)))
        meta = {
            "panel": "4h",
            "side": trade.side,
            "index": index,
            "entry_ts": trade.entry_ts.isoformat(),
            "exit_ts": trade.exit_ts.isoformat(),
            "entry": _num(trade.entry),
            "exit": _num(trade.exit),
            "stop": _num(trade.stop),
            "target": _num(trade.target),
            "reason": trade.reason,
        }
        _add(
            figure,
            go.Scatter(
                x=[trade.entry_ts, trade.exit_ts],
                y=[float(trade.entry), float(trade.exit)],
                mode="lines",
                line=dict(color=color, width=2),
                name=label,
                legendgroup=label,
                showlegend=show,
                hovertext=hover,
                hovertemplate="%{hovertext}<extra></extra>",
            ),
            row,
            {**meta, "role": "path"},
        )
        _add(
            figure,
            go.Scatter(
                x=[trade.entry_ts, trade.exit_ts],
                y=[float(trade.stop), float(trade.stop)],
                mode="lines",
                line=dict(color=color, width=1.4, dash="dash"),
                name=f"{label} 止损",
                legendgroup=label,
                showlegend=False,
                hovertext=hover,
                hovertemplate="%{hovertext}<extra></extra>",
            ),
            row,
            {**meta, "role": "stop"},
        )
        _add(
            figure,
            go.Scatter(
                x=[trade.entry_ts, trade.exit_ts],
                y=[float(trade.target), float(trade.target)],
                mode="lines",
                line=dict(color=color, width=1.4, dash="dot"),
                name=f"{label} 目标",
                legendgroup=label,
                showlegend=False,
                hovertext=hover,
                hovertemplate="%{hovertext}<extra></extra>",
            ),
            row,
            {**meta, "role": "target"},
        )
        _add(
            figure,
            go.Scatter(
                x=[trade.entry_ts],
                y=[float(trade.entry)],
                mode="markers",
                marker=dict(
                    symbol="triangle-up" if trade.side == "buy" else "triangle-down",
                    size=13,
                    color=color,
                    line=dict(width=1, color="#ffffff"),
                ),
                name=f"{label} 入场",
                legendgroup=label,
                showlegend=False,
                hovertext=hover,
                hovertemplate="%{hovertext}<extra></extra>",
            ),
            row,
            {**meta, "role": "entry"},
        )
        _add(
            figure,
            go.Scatter(
                x=[trade.exit_ts],
                y=[float(trade.exit)],
                mode="markers",
                marker=dict(symbol="x", size=12, color=color, line=dict(width=2, color=color)),
                name=f"{label} 出场",
                legendgroup=label,
                showlegend=False,
                hovertext=hover,
                hovertemplate="%{hovertext}<extra></extra>",
            ),
            row,
            {**meta, "role": "exit"},
        )


def _add_gate(
    figure: go.Figure,
    spans: Sequence[tuple[datetime, datetime]],
    gate_available: bool,
    row: int,
) -> None:
    if not gate_available:
        name = "未提供宏观门，不画 GATE=0"
    elif not spans:
        name = "窗口内没有 GATE=0"
    else:
        name = "L0 GATE=0 或无生效门"
    _add(
        figure,
        go.Scatter(
            x=[None],
            y=[None],
            mode="markers",
            marker=dict(symbol="square", size=12, color=GATE_FILL, line=dict(width=1, color="#666666")),
            name=name,
            hoverinfo="skip",
        ),
        row,
        {"role": "gate-legend", "panel": "4h", "available": gate_available, "spans": len(spans)},
    )


def _add_gate_shapes(figure: go.Figure, spans: Sequence[tuple[datetime, datetime]]) -> None:
    for start, end in spans:
        figure.add_shape(
            type="rect",
            x0=start,
            x1=end,
            y0=0,
            y1=1,
            xref="x",
            yref="paper",
            fillcolor=GATE_FILL,
            line_width=0,
            layer="below",
        )


def _trade_hover(
    trade: SniperTrade,
    bias_rows: Sequence[DailyBias],
    daily_days: set[date],
    zone: LabeledZone | None,
) -> str:
    session = trade.entry_ts.astimezone(UTC).date()
    active = bias_for_session(bias_rows, session) if bias_rows else None
    if active is not None and active.bias == trade.bias:
        bias_line = (
            f"日线偏向 {trade.bias}，来自已收完的 {active.day.isoformat()}，"
            "不是入场当天的日线收盘。"
        )
    elif active is not None:
        bias_line = (
            f"这笔记录的偏向是 {trade.bias}。"
            f"按图上这套参数重算，该 UTC 日得到 {active.bias}，两者不一致，不把重算结果当成这笔的依据。"
        )
    else:
        bias_line = f"这笔记录的偏向是 {trade.bias}。没有更早的日线可以标出它来自哪一天。"
    if session not in daily_days:
        bias_line += " 日线图没有这个 UTC 日的日K，没有把这笔成交补成日线。"
    if zone is None:
        zone_line = "没有从同一套订单块或公允价值缺口里对上这块入场，不画信号区。"
    else:
        zone_line = (
            f"信号区 {_ZONE_NAME[(zone.kind, zone.side)]} {_num(zone.low)}–{_num(zone.high)}，"
            f"形成于 {_stamp(zone.formed_ts)}。"
        )
    side = "多" if trade.side == "buy" else "空"
    return "\n".join(
        [
            f"{side} 顺序单仓 #{trade.entry_index}",
            f"入场 {_stamp(trade.entry_ts)}  {_num(trade.entry)}",
            f"出场 {_stamp(trade.exit_ts)}  {_num(trade.exit)}  {_REASON[trade.reason]} ({trade.reason})",
            f"止损 {_num(trade.stop)}",
            f"目标 {_num(trade.target)}",
            f"PnL {_num(trade.pnl)}",
            f"价格R {_r(trade.price_r)}",
            f"账户R {_r(trade.account_r)}",
            f"计划价格R {_num(trade.planned_price_r)}  计划账户R {_num(trade.planned_account_r)}",
            f"数量 {_num(trade.qty)}  杠杆 {_num(trade.leverage_used)}",
            bias_line,
            zone_line,
        ]
    )


def _notes_text(
    *,
    drawn: int,
    skipped: int,
    volume_shown: bool,
    gate_available: bool,
    spans: Sequence[tuple[datetime, datetime]],
    zones: Sequence[LabeledZone],
    extended: bool,
    has_daily: bool,
    start: datetime | None,
    end: datetime | None,
    limit: int | None,
) -> str:
    start_text = "不限起始" if start is None else _stamp(start)
    end_text = "不限结束" if end is None else _stamp(end)
    limit_text = "不限根数" if limit is None else f"最近 {limit} 根 4H"
    extra = ""
    if extended:
        extra = " 有出场落在这个窗口之后，K 线已补到出场；补上的 K 线没有再记入场。"
    lines = [
        "图例",
        "成交是顺序单仓，和 sniper-backtest 同一次 run_sniper。持仓期间的新回踩没有另画成成交。",
        (
            "4H 图上绿色上三角是多头入场，红色下三角是空头入场，叉是出场。"
            "实线连接同一笔的入场价和出场价。虚线是止损，点线是目标。"
            "悬停可以看平仓原因、PnL、价格 R 和账户 R。悬停里的钟点是 UTC。"
        ),
        f"时间窗 {start_text}（含）至 {end_text}（不含），{limit_text}。本图画出 {drawn} 笔。{extra}".rstrip(),
    ]
    if skipped:
        lines.append(
            f"另有 {skipped} 笔的入场或出场时间对不上 K 线下标，没有画，避免把入场和出场串到别的 K 线上。"
        )
    if has_daily:
        lines.append(
            "下图是回测用的 UTC 日线，不是把 4H 再合成一遍。"
            "圆点是这根日 K 收完后给下一会话的偏向，空心菱形是这个 UTC 日的 4H 已经生效的偏向，来自更早收完的日线。"
            "4H 的入场、止损、目标只画在上图，不标到日 K 的开盘或收盘上。"
        )
    else:
        lines.append("没有传入日线，不画日线偏向，也不把 4H 收成日 K。")
    if volume_shown:
        lines.append("成交量来自 K 线的 volume。有大于 0 的量才画。")
    else:
        lines.append("这些 K 线的 volume 都是 0，不画成交量。")
    if zones:
        lines.append(
            "色块是这笔入场对上的订单块或公允价值缺口，来自同一套 liquidity_zones 和 find_retest。"
            "对不上的笔不画信号区。"
        )
    else:
        lines.append("没有从同一套订单块或公允价值缺口里对上入场，不画信号区。")
    if not gate_available:
        lines.append("没有宏观序列，也没有 factor_macro_gate，不画 L0 GATE=0 阴影。")
    elif spans:
        lines.append(
            "灰色竖带是 L0 GATE=0，或该 UTC 日没有生效门。回测对这种日子不开新仓。关门不平掉已有仓。"
        )
    else:
        lines.append("已按回测同一套门检查。窗口里的 4H 都是 GATE=1，所以没有灰色竖带。")
    lines.append("拖拽、滚轮和左上角的时间按钮可以缩放时间轴。")
    return "\n".join(lines)


def _bias_color(bias: str) -> str:
    if bias == "long":
        return LONG_COLOR
    if bias == "short":
        return SHORT_COLOR
    return "#7f8c8d"


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _num(value: Decimal | float) -> str:
    if isinstance(value, float):
        text = f"{value:.8f}".rstrip("0").rstrip(".")
        return text or "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _r(value: float | None) -> str:
    if value is None:
        return "无"
    return f"{value:+.4f}"
