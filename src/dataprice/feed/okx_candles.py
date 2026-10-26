"""OKX 公共 K 线解析。不连接交易所，也不写数据库。

历史回补用两个公开接口，都不需要密钥：

- ``GET /api/v5/market/candles``：最近大约一个月
- ``GET /api/v5/market/history-candles``：更早的已完成 K 线

项目里的周期名是 ``30m`` 和 ``60m``。OKX 的 1 小时参数是 ``1H``，入库仍写 ``60m``。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from dataprice.contracts import BAR_FREQS, Bar
from dataprice.timeutil import UTC, ensure_aware

OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
OKX_HISTORY_URL = "https://www.okx.com/api/v5/market/history-candles"
OKX_SOURCE = "okx"

# 本仓库 freq -> OKX bar 参数。60m 是 1 小时。
OKX_BAR = {"30m": "30m", "60m": "1H"}


def okx_bar(freq: str) -> str:
    try:
        return OKX_BAR[freq]
    except KeyError as exc:
        raise ValueError(f"OKX K 线只回补 30m 和 60m，收到 {freq}") from exc


def parse_okx_candle_payload(
    payload: object,
    *,
    symbol: str,
    freq: str,
    received_at: datetime,
) -> list[Bar]:
    """把一页 OKX K 线转成 ``Bar``。未完成的 K 线（confirm != 1）丢掉。"""
    if freq not in BAR_FREQS:
        raise ValueError(f"不支持的周期: {freq}")
    received_at = ensure_aware(received_at)
    bars: list[Bar] = []
    for row in _rows(payload):
        bar = _bar_from_row(row, symbol=symbol, freq=freq, received_at=received_at)
        if bar is not None:
            bars.append(bar)
    return bars


def _rows(payload: object) -> list[object]:
    if not isinstance(payload, dict):
        raise ValueError("OKX 响应不是对象")
    if str(payload.get("code")) != "0":
        raise ValueError(f"OKX 错误 {payload.get('code')}: {payload.get('msg')}")
    data = payload.get("data") or []
    if not isinstance(data, list):
        raise ValueError("OKX data 不是列表")
    return data


def _bar_from_row(
    row: object,
    *,
    symbol: str,
    freq: str,
    received_at: datetime,
) -> Bar | None:
    if not isinstance(row, (list, tuple)) or len(row) < 9:
        return None
    # confirm=0 是未收盘的最后一根，不入库。
    if str(row[8]) != "1":
        return None
    try:
        millis = int(row[0])
        open_ = float(row[1])
        high = float(row[2])
        low = float(row[3])
        close = float(row[4])
        volume = float(row[5])
        amount = float(row[7])
    except (TypeError, ValueError):
        return None
    if millis < 0 or high < low or volume < 0 or amount < 0:
        return None
    if not (low <= open_ <= high and low <= close <= high):
        return None
    seconds, rem = divmod(millis, 1000)
    exchange_ts = datetime.fromtimestamp(seconds, tz=UTC)
    if rem:
        exchange_ts = exchange_ts + timedelta(milliseconds=rem)
    return Bar(
        symbol=symbol,
        freq=freq,
        exchange_ts=exchange_ts,
        local_recv_ts=received_at,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=amount,
    )
