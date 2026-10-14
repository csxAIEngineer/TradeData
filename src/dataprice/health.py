"""检查三个存储能不能连通。"""

from __future__ import annotations

import logging

import psycopg

from dataprice.bootstrap import _clickhouse
from dataprice.config import Settings, get_settings
from dataprice.factors.store import XS_LIBRARY, FactorStore

log = logging.getLogger(__name__)


def health(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    errors: list[str] = []
    try:
        with psycopg.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.postgres_password,
            dbname=settings.postgres_db,
            connect_timeout=5,
        ) as conn:
            version = conn.execute("SHOW server_version").fetchone()
        log.info("PostgreSQL %s", version[0] if version else "ok")
    except Exception as exc:
        errors.append(f"PostgreSQL: {exc}")

    client = None
    try:
        client = _clickhouse(settings)
        version = client.command("SELECT version()")
        tables = client.query("SHOW TABLES").result_rows
        names = ", ".join(row[0] for row in tables) or "还没有表"
        log.info("ClickHouse %s，表: %s", version, names)
    except Exception as exc:
        errors.append(f"ClickHouse: {exc}")
    finally:
        if client is not None:
            client.close()

    try:
        store = FactorStore(settings)
        libraries = store.ensure_libraries()
        log.info("ArcticDB %s，库: %s", settings.arctic_connection_string(), ", ".join(libraries))
        if XS_LIBRARY not in libraries:
            errors.append("ArcticDB: factor_xs 没有创建")
    except Exception as exc:
        errors.append(f"ArcticDB: {exc}")

    if errors:
        for item in errors:
            log.error(item)
        raise SystemExit(1)
