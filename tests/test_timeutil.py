from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd

from dataprice.timeutil import clock_ns, ensure_aware, to_ns


def test_to_ns_keeps_pandas_nanoseconds():
    stamp = pd.Timestamp("2024-01-02 01:30:00.000000001", tz="UTC")
    assert to_ns(stamp) == int(stamp.value)


def test_to_ns_rejects_naive_datetime():
    try:
        ensure_aware(datetime(2024, 1, 2))
    except ValueError:
        return
    raise AssertionError("naive timestamp should be rejected")


def test_clock_ns_is_current():
    before = datetime.now(timezone.utc)
    ns = clock_ns()
    after = datetime.now(timezone.utc)
    assert int(before.timestamp() * 1_000_000_000) - 1_000_000_000 <= ns
    assert ns <= int(after.timestamp() * 1_000_000_000) + 1_000_000_000
