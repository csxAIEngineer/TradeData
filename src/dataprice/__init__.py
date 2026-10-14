"""量化数据平面。

层边界对应《量化系统数据库选型》方案 C：

- L0 ``dataprice.hotpath``：内存状态。下单判断不许访问数据库。
- L2 ``dataprice.market``：ClickHouse，分钟/日 K 与逐笔的批量历史扫描。
- L2 ``dataprice.factors``：ArcticDB，因子面板和 point-in-time 版本。
- L3 ``dataprice.ledger``：PostgreSQL，账户、资金、持仓、订单、成交。
- ``dataprice.session``：启动时把账本载入 L0，盘后把 L0 的出站事件落账。

L1 流计算和 L4 对象存储归档不在这套骨架里。
"""

__version__ = "0.1.0"
