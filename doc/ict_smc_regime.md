# Regime filter on candidate P

Same book as `doc/ict_smc_evaluation.md`. Structural candidate P is unchanged: `evaluated_params()`, 4h agreement, equal high/low only, stop at most 80 bps, limit at the zone edge, no 1h bias-flip exit. Size 0.01 BTC. Cost is 5 bps fee and 2 bps adverse slippage per side. Train is an entry before 2026-03-01. Holdout is an entry on or after that date. Signals are built on the full 30m/60m history; the split is the entry time.

The Binance taker-flow gate is not part of this comparison.

`make backtest` still runs P. `uv run dataprice backtest --regime` runs `regime_params()`.

## Why the holdout loses

P's 63 holdout trades lost 92.78 (win rate 29%, profit factor 0.54). The loss is spread from March through October 2026. June is the only positive month (+19.56 on 4 trades). Both sides lose: 34 buys, −34.36; 29 sells, −58.42.

The slices below are a breakdown of those closed trades. They are not themselves a backtest. A filter that refuses a bar can free the single position for a later signal, so the re-simulated totals further down are the ones that count. Features use only information known at the entry bar's close. Session is the UTC hour of that bar (Asia 00–08, London 08–13, New York 13–21, otherwise off). Volatility is the 14-bar average true range on 30m, ranked against the previous 20 days. Trend uses 4h bars aggregated from the stored 1h series, and only after that 4h bar has closed. Efficiency is the absolute net move divided by the sum of absolute 4h moves (1 is a straight line, 0 is a round trip). ADX here is a 14-bar 4h directional index, used only as a label.

| Holdout slice of P | Trades | PnL | Win rate |
| --- | ---: | ---: | ---: |
| New York | 26 | −53.92 | 23% |
| Asia | 21 | −28.96 | 29% |
| London | 7 | −3.12 | 43% |
| 21:00–24:00 | 9 | −6.78 | 33% |
| ATR percentile under 0.33 | 24 | −51.90 | 21% |
| ATR percentile 0.33–0.67 | 23 | −24.84 | 35% |
| ATR percentile over 0.67 | 16 | −16.04 | 31% |
| 4h ADX under 20 | 20 | −4.47 | 35% |
| 4h ADX 20–30 | 12 | +22.48 | 58% |
| 4h ADX at least 30 | 31 | −110.79 | 13% |
| 3-day 4h efficiency under 0.25 | 42 | −44.04 | 31% |
| 3-day 4h efficiency 0.25–0.45 | 13 | −30.63 | 23% |
| 3-day 4h efficiency at least 0.45 | 8 | −18.11 | 25% |

The same labels on the train set do not point at the holdout's worst buckets:

| Train slice of P | Trades | PnL | Win rate |
| --- | ---: | ---: | ---: |
| Asia | 26 | −66.23 | 19% |
| New York | 39 | +123.13 | 49% |
| ATR percentile under 0.33 | 32 | +35.87 | 44% |
| 4h ADX at least 30 | 51 | +126.91 | 45% |
| 3-day 4h efficiency under 0.25 | 61 | +2.44 | 41% |
| 24h 4h efficiency at least 0.45 | 49 | +147.27 | 51% |

New York, low volatility, and a strong 4h trend are where the holdout gives the money back, and they are also where the train made it. A rule that skips ADX above 30, or that skips New York, would have been thrown out on the train window. Asia is the session that already loses in train. A 3-day path with efficiency under 0.25 is 61 train trades for about +2, which is the dead range inside an otherwise profitable book.

## What was locked on train

Three gates are implemented and unit-tested. All of them default off, so P is unchanged. Each one fails closed when its sample is not long enough yet.

- `skip_sessions`: do not enter on a named UTC session. The zone can still be taken on a later bar inside the return window.
- `min_er` over `er_bars` closed 4h returns: Kaufman efficiency of that path must clear the threshold.
- `min_atr_pct` / `max_atr_pct`: 30m ATR percentile must sit inside the band.

Selection, before any of these gates was scored on the holdout: keep a variant only if it has at least 40 train trades and train PnL of at least +100, then take the highest train profit factor. Costs stay 5 bps + 2 bps.

| Train variant | Trades | PnL | Win rate | Profit factor | Max drawdown | Eligible |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| P | 97 | +156.67 | 44.3% | 1.47 | 43.02 | yes |
| Skip Asia | 78 | +192.66 | 50.0% | 1.81 | 48.54 | yes |
| Skip Asia and 21:00–24:00 | 71 | +165.53 | 49.3% | 1.75 | 48.54 | yes |
| 24h efficiency ≥ 0.25 | 73 | +103.71 | 42.5% | 1.41 | 51.12 | yes |
| 24h efficiency ≥ 0.35 | 62 | +116.07 | 45.2% | 1.55 | 32.05 | yes |
| 24h efficiency ≥ 0.45 | 52 | +139.93 | 50.0% | 1.89 | 31.74 | yes |
| 3-day efficiency ≥ 0.25 | 41 | +164.91 | 48.8% | 2.54 | 40.37 | yes |
| ATR percentile ≥ 0.33 | 70 | +157.17 | 48.6% | 1.65 | 36.84 | yes |
| ATR percentile ≤ 0.67 | 67 | +37.52 | 40.3% | 1.18 | 37.40 | no |

`regime_params()` is the 3-day row: the last 18 closed 4h returns (19 closes, about three days) must have efficiency of at least 0.25. That is the highest train profit factor inside the rule. It raises train PnL from +156.67 to +164.91 and cuts the trade count from 97 to 41. Skip-Asia and the 24h efficiency gate were eligible and were not chosen.

## Holdout after the lock

| Sample, 5 bps + 2 bps | Trades | Realized PnL | Win rate | Profit factor | Avg R | Max drawdown | Avg hold |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| P train | 97 | +156.67 | 44.3% | 1.47 | +0.59 | 43.02 | 12.7 bars |
| P holdout | 63 | −92.78 | 28.6% | 0.54 | −0.31 | 99.65 | 11.9 bars |
| P full window | 160 | +63.89 | 38.1% | 1.12 | +0.24 | 110.52 | 12.4 bars |
| Regime train | 41 | +164.91 | 48.8% | 2.54 | +1.02 | 40.37 | 11.1 bars |
| Regime holdout | 24 | −43.73 | 29.2% | 0.42 | −0.33 | 43.73 | 10.2 bars |
| Regime full window | 65 | +121.18 | 41.5% | 1.66 | +0.53 | 57.35 | 10.8 bars |

The gate does not repair the holdout. Dollar loss falls from 92.78 to 43.73 because there are 24 trades instead of 63. Win rate stays at 29%. Profit factor and average R get worse (0.42 and −0.33, against 0.54 and −0.31). Both sides still lose in the holdout (15 buys, −18.34; 9 sells, −25.39). The full-window +121.18 is the train profit minus that smaller holdout loss. It is not evidence the rally was handled.

Checked after the lock, and not used to change it: skip-Asia holdout is −34.75 on 47 trades (profit factor 0.73), and 24h efficiency ≥ 0.45 is −23.90 on 24 trades (profit factor 0.68). Both still lose. Neither would have been selected by the train rule above the 3-day gate.

## Reproduce

```shell
uv run dataprice backtest --symbol BTC-USDT --months 24
uv run dataprice backtest --regime --symbol BTC-USDT --months 24
make test
```

`--regime` cannot be combined with `--baseline` or `--flow`.
