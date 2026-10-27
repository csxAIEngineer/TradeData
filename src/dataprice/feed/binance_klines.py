"""Binance 现货 K 线。解析官方 vision CSV，并把两根 5 分钟合成一根 10 分钟。

月文件::

    https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/5m/BTCUSDT-5m-YYYY-MM.zip

日文件::

    https://data.binance.vision/data/spot/daily/klines/BTCUSDT/5m/BTCUSDT-5m-YYYY-MM-DD.zip

CSV 没有表头时也能读。有表头时第一列是 ``open_time``。开盘时间在 2025 年之后的
包里可能是微秒，更早的包是毫秒。成交额用 ``quote_volume``，记到 ``Bar.amount``。
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from datetime import datetime, timedelta

from dataprice.contracts import Bar
from dataprice.timeutil import UTC, ensure_aware

BINANCE_SPOT = "https://data.binance.vision/data/spot"
_MICROSECOND_CUTOFF = 10**14


def parse_spot_kline_zip(
    blob: bytes,
    *,
    symbol: str,
    freq: str,
    received_at: datetime,
    start: datetime,
    end: datetime,
) -> list[Bar]:
    """读一个 zip 里的 CSV。只保留 ``[start, end)`` 内的开盘时间。"""
    received_at = ensure_aware(received_at)
    start = ensure_aware(start)
    end = ensure_aware(end)
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ValueError("Binance 现货 K 线压缩包损坏") from exc
    names = [name for name in archive.namelist() if name.endswith(".csv")]
    if len(names) != 1:
        raise ValueError(f"压缩包里应有一个 CSV，实际 {names}")
    text = archive.read(names[0]).decode("utf-8")
    return parse_spot_kline_csv(
        text,
        symbol=symbol,
        freq=freq,
        received_at=received_at,
        start=start,
        end=end,
    )


def parse_spot_kline_csv(
    text: str,
    *,
    symbol: str,
    freq: str,
    received_at: datetime,
    start: datetime,
    end: datetime,
) -> list[Bar]:
    received_at = ensure_aware(received_at)
    start = ensure_aware(start)
    end = ensure_aware(end)
    rows: list[Bar] = []
    for line in text.splitlines():
        parsed = _parse_line(line, symbol=symbol, freq=freq, received_at=received_at)
        if parsed is None:
            continue
        if start <= parsed.exchange_ts < end:
            rows.append(parsed)
    return rows


def aggregate_10m(bars: Sequence[Bar]) -> list[Bar]:
    """两根 UTC 对齐的 5 分钟合成一根 10 分钟。缺任意一半就不生成这根。

    开盘价用前半根，收盘价用后半根，最高最低取两根的极值，成交量和成交额相加。
    不是 10 根 1 分钟。没有对应的 5 分钟时不补价格。
    """
    by_key: dict[tuple[str, datetime], Bar] = {}
    for bar in bars:
        if bar.freq != "5m":
            continue
        ts = bar.exchange_ts.astimezone(UTC).replace(tzinfo=UTC)
        if ts.second or ts.microsecond or ts.minute % 5:
            continue
        by_key[(bar.symbol, ts)] = bar
    out: list[Bar] = []
    for symbol, ts in sorted(by_key):
        if ts.minute % 10:
            continue
        second = by_key.get((symbol, ts + timedelta(minutes=5)))
        if second is None:
            continue
        first = by_key[(symbol, ts)]
        out.append(
            Bar(
                symbol=symbol,
                freq="10m",
                exchange_ts=ts,
                local_recv_ts=max(first.local_recv_ts, second.local_recv_ts),
                open=first.open,
                high=max(first.high, second.high),
                low=min(first.low, second.low),
                close=second.close,
                volume=first.volume + second.volume,
                amount=first.amount + second.amount,
            )
        )
    return out


def _parse_line(
    line: str,
    *,
    symbol: str,
    freq: str,
    received_at: datetime,
) -> Bar | None:
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 8 or not parts[0].isdigit():
        return None
    try:
        opened = int(parts[0])
        open_ = float(parts[1])
        high = float(parts[2])
        low = float(parts[3])
        close = float(parts[4])
        volume = float(parts[5])
        amount = float(parts[7])
    except (TypeError, ValueError):
        return None
    if opened < 0 or volume < 0 or amount < 0:
        return None
    if not (low <= open_ <= high and low <= close <= high):
        return None
    return Bar(
        symbol=symbol,
        freq=freq,
        exchange_ts=_exchange_ts(opened),
        local_recv_ts=received_at,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=amount,
    )


def _exchange_ts(raw: int) -> datetime:
    """vision 的开盘时间。大于 1e14 视为微秒，否则视为毫秒。"""
    if raw > _MICROSECOND_CUTOFF:
        seconds, rem = divmod(raw, 1_000_000)
        extra = timedelta(microseconds=rem)
    else:
        seconds, rem = divmod(raw, 1_000)
        extra = timedelta(milliseconds=rem)
    return datetime.fromtimestamp(seconds, tz=UTC) + extra
