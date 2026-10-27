"""Binance USD-M 月度/日度 K 线里的主动成交量。解析不访问网络。

公开压缩包没有表头时也能读。有表头时第一列是 ``open_time``。
主动买量是 ``taker_buy_volume``，主动卖量是总成交量减去主动买量。
这是合约成交拆分，不是 OKX 现货盘口，也不是爆仓单。
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta

from dataprice.contracts import TakerFlow
from dataprice.timeutil import UTC, ensure_aware

BINANCE_UM_SOURCE = "binance-um"
BINANCE_VISION = "https://data.binance.vision/data/futures/um"
FLOW_FREQ = "30m"


def vision_kline_url(
    inst_id: str,
    freq: str,
    day: datetime,
    *,
    monthly: bool,
    root: str = BINANCE_VISION,
) -> str:
    stamp = ensure_aware(day).astimezone(UTC)
    if monthly:
        name = f"{inst_id}-{freq}-{stamp:%Y-%m}.zip"
        return f"{root}/monthly/klines/{inst_id}/{freq}/{name}"
    name = f"{inst_id}-{freq}-{stamp:%Y-%m-%d}.zip"
    return f"{root}/daily/klines/{inst_id}/{freq}/{name}"


def archive_requests(
    inst_id: str,
    freq: str,
    start: datetime,
    end: datetime,
    *,
    root: str = BINANCE_VISION,
) -> list[tuple[str, bool]]:
    """返回 ``(url, required)``。整月能盖住的用月文件；尾部未结束的月用日文件。

    日文件在 ``end`` 当天和前一天可以没有（包通常要等该 UTC 日结束才发布），所以不是必选。
    """
    start = ensure_aware(start).astimezone(UTC)
    end = ensure_aware(end).astimezone(UTC)
    if end <= start:
        raise ValueError("end 必须晚于 start")
    requests: list[tuple[str, bool]] = []
    month = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while month < end:
        nxt = _next_month(month)
        if nxt <= end:
            requests.append((vision_kline_url(inst_id, freq, month, monthly=True, root=root), True))
        else:
            day = max(month, start.replace(hour=0, minute=0, second=0, microsecond=0))
            last = end.date()
            # 日文件通常在该 UTC 日结束后才发布，运行当天和前一天都可能还没有。
            published_through = end.date() - timedelta(days=1)
            while day.date() <= last and day < end:
                required = day.date() < published_through
                requests.append(
                    (vision_kline_url(inst_id, freq, day, monthly=False, root=root), required)
                )
                day = day + timedelta(days=1)
        month = nxt
    return requests


def parse_binance_kline_zip(
    blob: bytes,
    *,
    symbol: str,
    source: str,
    freq: str,
    received_at: datetime,
    start: datetime,
    end: datetime,
) -> list[TakerFlow]:
    """读一个 zip 里的 CSV。只保留 ``[start, end)`` 内的开盘时间。"""
    received_at = ensure_aware(received_at)
    start = ensure_aware(start)
    end = ensure_aware(end)
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ValueError("Binance K 线压缩包损坏") from exc
    names = [name for name in archive.namelist() if name.endswith(".csv")]
    if len(names) != 1:
        raise ValueError(f"压缩包里应有一个 CSV，实际 {names}")
    text = archive.read(names[0]).decode("utf-8")
    return parse_binance_kline_csv(
        text,
        symbol=symbol,
        source=source,
        freq=freq,
        received_at=received_at,
        start=start,
        end=end,
    )


def parse_binance_kline_csv(
    text: str,
    *,
    symbol: str,
    source: str,
    freq: str,
    received_at: datetime,
    start: datetime,
    end: datetime,
) -> list[TakerFlow]:
    received_at = ensure_aware(received_at)
    start = ensure_aware(start)
    end = ensure_aware(end)
    rows: list[TakerFlow] = []
    for line in text.splitlines():
        parsed = _parse_line(
            line,
            symbol=symbol,
            source=source,
            freq=freq,
            received_at=received_at,
        )
        if parsed is None:
            continue
        if start <= parsed.exchange_ts < end:
            rows.append(parsed)
    return rows


def _parse_line(
    line: str,
    *,
    symbol: str,
    source: str,
    freq: str,
    received_at: datetime,
) -> TakerFlow | None:
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 11:
        return None
    if not parts[0].isdigit():
        return None
    try:
        millis = int(parts[0])
        volume = float(parts[5])
        trade_count = int(float(parts[8]))
        buy = float(parts[9])
    except (TypeError, ValueError):
        return None
    if millis < 0 or volume < 0 or buy < 0 or trade_count < 0:
        return None
    sell = volume - buy
    if sell < 0:
        if sell > -1e-6:
            sell = 0.0
        else:
            return None
    seconds, rem = divmod(millis, 1000)
    exchange_ts = datetime.fromtimestamp(seconds, tz=UTC) + timedelta(milliseconds=rem)
    return TakerFlow(
        symbol=symbol,
        source=source,
        freq=freq,
        exchange_ts=exchange_ts,
        local_recv_ts=received_at,
        buy_volume=buy,
        sell_volume=sell,
        volume=volume,
        trade_count=trade_count,
    )


def _next_month(month: datetime) -> datetime:
    if month.month == 12:
        return month.replace(year=month.year + 1, month=1)
    return month.replace(month=month.month + 1)
