# TradeData

BTC-USDT 量化策略研究。行情进 ClickHouse，因子面板用 ArcticDB，账本用 PostgreSQL。当前锁定的策略是一击必杀 v0.1：宏观门过滤环境，日线定偏向，4H 回踩 ICT 区域，逐仓控制仓位。

另一条研究是 TPO 价值区加当日 VWAP 收回，走 `dataprice backtest`，规则在 `doc/tpo_vwap_mikilab.md`。两条回测互不调用。

## 策略架构

一击必杀的规则在 `doc/sniper_yiji_bisha.md`。参数锁在 `sniper.evaluated_params()`。

| 层 | 作用 | 锁死值 |
| --- | --- | --- |
| L0 MacroGate | DXY、10Y TIPS、HY OAS、VIX 等权。因子不高于自己最近 63 个收盘的中位数记 1。分数达到 θ 才允许开新仓。门关掉时不平掉已有仓 | θ = 0.55，版本 v0.1 |
| L1 日线偏向 | 已收完的 UTC 日。做多要 HH/HL 或收盘站上前 20 根高点，并且收盘在 EMA50 之上。做空对称。当天收盘从下一日的 4H 才生效 | EMA50，结构 20 日，分形左右各 2 根 |
| L2 ICT 区域回踩 | 4H 回踩订单块或公允价值缺口。影线进区、不破远端、收盘回到有利一侧。止损在这根影线外 | 进场区只有这两类 |
| L3 风险管理 | 逐仓。保证金 = 权益 × 0.20，杠杆 20，名义大约是权益的 4 倍。目标按账户 R = 3 来解，解不出来就跳过 | `margin_mode=isolated`，`margin_fraction=0.20`，`leverage=20`，`min_account_rr=3.0` |

日线 EMA 只参与 L1。L2 不使用均线，也不使用 ATR。摆动高低点只参与日线偏向。

默认权益 10000，每边手续费 5 个基点，每边不利滑点 2 个基点。命令行只覆盖手续费和滑点。

## 回测方法

`dataprice sniper-backtest` 跑的是顺序单仓。同时只持有一笔，持仓期间的新回踩不进成交，平仓之后才看后面的 4H。胜率和净盈亏是账户路径，信号挤在一起时后面的信号不会出现。

独立信号用同一套进场、止损和目标，但每根合格回踩各自走到出场，不因为前面还有仓而跳过。它描述信号本身的分布。v0.1 没有第二条命令输出独立信号，避免把信号统计当成可以下单的权益曲线。两条结果的差别来自持仓占用。

回测从下一根 4H 才看出场。同一根先碰止损就按止损走。数据结束时还敞着的仓按最后一根收盘记 `end`，这是收尾，不是策略出场。没有资金费、维持保证金和强平。逐仓 20 倍时，价格逆向大约 5% 就接近交易所强平距离；影线止损可以比这更远，实盘会在模型止损之前被平掉。

仓库里没有附带 sniper 的历史成交表。净盈亏以本机跑完 `sniper-backtest` 的输出为准。单元测试不连接数据库。

## 数据

存储只绑在本机。连接参数放在仓库根目录的 `.env`，模板是 `.env.example`。`.env` 不进版本库。历史 K 线走交易所公开接口，拉行情不需要密钥。

```shell
make install
make up
make bootstrap
make health
make test
```

| 命令 | 作用 |
| --- | --- |
| `make backfill` | 从 OKX 拉 BTC-USDT 的 30m 和 60m，写入 `market.bars` |
| `make binance-backfill` | 从 Binance 现货拉 BTCUSDT 5m，并合成 10m |
| `make flow-backfill` | 从 Binance USD-M 拉 30m 主动买卖量 |
| `make backtest` | 用已入库 K 线跑 TPO + 当日 VWAP |
| `make sniper-backtest` | 用 60m 和宏观门跑一击必杀 |
| `make sniper-chart` | 提示如何把同一笔顺序单仓画成 HTML |
| `make sniper-web` | 本机打开图表页面，结果写到 `var/sniper-web` |
| `make down` | 停止容器，数据卷保留 |

一击必杀要先有 60 分钟 K 线，再有宏观日频：

```shell
uv run dataprice backfill --symbol BTC-USDT --freq 60m --months 24
uv run dataprice macro-backfill --csv macro.csv
uv run dataprice sniper-backtest --symbol BTC-USDT --months 24 --fee-bps 5 --slippage-bps 2
uv run dataprice sniper-chart --symbol BTC-USDT --months 24 --limit 400 --output /tmp/btc.sniper.html
```

`sniper-chart` 用同一次顺序单仓画 4H K 线和日线偏向。时间窗、`--limit` 和 HTML 路径可以改。策略参数不能从这条命令改。生成的 HTML 不要提交。用法在 `doc/sniper_chart.md`。

本机页面：

```shell
uv run dataprice sniper-web
```

浏览器打开 http://127.0.0.1:8765/ 。每次结果写到 `var/sniper-web/<运行编号>/`，里面是 `chart.html`、`trades.json`、`trades.csv`、`params.json` 和 `summary.json`。

宏观 CSV 三列：`series_id,obs_date,close`。序列名是 `dxy`、`tips_10y`、`hy_oas`、`vix`，日期是工作日。本机能访问 FRED 和 Yahoo 时可以 `uv run dataprice macro-backfill --fetch`。10Y TIPS 是 FRED `DFII10`，HY OAS 是 FRED `BAMLH0A0HYM2`，DXY 是 Yahoo `DX-Y.NYB`，VIX 是 Yahoo `^VIX`。拉不到就改 CSV。

60 分钟在内存里收成 UTC 对齐的 4H 和日线，缺一根就不合成那一根。K 线多取回测起点前 120 天，宏观多取 400 天，用来暖日线 EMA 和 63 日宏观中位数。成交只从 `--months` 窗口里记。

## 代码分层

- `dataprice.hotpath`：内存里的持仓、额度和出站队列。判断没有数据库调用。
- `dataprice.session`：开盘前把 PostgreSQL 快照载入内存，另一个线程把成交写回账本。
- `dataprice.market`：ClickHouse 的 K 线和逐笔。
- `dataprice.strategy`：TPO + VWAP，以及一击必杀。信号不读库。
- `dataprice.factors`：ArcticDB 因子面板。`write` 返回版本号，回测用这个版本号或写入时刻做 as-of。
- `dataprice.ledger`：账户、流水、持仓、限额、订单和成交。

行情表按 Asia/Shanghai 的月份分区。交易所时间、本地接收时间、入库时间分开存。数据库选型写在 `doc/量化系统数据库选型.md`。

## 维护

机器重启后，两个数据库容器会自己起来。行情进程不会跟着起来，需要再执行一次 `make feed`，新价格才会继续写入。ClickHouse 只监听本机端口。
