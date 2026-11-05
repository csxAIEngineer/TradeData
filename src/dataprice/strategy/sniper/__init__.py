"""一击必杀 v0。宏观门、日线偏向、4H 收回、账户 R 目标 10。

信号不读数据库。``run_stored_sniper`` 才从 ``market.bars`` 和宏观表取数。
TPO / VWAP 仍走自己的模块。这里没有 ICT / SMC 那一套。
"""

from dataprice.strategy.sniper.backtest import (
    SniperResult,
    SniperTrade,
    run_sniper,
    run_sniper_intraday,
    run_stored_sniper,
)
from dataprice.strategy.sniper.params import SniperParams
from dataprice.strategy.sniper.params import evaluated_params as sniper_params
from dataprice.strategy.sniper.report import format_sniper_report

__all__ = [
    "SniperParams",
    "SniperResult",
    "SniperTrade",
    "format_sniper_report",
    "run_sniper",
    "run_sniper_intraday",
    "run_stored_sniper",
    "sniper_params",
]
