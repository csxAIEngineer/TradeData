# ICT + SMC BTC evaluation

Backtest of `BTC-USDT` 30-minute entries with 1-hour structure, using bars stored in ClickHouse `market.bars`. The window is 2024-11-05 through 2026-10-26 (34,559 `30m` bars and 17,279 `60m` bars). Price went from about 67,871 to 79,758.

Size is 0.01 BTC. One position at a time. Long entries and exits go through `hotpath.TradingState`. Shorts use the same halt, notional, and buying-power checks, because that path cannot hold a negative position. No charts.

## Costs

The framework already had `fee_bps`. This round adds `slippage_bps`, applied against the trader on every fill (entry, stop, target, and bias-flip exit).

| Assumption | Value |
| --- | --- |
| Fee | 5 bps per side |
| Slippage | 2 bps adverse per side |
| Round trip | 14 bps of price |

OKX spot's public base tier is about 10 bps taker / 8 bps maker. 5 bps is a mid-tier taker, not a retail worst case. A 10 bps fee is reported as a stress, not as the headline. There is no funding rate and no partial fill.

## Why the baseline loses

Baseline rules: 1h BOS/CHoCH bias, 30m bias must agree, any liquidity sweep (equal high/low, previous UTC day, previous UTC session), then a with-structure FVG or order block, fill at the close, stop at the sweep wick, target at the nearest opposing liquidity beyond 1R (otherwise 2R). A 1h bias flip exits at the close.

On the full window with **zero** costs: 830 signals, 605 closed trades, win rate 29.1%, profit factor 0.78, average R about -0.07, realized PnL **-608.79**.

With the 5 bps + 2 bps cost: 605 trades, profit factor 0.58, realized PnL **-1,358.41**, max drawdown 1,479.66.

The loss is not one side and not one month. Buys were -276 and sells -333 at zero cost, with almost the same win rate (29.3% and 28.9%). Exit reason is the split that matters, still at zero cost:

| Exit | Trades | PnL | Avg R | Avg hold (30m bars) |
| --- | ---: | ---: | ---: | ---: |
| Target | 141 | +1,902 | +1.95 | 19 |
| Stop | 262 | -1,671 | -1.00 | 10 |
| 1h bias flip | 202 | -840 | -0.27 | 33 |

Targets pay. Stops cost a full R, and the wide ones cost more dollars because size is fixed at 0.01 BTC (risk width: 10th percentile 25 bps, median 90 bps, 90th percentile 233 bps). Bias-flip exits are the slow leak: most are small losers, and they cut the trade before either stop or target. Order blocks won 19.5% of the time versus 30.6% for FVGs. Equal lows were the least bad pool (about flat); session highs and equal highs were the largest dollar losses. Planned reward/risk had a median of only 1.59, which cannot carry a 29% win rate once the bias-flip exits are included.

Train window for every rule change below: entries before 2026-03-01 (about 23,083 bars, price roughly flat, 67,871 to 66,969). Holdout: entries from 2026-03-01 onward (price 66,779 to 79,758, a rally). Signals are generated on the full history so the 1h and 4h state is warmed up; a trade is train or holdout by its entry time. The loss breakdown above used the full sample. The parameter search below did not look at holdout until the candidate was already chosen.

## Experiments

All of these use 5 bps fee and 2 bps slippage. Train PnL is the selection metric. Single changes were not enough; every one of them still lost on the train window.

| Train experiment | Trades | PnL | Win rate | Profit factor | Avg R |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline | 370 | -983.66 | 28.1% | 0.58 | -0.16 |
| No bias-flip exit | 269 | -716.03 | 33.8% | 0.72 | -0.12 |
| FVG only | 330 | -845.12 | 29.1% | 0.60 | -0.07 |
| Stop distance ≤ 80 bps | 257 | -362.96 | 28.4% | 0.64 | -0.13 |
| Minimum target 2R | 351 | -915.37 | 23.4% | 0.61 | -0.14 |
| Close within 20 bps of the zone | 356 | -989.09 | 27.5% | 0.55 | -0.18 |
| Equal high/low only | 256 | -486.19 | 30.5% | 0.68 | -0.11 |
| 4h bias must agree | 214 | -490.63 | 33.2% | 0.66 | -0.00 |
| Move stop to entry after 1R | 379 | -1,009.97 | 24.0% | 0.55 | -0.48 |

The least-bad pieces were a hard cap on stop distance, a 4h agreement filter, equal-high/low pools, and turning the bias-flip exit off. Combining those, still on the train window only:

| Candidate | Trades | PnL | Win rate | Profit factor | Max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: |
| G: 4h + risk ≤ 80 bps + equal high/low + no bias-flip exit | 90 | +108.10 | 43.3% | 1.35 | 68.30 |
| J: limit entry at the zone + risk ≤ 80 bps + 4h | 146 | +95.57 | 40.4% | 1.20 | 78.06 |
| P: J's limit entry added to G | 97 | +156.67 | 44.3% | 1.47 | 43.02 |

P was selected because it had the best train PnL, profit factor, and drawdown. Neighbors stayed positive on train (risk cap 60 or 100 bps, FVG only, J with the bias-flip exit removed), so the train result is not a single knife-edge setting. That is not the same as a confirmed holdout.

## Chosen candidate

`evaluated_params()` is candidate P. `make backtest` runs it.

- 4h structure, built from the stored 1h bars and only after the 4h bar has closed, must match the 1h and 30m bias.
- Liquidity pools are only equal highs and equal lows. Session and previous-day levels are not used.
- The sweep-to-stop distance must be at most 80 bps of the entry.
- Entry is a limit at the near edge of the FVG or order block when that bar trades there, not the close.
- Exit is the stop or the liquidity/2R target. A 1h bias flip does not flatten the trade.
- FVG is still preferred over an order block when both qualify. Order blocks remain allowed.

Holdout was scored after this choice, for P and for the two parents. None of them survived.

| Sample, 5 bps + 2 bps | Trades | Realized PnL | Win rate | Profit factor | Avg R | Max drawdown | Avg hold |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| P train (to 2026-03-01) | 97 | +156.67 | 44.3% | 1.47 | +0.59 | 43.02 | 12.7 bars (6.4h) |
| P holdout (from 2026-03-01) | 63 | **-92.78** | 28.6% | 0.54 | -0.31 | 99.65 | 11.9 bars (6.0h) |
| P full window | 160 | **+63.89** | 38.1% | 1.12 | +0.24 | 110.52 | 12.4 bars (6.2h) |
| G holdout | 60 | -95.67 | 28.3% | 0.49 | -0.35 | 96.49 | 9.9 bars |
| J holdout | 84 | -107.25 | 28.6% | 0.58 | +0.01 | 118.89 | 12.0 bars |
| Baseline full window | 605 | -1,358.41 | 27.9% | 0.58 | -0.14 | 1,479.68 | 19.8 bars |

P's full-window profit is the train profit minus the holdout loss. Max drawdown on the full window (110.52) is larger than the ending profit (63.89).

Walk-forward of the same fixed rules, not used to pick them:

| Entry window | Trades | PnL | Win rate | Profit factor | Max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2024-11 to 2025-07 | 40 | +83.98 | 45.0% | 1.62 | 29.88 |
| 2025-07 to 2026-03 | 57 | +72.69 | 43.9% | 1.37 | 37.19 |
| 2026-03 to 2026-10 | 63 | -92.78 | 28.6% | 0.54 | 99.65 |

The first two slices split the train set and were both profitable, so the train result was not one lucky month. The third slice is the holdout, and the win rate fell from the mid-40s to 29%. Both sides lost there: longs -34.36 (32% wins), shorts -58.42 (24% wins). Over the whole window longs made +64.51 and shorts were about flat (-0.62). A long-only cut would still have lost money in the holdout rally, so dropping shorts does not repair the failure.

## Cost stress on P, full window

| Fee / slippage, per side | Full-window PnL | Holdout PnL | Full-window profit factor |
| --- | ---: | ---: | ---: |
| 0 / 0 | +262.24 | -27.90 | 1.64 |
| 5 / 2 (headline) | +63.89 | -92.78 | 1.12 |
| 8 / 2 | -21.11 | -120.58 | 0.96 |
| 10 / 2 | -77.79 | -139.12 | 0.87 |
| 5 / 5 | -21.11 | -120.58 | 0.96 |

Zero costs do not save the holdout. A 10 bps taker fee does not save the full window either. The headline profit exists only inside a narrow cost band and only if the losing holdout is averaged with the earlier sample.

## What works

- Requiring the 4h bias, refusing stops wider than 80 bps, and ignoring session/previous-day pools cuts a 600-trade loser down to about 160 trades and removes most of the dollar damage from wide stops.
- Filling at the zone edge instead of the close improved the train profit factor. The bar still has to trade into the zone; the fill is not a better price than the bar printed.
- On the trades that do reach the target, both equal-high shorts and equal-low longs pay. On the full P sample those two target buckets were +310 and +284, and the matching stops were -311 and -220.

## What does not

- The original rule set. It over-trades, exits on a 1h flip that is not the invalidation of the setup, and lets a 200 bps sweep define the stop while the position is a fixed 0.01 BTC.
- Breakeven-after-1R. It made the train result worse (profit factor 0.55).
- Trading only the first few hours after a 1h BOS. That was the worst single train experiment (profit factor 0.35).
- Treating the full-window +63.89 as evidence the strategy is profitable going forward. The holdout gave that number back, and a retail taker fee removes it even in sample.

## Next risks

- The holdout was a rally after a flat training tape. The filters may be describing chop, not a sweep-and-return edge that survives a trend.
- 80 bps, the equal-high/low list, and the 4h gate were chosen on one sample. A different cap (60 or 100 bps) still made money in train, but none of the parents made money after February 2026.
- Fixed 0.01 BTC means a stop at the 80 bps cap loses more dollars than a tight stop. Risk-based sizing would change the dollar path even if average R stayed the same.
- Stops are assumed to fill at the stop, plus slippage. A gap through the sweep low is worse than the model, and the same-bar stop-before-target rule already takes the pessimistic path when both are touched.
- Shorts are a cash model beside the hot path, not a real borrow. The long path is the one `TradingState` actually books.

## Reproduce

```shell
make up && make bootstrap
uv run dataprice backfill --symbol BTC-USDT --months 24
uv run dataprice backtest --symbol BTC-USDT --months 24
uv run dataprice backtest --baseline --fee-bps 0 --slippage-bps 0
make test
```

`make backtest` is the candidate: `evaluated_params()`, 5 bps fee, 2 bps slippage. `--baseline` is the original loose rule set.

Real aggressor volume (not an OHLC proxy) is a separate comparison in `doc/ict_smc_flow.md`. `make flow-backfill` loads it, and `uv run dataprice backtest --flow` runs the filter.

A train-locked 4h efficiency gate on this same candidate is in `doc/ict_smc_regime.md`. `uv run dataprice backtest --regime` runs it. The holdout is still a loss.

A train-locked long-only sweep rejection is in `doc/ict_smc_long.md`. `uv run dataprice backtest --long` runs it. The long holdout is flat, not a profit.
