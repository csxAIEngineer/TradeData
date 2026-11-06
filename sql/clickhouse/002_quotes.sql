-- 买卖一价。给实时盘口落库，按批写入。
-- 分区：交易所本地时区（Asia/Shanghai）的日期。
-- 排序键：symbol, source, exchange_ts, local_recv_ts。
-- price 是中间价，spread_bps 是买卖价差（基点）。
-- 不改 001 的列；这是新表。

CREATE TABLE IF NOT EXISTS market.quotes
(
    symbol          LowCardinality(String),
    source          LowCardinality(String),
    exchange_ts     DateTime64(9, 'UTC'),
    local_recv_ts   DateTime64(9, 'UTC'),
    ingest_ts       DateTime64(9, 'UTC'),
    bid             Float64,
    ask             Float64,
    price           Float64,
    spread_bps      Float64
)
ENGINE = ReplacingMergeTree(ingest_ts)
PARTITION BY toYYYYMMDD(toTimeZone(exchange_ts, 'Asia/Shanghai'))
ORDER BY (symbol, source, exchange_ts, local_recv_ts);
