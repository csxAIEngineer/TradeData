#!/usr/bin/env python3
"""
多源一手行情聚合网关 (BTC / ETH)

设计要点:
  - 直接连交易所撮合引擎推送流(WebSocket), 不经过任何第三方聚合 API
  - 多源并发 + 独立重连, 任一源故障不影响其他源
  - 中位数 + MAD 剔除离群源, 再按 24h 成交量加权合成
  - 陈旧检测: 单源超时即从权重池剔除, 存活源不足则标记 degraded

安装依赖:
    pip install websockets

运行:
    python crypto_feed_gateway.py

注意:
  - 中国大陆网络环境下 binance.com / coinbase / kraken 通常需要境外出口,
    请先在目标机器上逐源实测连通性
  - 生产环境请把 on_consolidated 回调换成写 Kafka / ClickHouse
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import statistics
import time
from dataclasses import dataclass
from typing import Callable, Optional

import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
)
log = logging.getLogger("feed")

SYMBOLS = ["BTC", "ETH"]

# 单源报价最长存活时间, 超过即视为陈旧并剔除
MAX_AGE_SEC = 3.0
# 连接静默超时: 连着但没数据, 主动重连
SILENCE_TIMEOUT_SEC = 30.0
# MAD 离群判定倍数
MAD_K = 5.0
# 重连退避
BACKOFF_MIN, BACKOFF_MAX = 1.0, 30.0


@dataclass
class Quote:
    source: str
    symbol: str
    bid: float
    ask: float
    recv_mono: float          # 本地单调钟, 用于判陈旧
    event_ts_ms: Optional[int]  # 交易所事件时间, 用于排序与延迟统计

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass
class Adapter:
    """描述一个数据源的连接方式与报文解析逻辑."""
    name: str
    weight: float          # 24h 成交量权重
    url: str
    subscribe: Optional[str]      # 连接后发送的订阅报文
    app_ping: Optional[str] = None  # 应用层心跳文本(OKX 需要)

    def parse(self, raw: str) -> list[Quote]:
        raise NotImplementedError


def _norm_symbol(raw: str) -> Optional[str]:
    """把各家交易对命名归一到 BTC / ETH."""
    s = raw.upper().replace("/", "-")
    for sym in SYMBOLS:
        if s.startswith(sym):
            return sym
    return None


class BinanceAdapter(Adapter):
    def parse(self, raw: str) -> list[Quote]:
        msg = json.loads(raw)
        if "stream" not in msg:
            return []
        d = msg["data"]
        sym = _norm_symbol(d.get("s", ""))
        if sym is None:
            return []
        now = time.monotonic()
        return [Quote(self.name, sym, float(d["b"]), float(d["a"]), now, d.get("E"))]


class OKXAdapter(Adapter):
    def parse(self, raw: str) -> list[Quote]:
        if raw.strip() == "pong":
            return []
        msg = json.loads(raw)
        if msg.get("event") == "error":
            log.warning("[okx] 订阅错误: %s", msg)
            return []
        if "data" not in msg:
            return []
        inst = msg.get("arg", {}).get("instId", "")
        sym = _norm_symbol(inst)
        if sym is None:
            return []
        now = time.monotonic()
        out = []
        for d in msg["data"]:
            bids, asks = d.get("bids") or [], d.get("asks") or []
            if not bids or not asks:
                continue
            out.append(
                Quote(self.name, sym, float(bids[0][0]), float(asks[0][0]),
                      now, int(d["ts"]) if d.get("ts") else None)
            )
        return out


class CoinbaseAdapter(Adapter):
    def parse(self, raw: str) -> list[Quote]:
        msg = json.loads(raw)
        if msg.get("type") != "ticker":
            return []
        sym = _norm_symbol(msg.get("product_id", ""))
        if sym is None:
            return []
        now = time.monotonic()
        return [Quote(self.name, sym, float(msg["best_bid"]), float(msg["best_ask"]), now, None)]


class KrakenAdapter(Adapter):
    def parse(self, raw: str) -> list[Quote]:
        msg = json.loads(raw)
        if msg.get("channel") != "ticker":
            return []
        now = time.monotonic()
        out = []
        for d in msg.get("data", []):
            sym = _norm_symbol(d.get("symbol", ""))
            if sym is None or d.get("bid") is None or d.get("ask") is None:
                continue
            out.append(Quote(self.name, sym, float(d["bid"]), float(d["ask"]), now, None))
        return out


def build_adapters() -> list[Adapter]:
    """按 24h 成交量给权重; 权重只影响合成, 不影响故障切换."""
    return [
        BinanceAdapter(
            name="binance", weight=1.0,
            url="wss://stream.binance.com:9443/stream?streams="
                "btcusdt@bookTicker/ethusdt@bookTicker",
            subscribe=None,
        ),
        OKXAdapter(
            name="okx", weight=0.7,
            url="wss://ws.okx.com:8443/ws/v5/public",
            subscribe=json.dumps({
                "op": "subscribe",
                "args": [{"channel": "books5", "instId": f"{s}-USDT"} for s in SYMBOLS],
            }),
            app_ping="ping",
        ),
        CoinbaseAdapter(
            name="coinbase", weight=0.5,
            url="wss://ws-feed.exchange.coinbase.com",
            subscribe=json.dumps({
                "type": "subscribe",
                "product_ids": [f"{s}-USD" for s in SYMBOLS],
                "channels": ["ticker"],
            }),
        ),
        KrakenAdapter(
            name="kraken", weight=0.3,
            url="wss://ws.kraken.com/v2",
            subscribe=json.dumps({
                "method": "subscribe",
                "params": {"channel": "ticker", "symbol": [f"{s}/USD" for s in SYMBOLS]},
            }),
        ),
    ]


class Aggregator:
    """维护每源最新报价, 输出仲裁后的合成价."""

    def __init__(self, weights: dict[str, float],
                 on_result: Callable[[dict], None]):
        self.weights = weights
        self.on_result = on_result
        self.latest: dict[str, dict[str, Quote]] = {n: {} for n in weights}

    def update(self, q: Quote) -> None:
        self.latest.setdefault(q.source, {})[q.symbol] = q

    def consolidate(self, symbol: str, now: float) -> dict:
        live = []
        for name, quotes in self.latest.items():
            q = quotes.get(symbol)
            if q is None:
                continue
            age = now - q.recv_mono
            if age > MAX_AGE_SEC:      # 陈旧源直接剔除
                continue
            live.append((name, q.mid, age))

        result = {
            "symbol": symbol,
            "ts": time.time(),
            "sources": [n for n, _, _ in live],
            "price": None,
            "degraded": True,
        }
        if not live:
            return result

        mids = [m for _, m, _ in live]
        med = statistics.median(mids)

        # 只有 3 个及以上源时才做离群剔除, 避免把真实行情误杀
        outliers: set[str] = set()
        if len(live) >= 3:
            mad = statistics.median([abs(m - med) for m in mids]) or 1e-9
            for name, m, _ in live:
                if abs(m - med) > MAD_K * mad:
                    outliers.add(name)
                    log.warning("[%s] 源 %s 偏离中位数 %.2f (中位 %.2f), 已剔除",
                                symbol, name, m, med)

        good = [(n, m) for n, m, _ in live if n not in outliers] or live[:1]
        wsum = sum(self.weights.get(n, 1.0) for n, _ in good)
        price = sum(m * self.weights.get(n, 1.0) for n, m in good) / wsum

        result["price"] = price
        result["spread_bps"] = (max(m for _, m in good) - min(m for _, m in good)) / price * 1e4
        result["outliers"] = sorted(outliers)
        result["degraded"] = len(good) < 2
        return result


async def run_source(ad: Adapter, agg: Aggregator) -> None:
    """单源长连接: 退避重连 + 静默超时 + 应用层心跳."""
    backoff = BACKOFF_MIN
    while True:
        try:
            async with websockets.connect(
                ad.url,
                ping_interval=20,     # 协议层心跳
                ping_timeout=15,
                open_timeout=10,
                max_queue=4096,
            ) as ws:
                if ad.subscribe:
                    await ws.send(ad.subscribe)
                log.info("[%s] 已连接", ad.name)
                backoff = BACKOFF_MIN

                last_msg_at = time.monotonic()
                stop = asyncio.Event()

                async def heartbeat():
                    while not stop.is_set():
                        await asyncio.sleep(25)
                        if ad.app_ping and not stop.is_set():
                            try:
                                await ws.send(ad.app_ping)
                            except Exception:
                                return

                hb = asyncio.create_task(heartbeat()) if ad.app_ping else None
                try:
                    while True:
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=SILENCE_TIMEOUT_SEC
                            )
                        except asyncio.TimeoutError:
                            log.warning("[%s] 静默超时, 主动重连", ad.name)
                            break
                        last_msg_at = time.monotonic()
                        try:
                            for q in ad.parse(raw):
                                agg.update(q)
                        except Exception as exc:
                            log.debug("[%s] 解析失败: %s | %s", ad.name, exc, raw[:200])
                finally:
                    stop.set()
                    if hb:
                        hb.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[%s] 连接异常: %s, %.1fs 后重连", ad.name, exc, backoff)

        await asyncio.sleep(backoff + random.uniform(0, backoff * 0.3))
        backoff = min(backoff * 2, BACKOFF_MAX)


async def main() -> None:
    adapters = build_adapters()
    weights = {a.name: a.weight for a in adapters}

    def on_result(r: dict) -> None:
        if r["price"] is None:
            log.warning("%s: 无可用源!", r["symbol"])
            return
        flag = "DEGRADED " if r["degraded"] else ""
        log.info(
            "%s%s = %.2f | 源: %s | 离散 %.1f bps%s",
            flag, r["symbol"], r["price"], ",".join(r["sources"]),
            r["spread_bps"],
            f" | 剔除: {r['outliers']}" if r["outliers"] else "",
        )
        # 生产环境: 在此写 Kafka / ClickHouse / Redis

    agg = Aggregator(weights, on_result)

    async def ticker():
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            for sym in SYMBOLS:
                agg.on_result(agg.consolidate(sym, now))

    tasks = [asyncio.create_task(run_source(a, agg)) for a in adapters]
    tasks.append(asyncio.create_task(ticker()))
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
