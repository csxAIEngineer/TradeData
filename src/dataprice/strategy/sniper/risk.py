"""把信号收成一笔全仓计划。目标账户 R 解不出来，就返回 None。

数量 = 权益 × ``leverage`` / 进场成交价，再按数量精度四舍五入。名义本金就是这倍数，不是先按权益的固定比例切仓、再用杠杆当上限。

1 账户 R = 这仓按止损成交价平掉时的现金亏损，含进场和止损两边的手续费。
价格 R = 目标成交价到进场成交价的距离 / 进场成交价到止损成交价的距离。不含手续费。
账户 R = 扣掉两边手续费之后的盈亏 / 1 账户 R。目标价按账户 R = ``min_account_rr`` 来解。

止损不在亏损一侧、打到止损并没有现金亏损、或做空目标价不是正数，就跳过。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from dataprice.contracts import PRICE_QUANT, Side, q_cash, q_px, q_qty
from dataprice.strategy.sniper.params import SniperParams


@dataclass(frozen=True)
class TradePlan:
    side: Side
    qty: Decimal
    entry: Decimal
    stop: Decimal
    stop_fill: Decimal
    target: Decimal
    target_fill: Decimal
    price_risk: Decimal
    planned_price_r: float
    planned_account_r: float
    leverage_used: float
    risk_cash: Decimal


def plan_trade(
    side: Side,
    raw_entry: float,
    raw_stop: float,
    params: SniperParams,
) -> TradePlan | None:
    """``raw_stop`` 已经是影线之外的触发价。返回 None 表示目标账户 R 解不出来。"""
    entry_side: Side = side
    exit_side: Side = "sell" if side == "buy" else "buy"
    entry = _slip(_px(raw_entry), entry_side, params.slippage_bps)
    stop = _px(raw_stop)
    stop_fill = _slip(stop, exit_side, params.slippage_bps)
    price_risk = entry - stop_fill if side == "buy" else stop_fill - entry
    if price_risk <= 0 or entry <= 0 or stop <= 0:
        return None
    qty = q_qty(params.equity * Decimal(str(params.leverage)) / entry)
    if qty <= 0:
        return None
    stop_pnl = _pnl(side, qty, entry, stop_fill, params.fee_bps)
    if stop_pnl >= 0:
        return None
    risk_cash = -stop_pnl
    leverage_used = qty * entry / params.equity
    reward = q_cash(risk_cash * Decimal(str(params.min_account_rr)))
    target_fill = _solve_target_fill(side, entry, qty, reward, params.fee_bps)
    if target_fill is None:
        return None
    target = _unslip(target_fill, exit_side, params.slippage_bps)
    if target is None or target <= 0:
        return None
    if side == "buy" and not stop < entry < target:
        return None
    if side == "sell" and not target < entry < stop:
        return None
    target, target_fill = _nudge(side, qty, entry, stop, target, exit_side, reward, params)
    if target is None or target_fill is None:
        return None
    priced = target_fill - entry if side == "buy" else entry - target_fill
    if priced <= 0:
        return None
    account_r = float(_pnl(side, qty, entry, target_fill, params.fee_bps) / risk_cash)
    if account_r < params.min_account_rr:
        return None
    return TradePlan(
        side=side,
        qty=qty,
        entry=entry,
        stop=stop,
        stop_fill=stop_fill,
        target=target,
        target_fill=target_fill,
        price_risk=price_risk,
        planned_price_r=float(priced / price_risk),
        planned_account_r=account_r,
        leverage_used=float(leverage_used),
        risk_cash=risk_cash,
    )


def trade_pnl(side: Side, qty: Decimal, entry: Decimal, exit_px: Decimal, fee_bps: float) -> Decimal:
    return _pnl(side, qty, entry, exit_px, fee_bps)


def fill_price(raw: float, side: Side, slippage_bps: float) -> Decimal:
    """``side`` 是这笔成交的方向：平多用 sell，平空用 buy。"""
    return _slip(_px(raw), side, slippage_bps)


def _solve_target_fill(
    side: Side,
    entry: Decimal,
    qty: Decimal,
    reward: Decimal,
    fee_bps: float,
) -> Decimal | None:
    fee = Decimal(str(fee_bps)) / Decimal("10000")
    one = Decimal("1")
    if side == "buy":
        denom = one - fee
        if denom <= 0:
            return None
        return q_px((reward / qty + entry * (one + fee)) / denom)
    numer = entry * (one - fee) - reward / qty
    if numer <= 0:
        return None
    return q_px(numer / (one + fee))


def _nudge(
    side: Side,
    qty: Decimal,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    exit_side: Side,
    reward: Decimal,
    params: SniperParams,
) -> tuple[Decimal, Decimal] | tuple[None, None]:
    """四舍五入可能让扣费后的盈亏差一截。往有利方向推，直到账户 R 够到，或放弃。"""
    step = max(PRICE_QUANT, q_px(entry * Decimal("0.000001")))
    for _ in range(64):
        target_fill = _slip(target, exit_side, params.slippage_bps)
        if side == "buy" and not stop < entry < target:
            return None, None
        if side == "sell" and not target < entry < stop:
            return None, None
        if _pnl(side, qty, entry, target_fill, params.fee_bps) >= reward:
            return target, target_fill
        target = q_px(target + step) if side == "buy" else q_px(target - step)
        if target <= 0:
            return None, None
    return None, None


def _pnl(side: Side, qty: Decimal, entry: Decimal, exit_px: Decimal, fee_bps: float) -> Decimal:
    if side == "buy":
        gross = qty * (exit_px - entry)
    else:
        gross = qty * (entry - exit_px)
    paid = _fee(qty, entry, fee_bps) + _fee(qty, exit_px, fee_bps)
    return q_cash(gross - paid)


def _fee(qty: Decimal, price: Decimal, fee_bps: float) -> Decimal:
    if fee_bps <= 0:
        return Decimal("0")
    raw = qty * price * Decimal(str(fee_bps)) / Decimal("10000")
    return q_cash(raw)


def _slip(price: Decimal, side: Side, slippage_bps: float) -> Decimal:
    if slippage_bps <= 0:
        return price
    slip = Decimal(str(slippage_bps)) / Decimal("10000")
    if side == "buy":
        return q_px(price * (Decimal("1") + slip))
    return q_px(price * (Decimal("1") - slip))


def _unslip(fill: Decimal, side: Side, slippage_bps: float) -> Decimal | None:
    if slippage_bps <= 0:
        return fill
    slip = Decimal(str(slippage_bps)) / Decimal("10000")
    if side == "buy":
        raw = fill / (Decimal("1") + slip)
    else:
        denom = Decimal("1") - slip
        if denom <= 0:
            return None
        raw = fill / denom
    if raw <= 0:
        return None
    return q_px(raw)


def _px(value: float) -> Decimal:
    return q_px(Decimal(format(value, ".10f")))
