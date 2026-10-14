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
    assert "Asia/Shanghai" in clickhouse
    assert "DateTime64(9" in clickhouse
