"""OKX books5 长连接。

回调只把报价放进 BatchBuffer。另一个任务大约每秒调用 insert_quotes。
写入失败把批次放回缓冲再退避。单源超过 3 秒没有新报价只打日志。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from datetime import datetime

from websockets.asyncio.client import connect

from dataprice.buffer import BatchBuffer, BufferFull
from dataprice.config import Settings, get_settings
from dataprice.contracts import BookQuote
from dataprice.feed.okx import (
    APP_PING_INTERVAL_SEC,
    BACKOFF_MAX,
    BACKOFF_MIN,
    MAX_AGE_SEC,
    OKX_URL,
    OPEN_TIMEOUT_SEC,
    PING_INTERVAL_SEC,
    PING_TIMEOUT_SEC,
    SILENCE_TIMEOUT_SEC,
    QuoteLiveness,
    parse_okx_books,
    push_quote,
    subscribe_message,
)
from dataprice.market.repo import flush_quotes, market_session
from dataprice.market.validate import validate_quote
from dataprice.timeutil import UTC

log = logging.getLogger(__name__)

FLUSH_INTERVAL_SEC = 1.0
QUOTE_WATERMARK = 100_000


async def run_feed(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    buffer: BatchBuffer[BookQuote] = BatchBuffer(QUOTE_WATERMARK)
    health = QuoteLiveness()
    stop = asyncio.Event()
    log.info("开始订阅 OKX books5，写入 ClickHouse %s.quotes", settings.clickhouse_db)
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(run_okx(buffer, health), name="okx")
            group.create_task(flush_forever(buffer, settings, stop), name="flush")
            group.create_task(watch_quiet(health, stop), name="quiet")
    finally:
        stop.set()


async def run_okx(buffer: BatchBuffer[BookQuote], health: QuoteLiveness) -> None:
    """单源长连接：协议层 ping、应用层 ping、静默超时、退避重连。"""
    backoff = BACKOFF_MIN
    while True:
        try:
            async with connect(
                OKX_URL,
                ping_interval=PING_INTERVAL_SEC,
                ping_timeout=PING_TIMEOUT_SEC,
                open_timeout=OPEN_TIMEOUT_SEC,
                max_queue=4096,
            ) as ws:
                await ws.send(subscribe_message())
                log.info("[okx] 已连接")
                backoff = BACKOFF_MIN
                health.connected(time.monotonic())
                stopped = asyncio.Event()
                heartbeat = asyncio.create_task(_app_ping(ws, stopped))
                try:
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=SILENCE_TIMEOUT_SEC)
                        except TimeoutError:
                            log.warning("[okx] 静默超时，主动重连")
                            break
                        _ingest(raw, buffer, health)
                finally:
                    stopped.set()
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat
                    health.disconnected()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[okx] 连接异常: %s，%.1fs 后重连", exc, backoff)
        delay = backoff + random.uniform(0, backoff * 0.3)
        await asyncio.sleep(delay)
        backoff = min(backoff * 2, BACKOFF_MAX)


def _ingest(raw: str | bytes, buffer: BatchBuffer[BookQuote], health: QuoteLiveness) -> None:
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    now = datetime.now(UTC)
    try:
        quotes = parse_okx_books(text, now)
    except Exception as exc:
        log.debug("[okx] 解析失败: %s | %s", exc, text[:200])
        return
    arrived = time.monotonic()
    for quote in quotes:
        try:
            validate_quote(quote)
        except ValueError as exc:
            log.debug("[okx] 报价无效: %s", exc)
            continue
        try:
            accepted = push_quote(buffer, quote, now)
        except BufferFull:
            log.warning("[okx] 报价缓冲已满，丢弃 %s", quote.symbol)
            continue
        if accepted:
            health.mark(quote.symbol, arrived)
        else:
            log.debug("[okx] %s 报价已超过 %.0f 秒，不入缓冲", quote.symbol, MAX_AGE_SEC)


async def _app_ping(ws, stopped: asyncio.Event) -> None:
    while not stopped.is_set():
        try:
            await asyncio.wait_for(stopped.wait(), timeout=APP_PING_INTERVAL_SEC)
        except TimeoutError:
            pass
        else:
            return
        try:
            await ws.send("ping")
        except Exception:
            return


async def flush_forever(
    buffer: BatchBuffer[BookQuote],
    settings: Settings,
    stop: asyncio.Event,
) -> None:
    backoff = BACKOFF_MIN
    while not stop.is_set():
        try:
            written = await asyncio.to_thread(_flush_once, buffer, settings)
        except Exception as exc:
            log.warning("[okx] 买卖一价写入失败: %s，%.1fs 后重试", exc, backoff)
            delay = backoff + random.uniform(0, backoff * 0.3)
            if await _wait(stop, delay):
                return
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue
        backoff = BACKOFF_MIN
        if written:
            log.info("写入 %s 条买卖一价", written)
        if await _wait(stop, FLUSH_INTERVAL_SEC):
            return


def _flush_once(buffer: BatchBuffer[BookQuote], settings: Settings) -> int:
    if len(buffer) == 0:
        return 0
    with market_session(settings) as repo:
        return flush_quotes(buffer, repo)


async def watch_quiet(health: QuoteLiveness, stop: asyncio.Event) -> None:
    announced: set[str] = set()
    while not stop.is_set():
        if await _wait(stop, FLUSH_INTERVAL_SEC):
            return
        quiet = health.quiet_symbols(time.monotonic())
        current = set(quiet)
        for symbol in quiet:
            if symbol not in announced:
                log.warning("[okx] %s 超过 %.0f 秒没有新报价", symbol, MAX_AGE_SEC)
        announced = current


async def _wait(stop: asyncio.Event, timeout: float) -> bool:
    """等到 stop 或超时。返回 True 表示该停了。"""
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout)
    except TimeoutError:
        return False
    return True


def main() -> None:
    try:
        asyncio.run(run_feed())
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("行情订阅已停止")
