"""把 Binance USD-M 30m 主动成交量写入 ClickHouse ``market.taker_flow``。

再拉一次（幂等，ReplacingMergeTree 按入库时间覆盖同一根）::

    uv run dataprice flow-backfill --symbol BTC-USDT --months 24
    make flow-backfill

OKX 公开接口没有覆盖本回测窗口的历史盘口或爆仓明细。这里用的是
Binance USD-M ``BTCUSDT`` 30 分钟 K 线里的主动买量，主动卖量由总量减去主动买量。
标的仍记成 ``BTC-USDT``，``source`` 是 ``binance-um``，方便和现货 K 线按开盘时间对齐。
不读 API key，也不改 ``.env``。
"""

from __future__ import annotations

import hashlib
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from dataprice.config import Settings
from dataprice.contracts import TakerFlow, require_name
from dataprice.feed.binance_flow import (
    BINANCE_UM_SOURCE,
    FLOW_FREQ,
    archive_requests,
    parse_binance_kline_zip,
)
from dataprice.market.repo import MarketRepository, market_session
from dataprice.timeutil import UTC, ensure_aware

log = logging.getLogger(__name__)

GetBytes = Callable[[str], bytes | None]
DEFAULT_MONTHS = 24
INSERT_CHUNK = 5_000


def backfill_taker_flow(
    *,
    symbol: str = "BTC-USDT",
    months: int = DEFAULT_MONTHS,
    now: datetime | None = None,
    settings: Settings | None = None,
    repo: MarketRepository | None = None,
    get_bytes: GetBytes | None = None,
    sleep_s: float = 0.05,
) -> int:
    """拉取并写入。返回写入的根数。"""
    symbol = require_name(symbol, "标的")
    inst_id = um_inst_id(symbol)
    if months < 1:
        raise ValueError("months 必须 >= 1")
    end = ensure_aware(now or datetime.now(UTC))
    start = end - timedelta(days=months * 30)
    fetch = get_bytes or default_get_bytes
    rows = fetch_binance_taker_flow(
        symbol=symbol,
        inst_id=inst_id,
        start=start,
        end=end,
        get_bytes=fetch,
        sleep_s=sleep_s,
        received_at=end,
    )
    if not rows:
        raise RuntimeError(f"{symbol} 没有取到主动成交量")
    count = _store(rows, repo=repo, settings=settings)
    log.info(
        "%s %s 主动成交写入 %s 根，%s → %s",
        symbol,
        BINANCE_UM_SOURCE,
        count,
        rows[0].exchange_ts.isoformat(),
        rows[-1].exchange_ts.isoformat(),
    )
    return count


def fetch_binance_taker_flow(
    *,
    symbol: str,
    inst_id: str,
    start: datetime,
    end: datetime,
    get_bytes: GetBytes,
    sleep_s: float = 0.05,
    received_at: datetime | None = None,
) -> list[TakerFlow]:
    start = ensure_aware(start)
    end = ensure_aware(end)
    received_at = ensure_aware(received_at or end)
    collected: list[TakerFlow] = []
    requests = archive_requests(inst_id, FLOW_FREQ, start, end)
    for index, (url, required) in enumerate(requests):
        blob = _zip_bytes(url, get_bytes, required=required)
        if blob is None:
            log.info("跳过尚未发布的 %s", url.rsplit("/", 1)[-1])
            continue
        collected.extend(
            parse_binance_kline_zip(
                blob,
                symbol=symbol,
                source=BINANCE_UM_SOURCE,
                freq=FLOW_FREQ,
                received_at=received_at,
                start=start,
                end=end,
            )
        )
        if sleep_s > 0 and index + 1 < len(requests):
            time.sleep(sleep_s)
    return _dedupe(collected)


def um_inst_id(symbol: str) -> str:
    if symbol != "BTC-USDT":
        raise ValueError("主动成交回补目前只支持 BTC-USDT")
    return "BTCUSDT"


def default_get_bytes(url: str, *, attempts: int = 5, timeout: float = 60.0) -> bytes | None:
    """GET 字节。404 返回 None。限流和短暂网络错误会退避重试。"""
    headers = {"User-Agent": "dataprice/0.1", "Accept": "*/*"}
    last: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
        time.sleep(min(2**attempt, 16))
    raise RuntimeError(f"下载失败: {url} ({last})")


def insert_flow(
    repo: MarketRepository,
    rows: Sequence[TakerFlow],
    *,
    chunk: int = INSERT_CHUNK,
) -> int:
    if chunk < 1:
        raise ValueError("chunk 必须 >= 1")
    total = 0
    for offset in range(0, len(rows), chunk):
        batch = list(rows[offset : offset + chunk])
        total += repo.insert_taker_flow(batch)
    return total


def _zip_bytes(url: str, get_bytes: GetBytes, *, required: bool) -> bytes | None:
    blob = get_bytes(url)
    if blob is None:
        if required:
            raise RuntimeError(f"缺少历史主动成交文件: {url}")
        return None
    _check_sha256(url, blob, get_bytes)
    return blob


def _check_sha256(url: str, blob: bytes, get_bytes: GetBytes) -> None:
    checksum = get_bytes(url + ".CHECKSUM")
    if not checksum:
        log.info("没有校验文件，仍使用 %s", url.rsplit("/", 1)[-1])
        return
    expected = _checksum_hex(checksum)
    if expected is None:
        raise ValueError(f"无法读取校验和: {url}.CHECKSUM")
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise ValueError(f"校验和不匹配: {url}")


def _checksum_hex(payload: bytes) -> str | None:
    text = payload.decode("utf-8", errors="replace")
    for token in text.replace("\n", " ").split():
        if len(token) == 64 and all(char in "0123456789abcdefABCDEF" for char in token):
            return token.lower()
    return None


def _store(
    rows: Sequence[TakerFlow],
    *,
    repo: MarketRepository | None,
    settings: Settings | None,
) -> int:
    if repo is not None:
        return insert_flow(repo, rows)
    with market_session(settings) as session:
        return insert_flow(session, rows)


def _dedupe(rows: Sequence[TakerFlow]) -> list[TakerFlow]:
    by_ts: dict[datetime, TakerFlow] = {}
    for row in rows:
        by_ts[row.exchange_ts] = row
    return [by_ts[key] for key in sorted(by_ts)]
