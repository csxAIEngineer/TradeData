from dataprice.config import repo_root
from dataprice.sqlutil import read_sql


def test_sql_files_define_the_three_stores():
    root = repo_root()
    postgres = "\n".join(read_sql(root / "sql" / "postgres" / "001_ledger.sql"))
    clickhouse = "\n".join(read_sql(root / "sql" / "clickhouse" / "001_market.sql"))
    for table in (
        "ledger.accounts",
        "ledger.cash_journal",
        "ledger.positions",
        "ledger.risk_limits",
        "ledger.orders",
        "ledger.fills",
        "ledger.strategies",
        "ledger.app_users",
    ):
        assert table in postgres
    assert "exchange_ts_ns" in postgres
    assert "local_recv_ts_ns" in postgres
    assert "ingest_ts_ns" in postgres
    assert "market.bars" in clickhouse
    assert "market.ticks" in clickhouse
    assert "market.quotes" not in clickhouse
    assert "Asia/Shanghai" in clickhouse
    assert "DateTime64(9" in clickhouse


def test_clickhouse_migrations_run_in_filename_order():
    from dataprice.bootstrap import clickhouse_sql_files

    names = [path.name for path in clickhouse_sql_files()]
    assert names == ["001_market.sql", "002_quotes.sql"]
    quotes = "\n".join(read_sql(repo_root() / "sql" / "clickhouse" / "002_quotes.sql"))
    assert "CREATE TABLE IF NOT EXISTS market.quotes" in quotes
    assert "ReplacingMergeTree(ingest_ts)" in quotes
    assert "PARTITION BY toYYYYMMDD(toTimeZone(exchange_ts, 'Asia/Shanghai'))" in quotes
    assert "ORDER BY (symbol, source, exchange_ts, local_recv_ts)" in quotes
    for column in (
        "source          LowCardinality(String)",
        "bid             Float64",
        "ask             Float64",
        "price           Float64",
        "spread_bps      Float64",
    ):
        assert column in quotes
