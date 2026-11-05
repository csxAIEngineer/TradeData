"""一击必杀 v0 的文字结果。"""

from __future__ import annotations

from decimal import Decimal

from dataprice.contracts import q_cash
from dataprice.strategy.sniper.backtest import SniperResult
from dataprice.strategy.sniper.params import SniperParams, evaluated_params


def format_sniper_report(result: SniperResult, params: SniperParams | None = None) -> str:
    params = params or evaluated_params()
    reasons: dict[str, int] = {}
    for trade in result.trades:
        reasons[trade.reason] = reasons.get(trade.reason, 0) + 1
    reason_text = "，".join(
        f"{name} {count}" for name, count in sorted(reasons.items())
    ) or "没有成交"
    wins = [trade for trade in result.trades if trade.pnl > 0]
    pnl = q_cash(sum((trade.pnl for trade in result.trades), Decimal("0")))
    account_rs = [trade.account_r for trade in result.trades if trade.account_r is not None]
    price_rs = [trade.price_r for trade in result.trades if trade.price_r is not None]
    avg_account = sum(account_rs) / len(account_rs) if account_rs else 0.0
    avg_price = sum(price_rs) / len(price_rs) if price_rs else 0.0
    count = len(result.trades)
    lines = [
        "一击必杀 v0：宏观门只拦新开仓，日线定偏向，4H 扫到前低或前高再收回才进场。",
        (
            f"MacroGate {params.macro_version}：DXY、10Y TIPS、HY OAS、VIX 等权。"
            f"因子不高于自己最近 {params.macro_lookback} 个收盘的中位数记 1，否则记 0。"
            f"分数 >= {params.theta:g} 才开门。四个里至少三个成立。"
        ),
        "t 日收盘从下一个工作日用。周六和周日沿用周五已经生效的那扇门，不把周五收盘提前用上。缺数据或过期就关门。",
        (
            "做多要 HH/HL，或收盘站上前 "
            f"{params.structure_days} 根日线的最高价，并且收盘在 EMA{params.ema_span} 之上。做空对称。没有偏向不做。"
        ),
        (
            f"4H 做多：最低价刺破前 {params.swing_n} 根的最低价，收盘回到那条低点之上。"
            "止损在这根影线之外。做空对称。同一根两边都扫到就放弃。"
        ),
        (
            "价格 R 是目标距离除以止损距离，不含手续费。"
            f"1 账户 R 是权益 {params.equity} 的 {params.risk_fraction:g}。"
            f"目标按账户 R >= {params.min_account_rr:g} 来解，杠杆上限 {params.leverage:g}。"
            "按这个风险把止损打满所需要的杠杆超过上限，就跳过，不缩小仓位。"
        ),
        "GATE=0 不平仓。偏向后来翻掉也不平。持仓直到止损或目标。最后一根还敞着的，按收盘记 end。",
        (
            f"费用每边 {params.fee_bps:g} bps，滑点每边 {params.slippage_bps:g} bps。"
            f"4H {result.bars} 根，{result.first_open:%Y-%m-%d %H:%M} → {result.last_open:%Y-%m-%d %H:%M}。"
            f"平仓 {count}，其中风险够不着而跳过的设置 {result.skipped}。出场：{reason_text}。"
        ),
        "",
        "| 笔数 | 胜率 | 净盈亏 | 平均价格R | 平均账户R |",
        "| ---: | ---: | ---: | ---: | ---: |",
        (
            f"| {count} | {(len(wins) / count) if count else 0.0:.1%} | {_signed(pnl)} | "
            f"{avg_price:+.2f} | {avg_account:+.2f} |"
        ),
    ]
    return "\n".join(lines)


def _signed(value: Decimal) -> str:
    text = f"{value:.4f}"
    if value > 0:
        return "+" + text
    return text
