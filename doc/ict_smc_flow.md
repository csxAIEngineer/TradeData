# Real liquidity versus structural pools

Same BTC backtest as `doc/ict_smc_evaluation.md`: OKX `BTC-USDT` 30-minute entries, 1-hour and 4-hour structure, candidate P from `evaluated_params()`. Size 0.01 BTC. Headline cost is 5 bps fee and 2 bps adverse slippage per side. Train is an entry before 2026-03-01. Holdout is an entry from 2026-03-01. Signals are built on the full stored history; the split is by entry time. This pass did not retune P's structural rules, and it did not pick a flow threshold from the holdout.

## What could not be loaded

OKX public REST does not have a multi-year order book for this window.

- Live `books5` still writes top-of-book into `market.quotes` from the moment the feed is running. That table is not a historical L2 archive, and the backtest does not read it.
- Historical OKX swap L2 (`books`, `books-l2-tbt`) and the `liquidation-orders` stream are sold by Tardis from 2023-03-30. That archive was not purchased.
- `GET /api/v5/public/liquidation-orders` (`instType=SWAP`, `uly=BTC-USDT`, `state=filled`, `limit=100`) paginates with `after`. On 2026-10-26 it walked back to `2026-10-25T04:58:02Z` and the next page was `code=0` with an empty `data` list. That is about 23 hours of filled prints, not the 2024-11 through 2026-10 window. Binance's public force-order endpoint returned 404, and `data.binance.vision` has no `liquidationSnapshot` for `BTCUSDT` on 2024-11-05.
- OKX contract taker volume (`/api/v5/rubik/stat/taker-volume-contract`, `BTC-USDT-SWAP`, `1H`) is non-zero only from about `2026-08-27T04:00:00Z` forward. Earlier `begin`/`end` ranges come back as zeros. Spot taker volume is about 30 days and returns `50030` outside that. Open-interest history for 2024-11 and 2025-06 came back empty. None of those series covers the train window.

## What was stored

The series that does cover the window is Binance USD-M `BTCUSDT` 30-minute klines from `data.binance.vision`. Each bar includes taker-buy base volume. Taker-sell volume is total volume minus taker-buy volume. That is aggressor volume from the matching engine, not a swing high or low derived from OHLC.

It is still not an OKX book and not a liquidation print. The price path stays OKX spot. The flow is Binance USDT-M, which is where perp liquidations actually trade, on a different venue.

Rows go to ClickHouse `market.taker_flow` (`sql/clickhouse/003_taker_flow.sql`):

| Column | Value |
| --- | --- |
| symbol | `BTC-USDT` (same key as `market.bars`) |
| source | `binance-um` |
| freq | `30m` |
| exchange_ts | bar open, UTC |
| buy_volume / sell_volume | taker buy, and volume minus taker buy |

This run wrote 34,504 bars, `2024-11-05T04:00:00Z` through `2026-10-24T23:30:00Z`. The OKX 30-minute query for the same `months=24` clock window has 34,556 bars ending `2026-10-26T01:30:00Z`. 34,504 opens match exactly. The other 52 are `2026-10-25` and `2026-10-26`, whose daily zip files were not published yet. A sweep on a bar with no flow row is dropped.

Reload:

```shell
make up && make bootstrap
uv run dataprice flow-backfill --symbol BTC-USDT --months 24
```

`make flow-backfill` is the same command. No API key. ReplacingMergeTree overwrites the same open time on a second load. SHA-256 is checked when Binance publishes a `.CHECKSUM` beside the zip.

## How the filter uses it

Structural pools are unchanged. Candidate P still requires an equal-high or equal-low sweep, then a with-structure FVG or order block. `flow_params()` adds one gate on the sweep bar, before the entry is built:

- Imbalance is `(taker buy - taker sell) / (taker buy + taker sell)`.
- A sell-side sweep (lows taken, long setup) needs imbalance `<= -threshold`, so sellers were the aggressor.
- A buy-side sweep needs imbalance `>= threshold`.
- Zero volume, or no row for that open time, does not confirm.

`min_flow_imbalance is None` leaves the structural path alone. The check is a pure function in `dataprice.strategy.flow` and does not open ClickHouse. `generate_signals(..., flow=...)` and `run_backtest(..., flow=...)` take the bars in memory. `dataprice backtest --flow` reads `market.taker_flow`.

## Thresholds, chosen on train

Three thresholds, same costs, train entries only. A setting needed at least 30 train trades to be eligible. Holdout was not used for that choice.

| Train threshold | Trades | PnL | Win rate | Profit factor | Avg R | Max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Structural P, no flow gate | 97 | +156.67 | 44.3% | 1.47 | +0.59 | 43.02 |
| Agreeing aggressor (`threshold 0`) | 32 | -9.93 | 34.4% | 0.93 | +0.03 | 55.71 |
| Imbalance at least 0.10 | 12 | +38.65 | 50.0% | 2.14 | +0.60 | 12.23 |
| Imbalance at least 0.20 | 1 | -2.45 | 0% | 0.00 | -1.15 | 2.45 |

Only threshold 0 had enough train trades. It does not beat P on train. The 0.10 row has a higher profit factor and a tiny sample, so it stays a sensitivity, not the candidate. `flow_params()` therefore uses threshold 0: on the sweep bar, taker volume on the side being taken is at least half of that bar's aggressor volume.

## Selected flow gate versus structural P

Both use 5 bps + 2 bps. The structural row is the same candidate as the earlier write-up (160 trades, +63.89 on the full window). The flow row is `flow_params()`.

| Sample | Trades | Realized PnL | Win rate | Profit factor | Avg R | Max drawdown | Avg hold |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| P train | 97 | +156.67 | 44.3% | 1.47 | +0.59 | 43.02 | 12.7 bars |
| P holdout | 63 | -92.78 | 28.6% | 0.54 | -0.31 | 99.65 | 11.9 bars |
| P full window | 160 | +63.89 | 38.1% | 1.12 | +0.24 | 110.52 | 12.4 bars |
| Flow train | 32 | -9.93 | 34.4% | 0.93 | +0.03 | 55.71 | 15.4 bars |
| Flow holdout | 24 | -21.19 | 33.3% | 0.68 | -0.19 | 24.07 | 9.0 bars |
| Flow full window | 56 | -31.12 | 33.9% | 0.85 | -0.06 | 55.71 | 12.7 bars |

P still shows 174 signals and the flow gate 61. Full-window flow sides: 22 buys, +22.39; 34 sells, -53.51.

Requiring real aggressor flow cut the trade count by about two thirds and removed P's train profit. The holdout dollar loss is smaller because there are fewer trades, not because the average trade improved (holdout average R is -0.19 versus -0.31 for P). Neither version is a confirmed edge on entries from 2026-03-01. P's full-window +63.89 is still the train sample plus a losing holdout. The flow version does not have that full-window profit at all.

## Reproduce

```shell
make up && make bootstrap
uv run dataprice backfill --symbol BTC-USDT --months 24
uv run dataprice flow-backfill --symbol BTC-USDT --months 24
uv run dataprice backtest --symbol BTC-USDT --months 24
uv run dataprice backtest --flow --symbol BTC-USDT --months 24
make test
```

`make backtest` remains structural P. `--flow` is the aggressor gate above. `--baseline` does not take `--flow`.
