"""把 OKX 历史 K 线写入 ClickHouse ``market.bars``。

再拉一次（幂等，ReplacingMergeTree 按入库时间覆盖同一根）::

    uv run dataprice backfill --symbol BTC-USDT --months 24
    make backfill

默认标的 ``BTC-USDT``，周期 ``30m`` 和 ``60m``（60m = 1 小时）。
公开接口，不读 API key，也不改 ``.env``。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from dataprice.config import Settings
from dataprice.contracts import Bar, require_name
from dataprice.feed.okx_candles import (
    OKX_CANDLES_URL,
    OKX_HISTORY_URL,
    okx_bar,
    parse_okx_candle_payload,
)
from dataprice.market.repo import MarketRepository, market_session
from dataprice.timeutil import UTC, ensure_aware

log = logging.getLogger(__name__)

GetJson = Callable[[str], dict]
DEFAULT_MONTHS = 24
INSERT_CHUNK = 5_000
# 最近接口单页上限 300，历史接口单页上限 100。
RECENT_LIMIT = 300
HISTORY_LIMIT = 100
MAX_PAGES = 5_000


def backfill(
    *,
    symbol: str = "BTC-USDT",
    freqs: Sequence[str] = ("30m", "60m"),
    months: int = DEFAULT_MONTHS,
    now: datetime | None = None,
    settings: Settings | None = None,
    repo: MarketRepository | None = None,
    get_json: GetJson | None = None,
    sleep_s: float = 0.08,
) -> dict[str, int]:
    """拉取并写入。返回每个周期写入的根数。"""
    symbol = require_name(symbol, "标的")
    if months < 1:
        raise ValueError("months 必须 >= 1")
    selected = _freqs(freqs)
    end = ensure_aware(now or datetime.now(UTC))
    start = end - timedelta(days=months * 30)
    fetch = get_json or default_get_json
    written: dict[str, int] = {}
    for freq in selected:
        bars = fetch_okx_bars(
            symbol,
            freq,
            start,
            end,
            get_json=fetch,
            sleep_s=sleep_s,
            received_at=end,
        )
        if not bars:
            raise RuntimeError(f"{symbol} {freq} 没有取到已完成的 K 线")
        count = _store(bars, repo=repo, settings=settings)
        written[freq] = count
        log.info(
            "%s %s 写入 %s 根，%s → %s",
            symbol,
            freq,
            count,
            bars[0].exchange_ts.isoformat(),
            bars[-1].exchange_ts.isoformat(),
        )
    return written


def fetch_okx_bars(
    symbol: str,
    freq: str,
    start: datetime,
    end: datetime,
    *,
    get_json: GetJson,
    sleep_s: float = 0.08,
    received_at: datetime | None = None,
) -> list[Bar]:
    """按时间升序返回 ``[start, end)`` 内已完成的 K 线。"""
    start = ensure_aware(start)
    end = ensure_aware(end)
    if end <= start:
        raise ValueError("end 必须晚于 start")
    received_at = ensure_aware(received_at or end)
    bar = okx_bar(freq)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    rows = _paginate(
        OKX_CANDLES_URL,
        symbol=symbol,
        okx_bar_name=bar,
        start_ms=start_ms,
        end_ms=end_ms,
        limit=RECENT_LIMIT,
        get_json=get_json,
        sleep_s=sleep_s,
    )
    oldest = _oldest_ms(rows)
    history_after = oldest if oldest is not None else end_ms
    if history_after > start_ms:
        rows.extend(
            _paginate(
                OKX_HISTORY_URL,
                symbol=symbol,
                okx_bar_name=bar,
                start_ms=start_ms,
                end_ms=history_after,
                limit=HISTORY_LIMIT,
                get_json=get_json,
                sleep_s=sleep_s,
            )
        )
    payload = {"code": "0", "data": rows, "msg": ""}
    bars = parse_okx_candle_payload(
        payload, symbol=symbol, freq=freq, received_at=received_at
    )
    kept = [bar_ for bar_ in bars if start <= bar_.exchange_ts < end]
    return _dedupe(kept)


def default_get_json(url: str, *, attempts: int = 6, timeout: float = 20.0) -> dict:
    """GET JSON。限流和短暂网络错误会退避重试。"""
    headers = {"User-Agent": "dataprice/0.1", "Accept": "application/json"}
    last: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(min(2**attempt, 16))
            continue
        if not isinstance(payload, dict):
            raise ValueError("OKX 响应不是对象")
        if str(payload.get("code")) == "50011":
            last = RuntimeError(str(payload.get("msg")))
            time.sleep(min(2**attempt, 16))
            continue
        return payload
    raise RuntimeError(f"OKX 请求失败: {url} ({last})")


def insert_history(
    repo: MarketRepository,
    bars: Sequence[Bar],
    *,
    chunk: int = INSERT_CHUNK,
) -> int:
    if chunk < 1:
        raise ValueError("chunk 必须 >= 1")
    total = 0
    for offset in range(0, len(bars), chunk):
        batch = list(bars[offset : offset + chunk])
        total += repo.insert_bars(batch)
    return total


def _store(
    bars: Sequence[Bar],
    *,
    repo: MarketRepository | None,
    settings: Settings | None,
) -> int:
    if repo is not None:
        return insert_history(repo, bars)
    with market_session(settings) as session:
        return insert_history(session, bars)


def _paginate(
    endpoint: str,
    *,
    symbol: str,
    okx_bar_name: str,
    start_ms: int,
    end_ms: int,
    limit: int,
    get_json: GetJson,
    sleep_s: float,
) -> list[object]:
    """OKX ``after`` 表示「早于这个毫秒」。从 end_ms 往更早翻页。"""
    collected: list[object] = []
    after: int | None = end_ms
    for page in range(MAX_PAGES):
        url = _url(endpoint, symbol, okx_bar_name, limit, after)
        payload = get_json(url)
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            code = payload.get("code") if isinstance(payload, dict) else None
            msg = payload.get("msg") if isinstance(payload, dict) else payload
            raise ValueError(f"OKX 错误 {code}: {msg}")
        data = payload.get("data") or []
        if not isinstance(data, list) or not data:
            break
        collected.extend(data)
        oldest = _oldest_ms(data)
        if page == 0 or (page + 1) % 25 == 0:
            log.info(
                "%s %s 已取 %s 页，最早 %s",
                endpoint.rsplit("/", 1)[-1],
                okx_bar_name,
                page + 1,
                oldest,
            )
        if oldest is None or oldest <= start_ms:
            break
        if after is not None and oldest >= after:
            break
        after = oldest
        if sleep_s > 0 and page + 1 < MAX_PAGES:
            time.sleep(sleep_s)
    else:
        raise RuntimeError(f"OKX 分页超过 {MAX_PAGES} 页: {endpoint}")
    return collected


def _url(endpoint: str, symbol: str, okx_bar_name: str, limit: int, after: int | None) -> str:
    params = {
        "instId": symbol,
        "bar": okx_bar_name,
        "limit": str(limit),
    }
    if after is not None:
        params["after"] = str(after)
    return endpoint + "?" + urllib.parse.urlencode(params)


def _oldest_ms(rows: Sequence[object]) -> int | None:
    stamps: list[int] = []
    for row in rows:
        if isinstance(row, (list, tuple)) and row:
            try:
                stamps.append(int(row[0]))
            except (TypeError, ValueError):
                continue
    if not stamps:
        return None
    return min(stamps)


def _dedupe(bars: Sequence[Bar]) -> list[Bar]:
    by_ts: dict[datetime, Bar] = {}
    for bar in bars:
        by_ts[bar.exchange_ts] = bar
    return [by_ts[key] for key in sorted(by_ts)]


def _freqs(freqs: Sequence[str]) -> tuple[str, ...]:
    if not freqs:
        raise ValueError("至少要一个周期")
    selected: list[str] = []
    for freq in freqs:
        if freq not in selected:
            if freq not in ("30m", "60m"):
                raise ValueError(f"只支持 30m 和 60m，收到 {freq}")
            selected.append(freq)
    return tuple(selected)
