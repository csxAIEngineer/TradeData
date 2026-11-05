"""策略。信号不读数据库，回测行情来自 ``market.bars``。

TPO 价值区加当日 VWAP 收回仍是 ``dataprice backtest``。
一击必杀是 ``dataprice.strategy.sniper``，走 ``dataprice sniper-backtest``。
"""

from dataprice.strategy.sniper import (
    SniperParams,
    SniperResult,
    format_sniper_report,
    run_sniper,
    sniper_params,
)
from dataprice.strategy.tpo_vwap import (
    TpoVwapParams,
    TpoVwapResult,
    format_tpo_vwap_report,
    run_tpo_vwap,
)
from dataprice.strategy.tpo_vwap import evaluated_params as tpo_vwap_params

__all__ = [
    "SniperParams",
    "SniperResult",
    "TpoVwapParams",
    "TpoVwapResult",
    "format_sniper_report",
    "format_tpo_vwap_report",
    "run_sniper",
    "run_tpo_vwap",
    "sniper_params",
    "tpo_vwap_params",
]
