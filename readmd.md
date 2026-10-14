# Introduction

采用的轻量方案是 ClickHouse 做回测扫描、ArcticDB 做因子面板和版本、PostgreSQL 做账务。我先确认本机 Docker 和 Python，再按这个边界搭服务和代码框架。


| 存储 | 地址 | 库 | 账号 |
| --- | --- | --- | --- |
| PostgreSQL 16 | `127.0.0.1:5432` | ledger | quant / quant_local_dev |
| ClickHouse 24.8 | HTTP `127.0.0.1:8123`，原生 `9000` | market | quant / quant_local_dev |
| ArcticDB | 本机 LMDB `var/arctic` | factor_xs、factor_ts | 无独立进程 |

端口只绑在本机。口令写在 .env，只给本地开发用。ArcticDB 不是数据库服务，数据文件在 var/arctic；以后可以把 ARCTIC_URI 改成 s3://。生产使用 ArcticDB 需要留意它的 BSL 许可证。

验收已经跑过：账本预加载、内存成交、异步落盘一致；ClickHouse 批量写入后纳秒时间戳可以原样读回；ArcticDB 可以用写入时返回的版本号读到改写前的因子。

常用命令：

```shell
make up          # 启动两个数据库
make bootstrap   # 建表，并创建 ArcticDB library
make health
make smoke       # 三个存储各做一轮读写
make test        # 不连数据库的单元测试
make down        # 停止容器，数据卷保留
```

代码按层分开：

dataprice.hotpath：内存里的持仓、额度和出站队列。evaluate / apply_fill 没有数据库调用。
dataprice.session：开盘前把 PostgreSQL 快照载入内存，另一个线程把成交写回账本。
dataprice.market：ClickHouse 的 K 线和逐笔。缓冲满了会拒绝继续堆数据，避免策略线程被落盘堵住。
dataprice.factors：ArcticDB 因子面板。factor_xs 是一个因子、多标的；factor_ts 是一个标的、多因子。write 返回版本号，回测要用这个版本号或写入时刻做 as_of。
dataprice.ledger：账户、资金流水、持仓、风控限额、订单、成交、策略参数和角色。
行情表按 Asia/Shanghai 的月份分区，排序键是周期、标的、交易所时间。交易所时间、本地接收时间、入库时间分开存。财务类因子的索引要用公告变得可知的时间。