# Long side of candidate P

Same book as `doc/ict_smc_evaluation.md`. Costs are 5 bps fee and 2 bps adverse slippage per side. Size is 0.01 BTC. Train is an entry before 2026-03-01. Holdout is an entry on or after that date. Signals are built on the full 30m/60m history; the split is the entry time.

This note is long-only. Every row below is resimulated with `sides=("buy",)`, so a short cannot occupy the single position. On this sample that resimulation matches the buy slice of the two-sided book: candidate P's longs are 37 train trades for +98.87 and 34 holdout trades for −34.36, the same figures as `doc/ict_smc_regime.md`. The efficiency gate's longs are 17 train trades for +53.65 and 15 holdout trades for −18.34.

`make backtest` is still two-sided P. `uv run dataprice backtest --buy-only` is long-only P. `uv run dataprice backtest --regime --buy-only` is the long-only efficiency gate. `uv run dataprice backtest --long` is the rule locked below.

## What was searched

The holdout longs lose inside a rally, so the first guess was "only buy an established 4h uptrend." Candidate P already requires the closed 4h structure to be bullish before a long. Extra trend rules were still tested, using only 4h information that has closed: signed efficiency (net move over the path, positive when the window rose), a higher 4h swing low, and a 4h close above its own simple average.

The other family is the sweep itself. A long sweeps sell-side equal lows: the wick trades through the level and the bar closes back above it. Close location is `(close - low) / (high - low)` on that sweep bar. A value near 1 means the close reclaimed the top of the bar. A value near 0 means the close stayed on the lows. Two path rules were also tested: no later close back through the sweep wick, and no later close back through the equal-low level, up to and including the entry bar.

Selection, before any of these variants was scored on the holdout. Long-only, same costs. Keep a variant only if it has at least 20 train trades, train PnL of at least +88.87 (10 dollars under P's +98.87), and train profit factor of at least 1.85 (P's long train profit factor). Then take the highest train profit factor, then the higher train PnL, then the larger trade count. If both a sweep rule and a trend rule cleared that bar, their pair would have been resimulated under the same rule. None of the trend rules cleared it, so no pair was run.

| Train variant | Trades | PnL | Win rate | Profit factor | Eligible |
| --- | ---: | ---: | ---: | ---: | --- |
| P long-only | 37 | +98.87 | 51.4% | 1.85 | baseline |
| Efficiency gate, long-only | 17 | +53.65 | 52.9% | 2.25 | no |
| Sweep close location ≥ 0.55 | 30 | +122.29 | 60.0% | 2.59 | yes |
| Sweep close location ≥ 0.70 | 23 | +112.58 | 60.9% | 3.04 | yes |
| Sweep close location ≥ 0.80 | 21 | +91.65 | 57.1% | 2.66 | yes |
| Reclaim ≥ 5 bps | 28 | +108.70 | 57.1% | 2.39 | yes |
| Reclaim ≥ 10 bps | 24 | +80.77 | 50.0% | 2.05 | no |
| Reclaim ≥ 20 bps | 18 | +36.66 | 38.9% | 1.51 | no |
| No close through the sweep wick | 32 | +97.42 | 53.1% | 1.95 | yes |
| No close back through the equal low | 24 | +43.42 | 50.0% | 1.52 | no |
| 6-bar signed efficiency ≥ 0 | 30 | +88.26 | 50.0% | 1.96 | no |
| 6-bar signed efficiency ≥ 0.15 | 24 | +35.29 | 45.8% | 1.45 | no |
| 6-bar signed efficiency ≥ 0.45 | 12 | +52.63 | 66.7% | 3.11 | no |
| 18-bar signed efficiency ≥ 0 | 34 | +66.05 | 50.0% | 1.59 | no |
| 18-bar signed efficiency ≥ 0.25 | 16 | +38.55 | 50.0% | 1.90 | no |
| 42-bar signed efficiency ≥ 0 | 25 | +66.40 | 52.0% | 1.88 | no |
| Higher 4h swing low | 27 | +80.13 | 59.3% | 2.07 | no |
| 4h close above 18-bar average | 32 | +46.60 | 50.0% | 1.45 | no |
| 4h close above 42-bar average | 29 | +81.17 | 55.2% | 1.93 | no |

`long_params()` is the 0.70 close-location row. It had the highest train profit factor inside the rule. The 0.55 row made more train dollars and was not chosen. Requiring the equal low to keep holding after the sweep removed the profitable train trades and failed the floor. The uptrend filters did the same: the train tape is roughly flat, and P's long profit was not sitting in the slices that were already trending.

## Holdout after the lock

Scored once, after the row above was chosen. The other variants in the table were not scored on the holdout.

| Sample, 5 bps + 2 bps | Trades | Realized PnL | Win rate | Profit factor | Avg R | Max drawdown | Avg hold |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| P long train | 37 | +98.87 | 51.4% | 1.85 | +0.55 | 29.11 | 14.5 bars |
| P long holdout | 34 | −34.36 | 32.4% | 0.67 | −0.17 | 42.18 | 15.5 bars |
| P long full window | 71 | +64.51 | 42.3% | 1.29 | +0.21 | 60.32 | 15.0 bars |
| Efficiency long train | 17 | +53.65 | 52.9% | 2.25 | +0.33 | 17.83 | 10.1 bars |
| Efficiency long holdout | 15 | −18.34 | 33.3% | 0.58 | −0.18 | 32.07 | 11.3 bars |
| Efficiency long full window | 32 | +35.32 | 43.8% | 1.41 | +0.09 | 36.16 | 10.7 bars |
| Close location ≥ 0.70 train | 23 | +112.58 | 60.9% | 3.04 | +1.01 | 14.03 | 13.9 bars |
| Close location ≥ 0.70 holdout | 18 | +0.04 | 44.4% | 1.00 | +0.11 | 22.52 | 15.2 bars |
| Close location ≥ 0.70 full window | 41 | +112.62 | 53.7% | 2.11 | +0.61 | 22.52 | 14.5 bars |

The holdout is flat. Eighteen trades, profit factor 1.00, four cents of realized PnL. That is not a demonstrated edge. It is also not the −34.36 loss of long-only P, or the −18.34 loss of the efficiency gate. Win rate on the holdout is 44%, against 32% for long-only P. Average R is about +0.11, against −0.17. The full-window +112.62 is the train profit plus that flat holdout. One more losing holdout trade, or a wider fee, can put the holdout back under water. The window for these numbers was 34,554 `30m` bars from 2024-11-05 05:00 UTC through 2026-10-26 01:30 UTC.

The rule does not change shorts, because the profile does not emit them. `long_min_sweep_clv` left at its default does not change candidate P. Turning the threshold on inside a two-sided book would only drop buys; it is not the profile that was scored.

## What to change next

Do not pick a different threshold, or add another filter, by looking at this holdout. The 0.55 and 0.80 neighbors were eligible on train and were not scored out of sample.

The uptrend family is the wrong repair for this train set. A bullish closed 4h structure is already required. Asking for a rising 4h path on top of that threw out train trades that had paid, and it never reached the selection bar.

The holdout book is too small to tell a true zero from a small positive or a small negative. The next measurement is a new window that was not used to choose 0.70, or a forward record of the same fixed rule. Until that exists, the honest summary is: a train-locked rejection filter stopped the long holdout from losing and did not show that it can make money there.

## Reproduce

```shell
uv run dataprice backtest --buy-only --symbol BTC-USDT --months 24
uv run dataprice backtest --regime --buy-only --symbol BTC-USDT --months 24
uv run dataprice backtest --long --symbol BTC-USDT --months 24
make test
```

The report prints a train line and a holdout line split at 2026-03-01. `--long` cannot be combined with `--baseline`, `--flow`, `--regime`, or `--trend-regime`.
