-- 宏观日频和合成的 MacroGate。给一击必杀用，不改 001–003 的列。
-- macro_daily 是原始收盘。factor_macro_gate 是 v0.1 的分数和门，一行一个生效日。
-- 分区按观测日或生效日的月份。排序键带着版本，同一天重算会按入库时间盖住旧行。

CREATE TABLE IF NOT EXISTS market.macro_daily
(
    series_id       LowCardinality(String),
    obs_date        Date,
    close           Float64,
    source          LowCardinality(String),
    ingest_ts       DateTime64(9, 'UTC')
)
ENGINE = ReplacingMergeTree(ingest_ts)
PARTITION BY toYYYYMM(obs_date)
ORDER BY (series_id, obs_date);

CREATE TABLE IF NOT EXISTS market.factor_macro_gate
(
    version             LowCardinality(String),
    effective_date      Date,
    obs_date            Date,
    score               Nullable(Float64),
    gate                UInt8,
    level_dxy           Nullable(Float64),
    level_tips_10y      Nullable(Float64),
    level_hy_oas        Nullable(Float64),
    level_vix           Nullable(Float64),
    component_dxy       Nullable(Float64),
    component_tips_10y  Nullable(Float64),
    component_hy_oas    Nullable(Float64),
    component_vix       Nullable(Float64),
    ingest_ts           DateTime64(9, 'UTC')
)
ENGINE = ReplacingMergeTree(ingest_ts)
PARTITION BY toYYYYMM(effective_date)
ORDER BY (version, effective_date);
