import pandas as pd
import pytest

from dataprice.factors.frame import slice_as_of, validate_panel


def _panel() -> pd.DataFrame:
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2024-01-02", tz="Asia/Shanghai"),
            pd.Timestamp("2024-01-03", tz="Asia/Shanghai"),
        ]
    )
    return pd.DataFrame({"000001.SZ": [1.0, 1.2], "000002.SZ": [2.0, 2.2]}, index=index)


def test_panel_requires_aware_sorted_index():
    frame = validate_panel(_panel())
    assert frame.index.name == "exchange_ts"
    naive = _panel()
    naive.index = naive.index.tz_localize(None)
    with pytest.raises(ValueError, match="时区"):
        validate_panel(naive)
    flipped = _panel().iloc[::-1]
    with pytest.raises(ValueError, match="升序"):
        validate_panel(flipped)


def test_slice_drops_rows_after_decision():
    frame = validate_panel(_panel())
    visible = slice_as_of(frame, pd.Timestamp("2024-01-02 15:00", tz="Asia/Shanghai"))
    assert list(visible.index) == [pd.Timestamp("2024-01-02", tz="Asia/Shanghai")]
