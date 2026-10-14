"""ArcticDB 因子库。

factor_xs：一个因子一个 symbol，列是标的，服务横截面。
factor_ts：一个标的一个 symbol，列是因子，服务时序。

每次 write 返回新版本号。版本号只增不减，删掉 symbol 之后也不会从 0 重新计。
回测要保存这个版本号，或用写入时刻作为 as_of，才能读到当时的因子。
旧版本不会被覆盖。不要打开 prune_previous_versions。
ArcticDB 是进程内库，本机数据在 LMDB；生产部署注意 BSL 许可证。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from dataprice.config import Settings, get_settings
from dataprice.contracts import require_name
from dataprice.factors.frame import slice_as_of, validate_panel
from dataprice.timeutil import clock_ns

XS_LIBRARY = "factor_xs"
TS_LIBRARY = "factor_ts"


@dataclass(frozen=True)
class FactorSnapshot:
    symbol: str
    layout: str
    frame: pd.DataFrame
    version: int


class FactorStore:
    def __init__(self, settings: Settings | None = None) -> None:
        from arcticdb import Arctic, LibraryOptions

        self._settings = settings or get_settings()
        self._arctic = Arctic(self._settings.arctic_connection_string())
        self._options = LibraryOptions(dynamic_schema=True, dedup=True)
        self._libs: dict = {}

    def ensure_libraries(self) -> list[str]:
        names = [XS_LIBRARY, TS_LIBRARY]
        for name in names:
            self._library(name)
        return names

    def write_cross_section(self, factor: str, frame: pd.DataFrame) -> int:
        return self._write(XS_LIBRARY, factor, frame, "cross_section")

    def write_symbol_factors(self, symbol: str, frame: pd.DataFrame) -> int:
        return self._write(TS_LIBRARY, symbol, frame, "time_series")

    def append_cross_section(self, factor: str, frame: pd.DataFrame) -> int:
        return self._append(XS_LIBRARY, factor, frame, "cross_section")

    def append_symbol_factors(self, symbol: str, frame: pd.DataFrame) -> int:
        return self._append(TS_LIBRARY, symbol, frame, "time_series")

    def read_cross_section(
        self,
        factor: str,
        *,
        as_of: datetime | int | str | None = None,
        decision_ts: datetime | None = None,
    ) -> FactorSnapshot:
        return self._read(XS_LIBRARY, factor, "cross_section", as_of, decision_ts)

    def read_symbol_factors(
        self,
        symbol: str,
        *,
        as_of: datetime | int | str | None = None,
        decision_ts: datetime | None = None,
    ) -> FactorSnapshot:
        return self._read(TS_LIBRARY, symbol, "time_series", as_of, decision_ts)

    def delete_cross_section(self, factor: str) -> None:
        self._delete(XS_LIBRARY, factor)

    def delete_symbol_factors(self, symbol: str) -> None:
        self._delete(TS_LIBRARY, symbol)

    def list_factors(self) -> list[str]:
        return sorted(self._library(XS_LIBRARY).list_symbols())

    def list_symbols(self) -> list[str]:
        return sorted(self._library(TS_LIBRARY).list_symbols())

    def _write(self, library: str, symbol: str, frame: pd.DataFrame, layout: str) -> int:
        symbol = require_name(symbol, "symbol")
        panel = validate_panel(frame)
        lib = self._library(library)
        lib.write(
            symbol,
            panel,
            metadata=_metadata(layout, symbol),
            prune_previous_versions=False,
        )
        return _version(lib, symbol)

    def _append(self, library: str, symbol: str, frame: pd.DataFrame, layout: str) -> int:
        symbol = require_name(symbol, "symbol")
        panel = validate_panel(frame)
        lib = self._library(library)
        lib.append(
            symbol,
            panel,
            metadata=_metadata(layout, symbol),
            prune_previous_versions=False,
        )
        return _version(lib, symbol)

    def _read(
        self,
        library: str,
        symbol: str,
        layout: str,
        as_of: datetime | int | str | None,
        decision_ts: datetime | None,
    ) -> FactorSnapshot:
        symbol = require_name(symbol, "symbol")
        lib = self._library(library)
        try:
            item = lib.read(symbol, as_of=as_of) if as_of is not None else lib.read(symbol)
        except Exception as exc:
            if _missing(exc):
                raise KeyError(symbol) from exc
            raise
        frame = slice_as_of(item.data, None if decision_ts is None else pd.Timestamp(decision_ts))
        return FactorSnapshot(
            symbol=symbol,
            layout=layout,
            frame=frame,
            version=int(item.version),
        )

    def _delete(self, library: str, symbol: str) -> None:
        symbol = require_name(symbol, "symbol")
        lib = self._library(library)
        if lib.has_symbol(symbol):
            lib.delete(symbol)

    def _library(self, name: str):
        cached = self._libs.get(name)
        if cached is not None:
            return cached
        existing = set(self._arctic.list_libraries())
        if name in existing:
            lib = self._arctic.get_library(name)
        else:
            lib = self._arctic.create_library(name, library_options=self._options)
        self._libs[name] = lib
        return lib


def _metadata(layout: str, symbol: str) -> dict[str, str | int]:
    return {"layout": layout, "symbol": symbol, "written_at_ns": clock_ns()}


def _version(lib, symbol: str) -> int:
    item = lib.read_metadata(symbol)
    return int(item.version)


def _missing(exc: Exception) -> bool:
    name = type(exc).__name__
    return "NoData" in name or "NoSuch" in name or "Unbound" in name
