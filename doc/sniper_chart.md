# 一击必杀 K 线与成交图

`dataprice sniper-chart` 把 BTC-USDT 的 4H、UTC 日线和 `sniper-backtest` 的顺序单仓画成一个 HTML。浏览器直接打开，不需要再起服务。

成交来自 `load_stored_sniper_view`，也就是 `sniper-backtest` 那一次 `run_sniper`。持仓期间的新回踩不会另外画成一笔。命令行只改手续费、滑点和看哪一段，不改 v0.1 的策略参数。

```shell
uv run dataprice backfill --symbol BTC-USDT --freq 60m --months 24
uv run dataprice macro-backfill --csv macro.csv
uv run dataprice sniper-chart --symbol BTC-USDT --months 24 --limit 400 --output /tmp/btc.sniper.html
```

`--months` 和回测一样，大约 30 天算一个月，默认 24。也可以改用 `--start` 和 `--end`，时间是 UTC，`--end` 不包含在内。`--start` 和 `--months` 只能给一个。`--limit` 是这段时间里最近的 N 根 4H。某笔的出场如果晚于窗口，图会把 K 线补到出场，补上的部分不再增加入场。

HTML 写到仓库外面，例如 `/tmp/btc.sniper.html`。`*.sniper.html` 已在 `.gitignore` 里。不要把生成的图、行情或 `.env` 提交进来。

## 图上有什么

上图是 4H。K 线有开高低收。`volume` 有大于 0 的值才画成交量，全是 0 就在图例里说明不画。拖拽、滚轮和左上角的时间按钮可以缩放。悬停里的钟点是 UTC。

绿色上三角是多头入场，红色下三角是空头入场，叉是同一笔的出场。实线连入场价和出场价。虚线是止损，点线是目标。悬停可以看到平仓原因（止损、目标、数据结束收尾）、PnL、价格 R 和账户 R。

下图是回测已经合成好的 UTC 日线，不是把 4H 再收成日 K。圆点是这根日 K 收完以后、下一会话才用的偏向。空心菱形是这个 UTC 日的 4H 实际生效的偏向，来自更早已经收完的那根日线。4H 的入场、止损和目标只画在上图。

## 能标和不能标

| 图层 | 来源 | 没有数据时 |
| --- | --- | --- |
| 4H OHLC、日线 OHLC | 同一次回测用的 K 线 | 时间窗里没有 4H 就报错，不画空图 |
| 成交、止损、目标、PnL、R、平仓原因 | 这次顺序单仓的 `SniperTrade` | 窗口里没有成交就写明 0 笔 |
| 成交量 | K 线的 `volume` | 全是 0 就不画，图例说明 |
| 订单块 / 公允价值缺口 | 这笔入场能被同一套 `find_retest` 对上才画 | 对不上就不画，图例说明 |
| L0 GATE=0 阴影 | 送进这次回测的宏观序列或 `factor_macro_gate` | 两个都没有就不画，图例说明 |

灰色竖带表示该 UTC 日是 GATE=0，或者没有生效门。这和回测一样：关门只是不开新仓，不平掉已有的单。
