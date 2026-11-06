-- 主动买/卖成交量。给回测里的流动性确认用，按批写入。
-- 不是盘口，也不是爆仓明细。exchange_ts 是这根 K 线的开盘时间。
-- 分区和 K 线一样，按 Asia/Shanghai 的月份。
-- 不改 001、002 的列；这是新表。

CREATE TABLE IF NOT EXISTS market.taker_flow
(
    symbol          LowCardinality(String),
    source          LowCardinality(String),
    freq            LowCardinality(String),
    exchange_ts     DateTime64(9, 'UTC'),
    local_recv_ts   DateTime64(9, 'UTC'),
    ingest_ts       DateTime64(9, 'UTC'),
    buy_volume      Float64,
    sell_volume     Float64,
    volume          Float64,
    trade_count     UInt32
)
ENGINE = ReplacingMergeTree(ingest_ts)
PARTITION BY toYYYYMM(toTimeZone(exchange_ts, 'Asia/Shanghai'))
ORDER BY (symbol, source, freq, exchange_ts);
