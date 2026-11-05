"""一击必杀 v0 的锁死参数。

价格 R 和账户 R 是两个数。价格 R 只看价格距离。账户 R 看扣费后的盈亏
除以配置的风险预算。杠杆决定这笔 1 账户 R 的仓位能不能上，不上就跳过。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

MACRO_GATE_VERSION = "v0.1"
MACRO_FACTORS = ("dxy", "tips_10y", "hy_oas", "vix")


@dataclass(frozen=True)
class SniperParams:
    """v0 的全部数字。``evaluated_params`` 返回这份默认值。"""

    theta: float = 0.55
    macro_lookback: int = 63
    macro_min_obs: int = 63
    macro_max_stale_days: int = 10
    macro_version: str = MACRO_GATE_VERSION
    ema_span: int = 50
    structure_days: int = 20
    pivot_k: int = 2
    swing_n: int = 20
    stop_buffer_bps: float = 0.0
    equity: Decimal = Decimal("10000")
    risk_fraction: float = 0.01
    leverage: float = 5.0
    min_account_rr: float = 10.0
    fee_bps: float = 5.0
    slippage_bps: float = 2.0

    def __post_init__(self) -> None:
        if not 0 < self.theta <= 1:
            raise ValueError("theta 必须在 (0, 1] 内")
        if self.macro_lookback < 3 or self.macro_min_obs < 3:
            raise ValueError("宏观窗口至少要 3 个点")
        if self.macro_min_obs > self.macro_lookback:
            raise ValueError("macro_min_obs 不能大于 macro_lookback")
        if self.macro_max_stale_days < 3:
            raise ValueError("macro_max_stale_days 至少为 3，周一才能用到周五的收盘")
        if self.macro_version != MACRO_GATE_VERSION:
            raise ValueError(f"宏观门版本只能是 {MACRO_GATE_VERSION}")
        if self.ema_span < 2:
            raise ValueError("ema_span 至少为 2")
        if self.structure_days < 2:
            raise ValueError("structure_days 至少为 2")
        if self.pivot_k < 1:
            raise ValueError("pivot_k 至少为 1")
        if self.swing_n < 2:
            raise ValueError("swing_n 至少为 2")
        if self.stop_buffer_bps < 0:
            raise ValueError("stop_buffer_bps 不能为负")
        if self.equity <= 0:
            raise ValueError("equity 必须为正")
        if not 0 < self.risk_fraction <= 1:
            raise ValueError("risk_fraction 必须在 (0, 1] 内")
        if self.leverage < 1:
            raise ValueError("leverage 至少为 1")
        if self.min_account_rr < 10:
            raise ValueError("min_account_rr 至少为 10")
        if self.fee_bps < 0 or self.slippage_bps < 0:
            raise ValueError("费用和滑点不能为负")


def evaluated_params() -> SniperParams:
    """锁死的 v0。不要按回测成交改这些数。

    杠杆 5、单笔风险 1% 是配置，不是拟合出来的。账户 R 目标是 10。
    """
    return SniperParams()
