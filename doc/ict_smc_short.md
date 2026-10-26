# Short-only gate on candidate P

Same book as `doc/ict_smc_evaluation.md` and `doc/ict_smc_regime.md`. Candidate P is `evaluated_params()`: 4h agreement, equal high/low only, stop at most 80 bps, limit at the zone edge, no 1h bias-flip exit. Size 0.01 BTC. Cost is 5 bps fee and 2 bps adverse slippage per side. Train is an entry before 2026-03-01. Holdout is an entry on or after that date. Signals are built on the stored 30m/60m history; the split is the entry time.

The table below uses bars from 2024-11-05 04:30 UTC through 2026-10-26 01:30 UTC (34,555 `30m` bars, 17,277 `60m` bars). That is the sample the lock was chosen on. `uv run dataprice backtest --months 24` is the same length measured from the clock, so a later run adds bars at the end and drops them at the start.

Long rules are P's rules. The short gate runs after that signal list is built and can only delete sells. It does not retake the sweep on a later bar, and it does not change which buy signals exist. In this sample the closed long trades are also unchanged: 37 train buys, +98.87; 34 holdout buys, −34.36; 71 full-window buys, +64.51.

`make backtest` still runs P. `--regime` and `--trend-regime` are unchanged and still apply to both sides. `--short-regime` is the lock below.

## What was asked, and the train floor

P already requires a closed 4h bear bias for a short. A short can still be a dip inside a daily uptrend. Two definitions of "a real decline, and not chop" were scored on train only.

A variant was eligible only if it had at least 20 closed train shorts and train short PnL of at least +50. Among those, the highest train short profit factor wins. It also had to beat P's train short profit factor (1.27). Holdout was not used to drop, add, or rank a row. The same costs as the rest of this file.

P's own train shorts, labeled with the closed daily structure and not re-simulated:

| Train sells of P | Trades | PnL | Win rate | Profit factor |
| --- | ---: | ---: | ---: | ---: |
| Closed daily bias bull | 30 | +88.35 | 46.7% | 1.88 |
| Closed daily bias bear | 30 | −30.55 | 33.3% | 0.73 |
| Closed daily bias neutral | 0 | 0 | — | — |

The train short profit sits in the daily-bull bucket. The daily-bear bucket loses. There is no neutral daily bias on these trades.

## Menu 1, rejected: daily bear and not chop

Every row requires the closed daily structure to be bear, and then a non-chop gate. "Down" also requires that same window's net close change to be negative. These are re-simulated. Buys stay on P. None cleared the floor. Daily-bear shorts stay a loss, and the efficient ones are the worse slice (8 trades, −20.07, profit factor 0.42).

| Train shorts | Trades | PnL | Profit factor | Eligible |
| --- | ---: | ---: | ---: | --- |
| Daily bear only | 30 | −30.55 | 0.73 | no |
| Daily + 4h efficiency ≥ 0.15 / 18 | 18 | −32.95 | 0.56 | no |
| Daily + 4h efficiency ≥ 0.25 / 18 | 8 | −20.07 | 0.42 | no |
| Daily + 4h efficiency ≥ 0.35 / 18 | 5 | −4.34 | 0.77 | no |
| Daily + 4h efficiency ≥ 0.25 / 6 | 24 | −10.35 | 0.87 | no |
| Daily + 4h efficiency ≥ 0.45 / 6 | 18 | −15.71 | 0.75 | no |
| Daily + 4h efficiency ≥ 0.25 / 12 | 14 | −21.58 | 0.58 | no |
| Daily + 4h efficiency ≥ 0.35 / 12 | 10 | −16.03 | 0.57 | no |
| Daily + 4h efficiency ≥ 0.25 / 30 | 7 | +0.71 | 1.03 | no |
| Daily + 4h ≥ 0.25 / 18 and net down | 8 | −20.07 | 0.42 | no |
| Daily + 4h ≥ 0.25 / 6 and net down | 14 | +12.62 | 1.30 | no |
| Daily + 4h ≥ 0.35 / 12 and net down | 9 | −11.92 | 0.64 | no |
| Daily + 1d efficiency ≥ 0.25 / 5 | 18 | −39.51 | 0.50 | no |
| Daily + 1d efficiency ≥ 0.35 / 5 | 14 | −30.58 | 0.47 | no |
| Daily + 1d efficiency ≥ 0.25 / 8 | 17 | −42.19 | 0.46 | no |
| Daily + 1d efficiency ≥ 0.45 / 3 | 21 | −4.68 | 0.94 | no |
| Daily + 1d ≥ 0.25 / 5 and net down | 17 | −35.40 | 0.52 | no |
| Daily + 1d ≥ 0.35 / 8 and net down | 12 | −30.03 | 0.48 | no |

This menu was closed before the holdout of any of these rows was looked at. Requiring a closed daily bear does not improve the train short book. It deletes the bucket that paid.

## Menu 2, the lock: short-only 4h path

Opened after menu 1 failed, still on train, same floor. No daily-bias requirement. Efficiency is the same Kaufman ratio as `--regime`. Net down is the sign of that window. Both are known only after the 4h bar has closed, and both apply only to sells.

| Train shorts | Trades | PnL | Win rate | Profit factor | Eligible |
| --- | ---: | ---: | ---: | ---: | --- |
| Net down, 18 bars | 44 | +77.96 | 40.9% | 1.52 | yes |
| Net down, 6 bars | 45 | +59.22 | 44.4% | 1.39 | yes |
| Net down and efficiency ≥ 0.25 / 18 | 19 | +89.55 | 42.1% | 2.72 | no |
| Net down and efficiency ≥ 0.25 / 6 | 36 | +80.03 | 50.0% | 1.68 | yes |
| Net down and efficiency ≥ 0.45 / 6 | 29 | +79.16 | 51.7% | 1.89 | yes |
| Efficiency ≥ 0.25 / 18 | 20 | +93.49 | 45.0% | 2.80 | yes |

`short_regime_params()` is the last row: the last 18 closed 4h returns must have efficiency of at least 0.25, shorts only. It is the highest train short profit factor inside the floor, 2.80 against P's 1.27, on 20 shorts and +93.49. The 19-trade row is the same idea plus a falling path. It missed the count floor by one trade and was not taken. The lock sits on that count boundary.

The 20 kept train shorts are not a cleaned bear book. Twelve are still daily-bull (+113.56, profit factor 7.52). Eight are daily-bear (−20.07, profit factor 0.42), the same losing slice as menu 1.

## Holdout after the lock

| Sample, 5 bps + 2 bps | Trades | Realized PnL | Win rate | Profit factor | Avg R | Max drawdown | Avg hold |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| P train | 97 | +156.67 | 44.3% | 1.47 | +0.59 | 43.02 | 12.7 bars |
| P holdout | 63 | −92.78 | 28.6% | 0.54 | −0.31 | 99.65 | 11.9 bars |
| P full window | 160 | +63.89 | 38.1% | 1.12 | +0.24 | 110.52 | 12.4 bars |
| Regime train | 41 | +164.91 | 48.8% | 2.54 | +1.02 | 40.37 | 11.1 bars |
| Regime holdout | 24 | −43.73 | 29.2% | 0.42 | −0.33 | 43.73 | 10.2 bars |
| Regime full window | 65 | +121.18 | 41.5% | 1.66 | +0.53 | 57.35 | 10.8 bars |
| Trend-regime train | 41 | +164.91 | 48.8% | 2.54 | +1.02 | 40.37 | 11.1 bars |
| Trend-regime holdout | 24 | −43.73 | 29.2% | 0.42 | −0.33 | 43.73 | 10.2 bars |
| Trend-regime full window | 65 | +121.18 | 41.5% | 1.66 | +0.53 | 57.35 | 10.8 bars |
| Short train | 57 | +192.36 | 49.1% | 2.15 | +0.94 | 34.98 | 13.6 bars |
| Short holdout | 42 | −64.40 | 28.6% | 0.53 | −0.28 | 64.40 | 14.3 bars |
| Short full window | 99 | +127.96 | 40.4% | 1.42 | +0.42 | 89.17 | 13.9 bars |

`--trend-regime` matches `--regime` on this candidate, as in `doc/ict_smc_regime.md`.

Side split. Buy rows of the short rule match P. Regime and trend-regime still change both sides.

| Sample | P buys | P sells | Regime sells | Short buys | Short sells |
| --- | ---: | ---: | ---: | ---: | ---: |
| Train | 37 / +98.87 | 60 / +57.80 / 1.27 | 24 / +111.26 / 2.74 | 37 / +98.87 | 20 / +93.49 / 2.80 |
| Holdout | 34 / −34.36 | 29 / −58.42 / 0.39 | 9 / −25.39 / 0.22 | 34 / −34.36 | 8 / −30.04 / 0.08 |
| Full window | 71 / +64.51 | 89 / −0.62 / 1.00 | 33 / +85.87 / 1.89 | 71 / +64.51 | 28 / +63.45 / 1.75 |

Holdout short win rate is 12.5% and average R is −0.77. Dollar loss falls from 58.42 to 30.04 because 21 of the 29 shorts are gone. Profit factor falls from 0.39 to 0.08. The full-window short profit (+63.45) is the train profit minus that smaller holdout loss.

Closed daily bias of the shorts. Bull means P's 4h bear short was taken while the closed daily structure was still bull.

| Shorts | P train | P holdout | Short-rule train | Short-rule holdout |
| --- | ---: | ---: | ---: | ---: |
| Daily bull | 30 / +88.35 / 1.88 | 16 / −34.21 / 0.37 | 12 / +113.56 / 7.52 | 3 / −7.33 / 0.25 |
| Daily bear | 30 / −30.55 / 0.73 | 13 / −24.21 / 0.42 | 8 / −20.07 / 0.42 | 5 / −22.71 / 0.00 |

The efficiency gate does cut bull-market shorts in the holdout, from 16 trades and −34.21 to 3 trades and −7.33. The three that remain still lose. It does not improve bear-market shorts. Train daily-bear profit factor stays 0.42. Holdout daily-bear shorts go from 13 trades at a 15% win rate to 5 trades at a 0% win rate, and the dollar loss barely moves (−24.21 to −22.71). The train profit factor of 2.80 is the twelve daily-bull shorts.

Checked after the lock, and not used to change it: daily bear alone, and daily bear plus 4h efficiency ≥ 0.25 / 18. Both still refuse every daily-bull short. Both still lose on the shorts they keep.

| Rejected, not locked | Train sells | Holdout sells | Holdout book |
| --- | ---: | ---: | ---: |
| Daily bear only | 30 / −30.55 / 0.73 | 13 / −24.21 / 0.42 | 47 / −58.57 / 0.60 |
| Daily bear + 4h ≥ 0.25 / 18 | 8 / −20.07 / 0.42 | 5 / −22.71 / 0.00 | 39 / −57.07 / 0.55 |

Their holdout longs are P's −34.36. Removing the daily-bull shorts does not leave a bear-market short book that makes money in either window.

## What changed in the rules

`short_daily_bias`, `short_min_er` / `short_er_bars` / `short_er_freq`, and `short_net_down` default off. P, `--regime`, and `--trend-regime` do not set them. When one of them is on, a sell is dropped unless the closed daily bias is bear, the short efficiency window clears its floor, and, if asked, that window's net change is down. A missing sample fails closed. Buys ignore the flags.

`short_regime_params()` sets only `short_min_er=0.25` and `short_er_bars=18` on P. Shared `min_er` stays off, so longs are not efficiency-gated.

## Reproduce

```shell
uv run dataprice backtest --symbol BTC-USDT --months 24
uv run dataprice backtest --regime --symbol BTC-USDT --months 24
uv run dataprice backtest --trend-regime --symbol BTC-USDT --months 24
uv run dataprice backtest --short-regime --symbol BTC-USDT --months 24
make test
```

`--short-regime` cannot be combined with `--baseline`, `--flow`, `--regime`, or `--trend-regime`.
