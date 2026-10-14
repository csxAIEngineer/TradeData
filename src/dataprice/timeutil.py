"""三套时钟：交易所时间、本地接收时间、入库时间。精度用纳秒整数。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
_NS = 1_000_000_000


def clock_ns() -> int:
    """本机墙钟，Unix 纳秒。用于 local_recv_ts 和 ingest_ts。"""
    return time.time_ns()


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("时间戳必须带时区。交易所时间、接收时间、入库时间要分开记")
    return value


def to_ns(value: datetime) -> int:
    """带时区的时间转 Unix 纳秒。pandas.Timestamp 保留纳秒，datetime 只到微秒。"""
    ensure_aware(value)
    if type(value).__module__.startswith("pandas"):
        return int(value.value)  # type: ignore[attr-defined]
    utc = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc - epoch
    return (
        delta.days * 86_400 * _NS
        + delta.seconds * _NS
        + delta.microseconds * 1_000
    )


def from_ns(ns: int) -> datetime:
    """纳秒转 UTC datetime。亚微秒部分会被截掉，展示用，不作为存储格式。"""
    seconds, rem = divmod(int(ns), _NS)
    base = datetime.fromtimestamp(seconds, tz=UTC)
    return base + timedelta(microseconds=rem // 1_000)
