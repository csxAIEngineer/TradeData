"""一击必杀 v0。宏观门、日线偏向、4H 回踩流动性区、账户 R 目标 3。

信号不读数据库。``run_stored_sniper`` 才从 ``market.bars`` 和宏观表取数。
TPO / VWAP 仍走自己的模块。这里的进场是摆动点、订单块和公允价值缺口的回踩。
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
