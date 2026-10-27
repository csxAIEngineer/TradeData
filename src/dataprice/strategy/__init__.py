"""ICT + SMC + 流动性、猴市震荡网格、日线辩论分配，以及 TPO+VWAP。

信号、网格、分配和 TPO 都不读数据库，也不共用一套默认参数。回测行情来自 ``market.bars``。
"""

from dataprice.strategy.backtest import BacktestResult, format_report, run_backtest, simulate
from dataprice.strategy.debate import format_debate_report, resolve, run_debate
from dataprice.strategy.grid import (
    GridParams,
    GridResult,
    format_grid_report,
    grid_windows,
    run_grid,
    simulate_grid,
)
from dataprice.strategy.signals import (
    Signal,
    SignalBook,
    StrategyParams,
    continuation_params,
    evaluated_params,
    flow_params,
    generate_signals,
    long_params,
    regime_params,
    short_regime_params,
    trend_regime_params,
)
from dataprice.strategy.tpo_vwap import (
    TpoVwapParams,
    TpoVwapResult,
    format_tpo_vwap_report,
    run_tpo_vwap,
)
from dataprice.strategy.tpo_vwap import evaluated_params as tpo_vwap_params

__all__ = [
    "BacktestResult",
    "GridParams",
    "GridResult",
    "Signal",
    "SignalBook",
    "StrategyParams",
    "continuation_params",
    "evaluated_params",
    "flow_params",
    "format_debate_report",
    "format_grid_report",
    "format_report",
    "generate_signals",
    "grid_windows",
    "long_params",
    "regime_params",
    "resolve",
    "run_backtest",
    "run_debate",
    "run_grid",
    "short_regime_params",
    "simulate",
    "simulate_grid",
    "trend_regime_params",
    "TpoVwapParams",
    "TpoVwapResult",
    "format_tpo_vwap_report",
    "run_tpo_vwap",
    "tpo_vwap_params",
]
