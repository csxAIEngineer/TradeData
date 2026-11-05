"""从公开日频拉 MacroGate v0.1 的四条序列。失败时改走 CSV，不在这里重试行情。"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timezone
from io import StringIO
from urllib.error import URLError
from urllib.request import Request, urlopen

from dataprice.strategy.sniper.macro_gate import MacroBook, MacroPoint

FRED_IDS = {
    "tips_10y": "DFII10",
    "hy_oas": "BAMLH0A0HYM2",
}
YAHOO_IDS = {
    "dxy": "DX-Y.NYB",
    "vix": "^VIX",
}
_USER_AGENT = "dataprice-macro-backfill/0.1"


def fetch_macro_book(timeout: float = 30.0) -> MacroBook:
    """10Y TIPS 和 HY OAS 走 FRED，DXY 和 VIX 走 Yahoo 日线。不需要密钥。"""
    try:
        points = {
            "tips_10y": tuple(_fred(FRED_IDS["tips_10y"], timeout)),
            "hy_oas": tuple(_fred(FRED_IDS["hy_oas"], timeout)),
            "dxy": tuple(_yahoo(YAHOO_IDS["dxy"], timeout)),
            "vix": tuple(_yahoo(YAHOO_IDS["vix"], timeout)),
        }
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(
            "拉不到 FRED / Yahoo 日频。把 series_id,obs_date,close 存成 CSV，"
            "再执行 uv run dataprice macro-backfill --csv <文件>。"
            f"原因: {exc}"
        ) from exc
    return MacroBook(**points)


def parse_fred_csv(series_id: str, text: str) -> list[MacroPoint]:
    """FRED graph CSV。小数点或空值是缺失，跳过。周末行拒绝交给 MacroPoint。"""
    reader = csv.DictReader(StringIO(text))
    if reader.fieldnames is None or series_id not in reader.fieldnames or "DATE" not in reader.fieldnames:
        raise ValueError(f"FRED CSV 不是 {series_id} 的日期和收盘")
    points: list[MacroPoint] = []
    for row in reader:
        raw = (row.get(series_id) or "").strip()
        if raw in {"", "."}:
            continue
        obs = date.fromisoformat(row["DATE"])
        if obs.weekday() >= 5:
            continue
        points.append(MacroPoint(obs, float(raw)))
    return points


def parse_yahoo_chart(payload: dict, symbol: str) -> list[MacroPoint]:
    """Yahoo chart v8 的日线。收盘为空的格子跳过。时间戳按 UTC 日期归日。"""
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        error = (payload.get("chart") or {}).get("error")
        raise ValueError(f"Yahoo 没有 {symbol} 的日线: {error}")
    block = result[0]
    stamps = block.get("timestamp") or []
    quotes = ((block.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quotes.get("close") or []
    points: list[MacroPoint] = []
    for stamp, close in zip(stamps, closes):
        if close is None:
            continue
        obs = datetime.fromtimestamp(int(stamp), tz=timezone.utc).date()
        if obs.weekday() >= 5:
            continue
        if points and points[-1].obs_date == obs:
            points[-1] = MacroPoint(obs, float(close))
            continue
        if points and obs < points[-1].obs_date:
            raise ValueError(f"Yahoo {symbol} 的日期倒序")
        points.append(MacroPoint(obs, float(close)))
    if not points:
        raise ValueError(f"Yahoo {symbol} 没有可用收盘")
    return points


def _fred(series_id: str, timeout: float) -> list[MacroPoint]:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    text = _get(url, timeout)
    return parse_fred_csv(series_id, text)


def _yahoo(symbol: str, timeout: float) -> list[MacroPoint]:
    from urllib.parse import quote

    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{quote(symbol, safe='')}?interval=1d&range=10y"
    )
    payload = json.loads(_get(url, timeout))
    return parse_yahoo_chart(payload, symbol)


def _get(url: str, timeout: float) -> str:
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")
