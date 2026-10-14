-- L2 历史行情。给回测扫描用，按批写入。
-- 分区：交易所本地时区（Asia/Shanghai）的月份。排序键：freq, symbol, exchange_ts（时间 × 标的）。
-- 因子横截面不放这里，放 ArcticDB。
-- 已发布的 001 不要改列；后续变更另加迁移文件。

CREATE TABLE IF NOT EXISTS market.bars
(
    symbol          LowCardinality(String),
    freq            LowCardinality(String),
    exchange_ts     DateTime64(9, 'UTC'),
    local_recv_ts   DateTime64(9, 'UTC'),
    ingest_ts       DateTime64(9, 'UTC'),
    open            Float64,
    high            Float64,
    low             Float64,
    close           Float64,
    volume          Float64,
    amount          Float64
)
ENGINE = ReplacingMergeTree(ingest_ts)
PARTITION BY toYYYYMM(toTimeZone(exchange_ts, 'Asia/Shanghai'))
ORDER BY (freq, symbol, exchange_ts);

CREATE TABLE IF NOT EXISTS market.ticks
(
    symbol          LowCardinality(String),
    trade_id        String,
    exchange_ts     DateTime64(9, 'UTC'),
    local_recv_ts   DateTime64(9, 'UTC'),
    ingest_ts       DateTime64(9, 'UTC'),
    price           Float64,
    volume          Float64,
    side            LowCardinality(String)
)
ENGINE = ReplacingMergeTree(ingest_ts)
PARTITION BY toYYYYMMDD(toTimeZone(exchange_ts, 'Asia/Shanghai'))
ORDER BY (symbol, exchange_ts, trade_id)
TTL toDateTime(exchange_ts) + toIntervalYear(3);

CREATE TABLE IF NOT EXISTS market.schema_migrations
(
    version     String,
    applied_at  DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(applied_at)
ORDER BY version;
