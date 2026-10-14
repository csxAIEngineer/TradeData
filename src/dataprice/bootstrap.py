"""创建库表和 ArcticDB library。"""

from __future__ import annotations

import logging
import re
import time

import clickhouse_connect
import psycopg

from dataprice.config import Settings, get_settings, repo_root
from dataprice.factors.store import FactorStore
from dataprice.sqlutil import read_sql

log = logging.getLogger(__name__)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def bootstrap(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    wait_postgres(settings)
    apply_postgres(settings)
    wait_clickhouse(settings)
    apply_clickhouse(settings)
    store = FactorStore(settings)
    libraries = store.ensure_libraries()
    log.info("ArcticDB 已就绪: %s (%s)", ", ".join(libraries), settings.arctic_connection_string())


def apply_postgres(settings: Settings) -> None:
    path = repo_root() / "sql" / "postgres" / "001_ledger.sql"
    statements = read_sql(path)
    with psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password,
        dbname=settings.postgres_db,
        connect_timeout=5,
    ) as conn:
        for statement in statements:
            try:
                conn.execute(statement)
            except Exception:
                log.error("PostgreSQL 语句失败: %s", statement[:240])
                raise
    log.info("PostgreSQL 账本结构已应用 (%s 条语句)", len(statements))


def apply_clickhouse(settings: Settings) -> None:
    database = _ident(settings.clickhouse_db)
    admin = _clickhouse(settings, database="default")
    try:
        admin.command(f"CREATE DATABASE IF NOT EXISTS {database}")
    finally:
        admin.close()
    # HTTP 连接不保留 USE，建表语句写全限定名，后续查询走目标库。
    client = _clickhouse(settings, database=database)
    try:
        path = repo_root() / "sql" / "clickhouse" / "001_market.sql"
        statements = read_sql(path)
        for statement in statements:
            qualified = statement.replace("market.", f"{database}.")
            try:
                client.command(qualified)
            except Exception:
                log.error("ClickHouse 语句失败: %s", qualified[:240])
                raise
        found = client.query(
            "SELECT count() FROM schema_migrations WHERE version = {version:String}",
            parameters={"version": "001_market"},
        ).result_rows[0][0]
        if not found:
            client.command(
                "INSERT INTO schema_migrations (version) VALUES ({version:String})",
                parameters={"version": "001_market"},
            )
    finally:
        client.close()
    log.info("ClickHouse 行情库已应用")


def wait_postgres(settings: Settings, timeout_s: float = 60) -> None:
    _wait("PostgreSQL", timeout_s, lambda: _ping_postgres(settings))


def wait_clickhouse(settings: Settings, timeout_s: float = 60) -> None:
    _wait("ClickHouse", timeout_s, lambda: _ping_clickhouse(settings))


def _ping_postgres(settings: Settings) -> None:
    with psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password,
        dbname=settings.postgres_db,
        connect_timeout=3,
    ) as conn:
        conn.execute("SELECT 1")


def _ping_clickhouse(settings: Settings) -> None:
    client = _clickhouse(settings)
    try:
        client.command("SELECT 1")
    finally:
        client.close()


def _ident(name: str) -> str:
    if _IDENT.fullmatch(name) is None:
        raise ValueError(f"非法数据库名: {name}")
    return name


def _clickhouse(settings: Settings, database: str | None = None):
    return clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_http_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=database or settings.clickhouse_db,
    )


def _wait(name: str, timeout_s: float, probe) -> None:
    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            probe()
            log.info("%s 已连接", name)
            return
        except Exception as exc:
            last = exc
            time.sleep(1)
    raise TimeoutError(f"{name} 在 {timeout_s:.0f}s 内没有就绪: {last}") from last
