"""把 Binance 现货 ``BTCUSDT`` 5 分钟 K 线写入 ClickHouse，并合成 10 分钟。

再拉一次（幂等，ReplacingMergeTree 按入库时间覆盖同一根）::

    uv run dataprice binance-backfill --symbol BTCUSDT --months 36 --freq 5m

标的写成 ``BTCUSDT``，和 OKX 现货的 ``BTC-USDT`` 30m/60m 分开，不会覆盖那两档。
10 分钟只在两根 UTC 对齐的 5 分钟都在时生成。月文件或日文件 404 就跳过并记名字，
不补 K 线。校验和不匹配则整次失败，不把坏包写进库。不下载 1 分钟。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from dataprice.config import Settings
from dataprice.contracts import Bar, require_name
from dataprice.feed.binance_flow import archive_requests
from dataprice.feed.binance_klines import BINANCE_SPOT, aggregate_10m, parse_spot_kline_zip
from dataprice.feed.flow_history import _check_sha256, default_get_bytes
from dataprice.market.repo import MarketRepository, market_session
from dataprice.timeutil import UTC, ensure_aware

log = logging.getLogger(__name__)

GetBytes = Callable[[str], bytes | None]
DEFAULT_MONTHS = 36
INSERT_CHUNK = 5_000
SPOT_SYMBOL = "BTCUSDT"
SPOT_FREQ = "5m"


@dataclass(frozen=True)
class SpotBackfillResult:
    symbol: str
    bars_5m: int
    bars_10m: int
    first_open: datetime | None
    last_open: datetime | None
    skipped: tuple[str, ...]


def backfill_spot_klines(
    *,
    symbol: str = SPOT_SYMBOL,
    months: int = DEFAULT_MONTHS,
    freq: str = SPOT_FREQ,
    now: datetime | None = None,
    settings: Settings | None = None,
    repo: MarketRepository | None = None,
    get_bytes: GetBytes | None = None,
    sleep_s: float = 0.05,
) -> SpotBackfillResult:
    """拉取 5 分钟并写入，再写入能配齐的 10 分钟。返回根数和跳过的文件名。"""
    symbol = require_name(symbol, "标的")
    if symbol != SPOT_SYMBOL:
        raise ValueError("Binance 现货回补只写入 BTCUSDT，避免和 OKX 的 BTC-USDT 混在一起")
    if freq != SPOT_FREQ:
        raise ValueError("目前只下载 5m。10m 由两根 5m 合成，不单独下")
    if months < 1:
        raise ValueError("months 必须 >= 1")
    end = ensure_aware(now or datetime.now(UTC))
    start = end - timedelta(days=months * 30)
    fetch = get_bytes or default_get_bytes
    bars, skipped = fetch_spot_bars(
        symbol=symbol,
        start=start,
        end=end,
        get_bytes=fetch,
        sleep_s=sleep_s,
        received_at=end,
    )
    if not bars:
        missing = ", ".join(skipped) if skipped else "没有文件"
        raise RuntimeError(f"{symbol} 没有取到 5m K 线。跳过: {missing}")
    tens = aggregate_10m(bars)
    stored_5m = _store(bars, repo=repo, settings=settings)
    stored_10m = _store(tens, repo=repo, settings=settings) if tens else 0
    log.info(
        "%s 现货写入 5m %s 根、10m %s 根，%s → %s。跳过 %s 个缺失文件",
        symbol,
        stored_5m,
        stored_10m,
        bars[0].exchange_ts.isoformat(),
        bars[-1].exchange_ts.isoformat(),
        len(skipped),
    )
    if skipped:
        log.info("缺失文件: %s", ", ".join(skipped))
    return SpotBackfillResult(
        symbol=symbol,
        bars_5m=stored_5m,
        bars_10m=stored_10m,
        first_open=bars[0].exchange_ts,
        last_open=bars[-1].exchange_ts,
        skipped=skipped,
    )


def fetch_spot_bars(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    get_bytes: GetBytes,
    sleep_s: float = 0.05,
    received_at: datetime | None = None,
) -> tuple[list[Bar], tuple[str, ...]]:
    """下载现货 5 分钟。404 记入跳过名单，不中断其它月份。"""
    start = ensure_aware(start)
    end = ensure_aware(end)
    received_at = ensure_aware(received_at or end)
    collected: list[Bar] = []
    skipped: list[str] = []
    requests = archive_requests(symbol, SPOT_FREQ, start, end, root=BINANCE_SPOT)
    for index, (url, _required) in enumerate(requests):
        name = url.rsplit("/", 1)[-1]
        blob = get_bytes(url)
        if blob is None:
            skipped.append(name)
            log.info("跳过缺失的 %s", name)
            continue
        _check_sha256(url, blob, get_bytes)
        collected.extend(
            parse_spot_kline_zip(
                blob,
                symbol=symbol,
                freq=SPOT_FREQ,
                received_at=received_at,
                start=start,
                end=end,
            )
        )
        log.info("已读 %s (%s/%s)", name, index + 1, len(requests))
        if sleep_s > 0 and index + 1 < len(requests):
            time.sleep(sleep_s)
    return _dedupe(collected), tuple(skipped)


def insert_bars(
    repo: MarketRepository,
    rows: Sequence[Bar],
    *,
    chunk: int = INSERT_CHUNK,
) -> int:
    if chunk < 1:
        raise ValueError("chunk 必须 >= 1")
    total = 0
    for offset in range(0, len(rows), chunk):
        batch = list(rows[offset : offset + chunk])
        total += repo.insert_bars(batch)
    return total


def _store(
    rows: Sequence[Bar],
    *,
    repo: MarketRepository | None,
    settings: Settings | None,
) -> int:
    if repo is not None:
        return insert_bars(repo, rows)
    with market_session(settings) as session:
        return insert_bars(session, rows)


def _dedupe(rows: Sequence[Bar]) -> list[Bar]:
    by_ts: dict[datetime, Bar] = {}
    for row in rows:
        by_ts[row.exchange_ts] = row
    return [by_ts[key] for key in sorted(by_ts)]
