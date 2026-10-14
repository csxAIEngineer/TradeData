"""把多语句 SQL 文件拆开。psycopg 和 clickhouse-client 的 Python 驱动一次只执行一条。"""

from __future__ import annotations

from pathlib import Path


def split_sql(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("--"):
            continue
        lines.append(line)
    statements: list[str] = []
    for part in "\n".join(lines).split(";"):
        statement = part.strip()
        if statement:
            statements.append(statement)
    return statements


def read_sql(path: Path) -> list[str]:
    return split_sql(path.read_text(encoding="utf-8"))
