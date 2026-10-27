.DEFAULT_GOAL := help

.PHONY: help install up down bootstrap smoke feed backfill flow-backfill binance-backfill backtest test health

help:
	@printf '%s\n' \
		'make install    安装 Python 依赖' \
		'make up         启动 ClickHouse 和 PostgreSQL' \
		'make bootstrap  建表并创建 ArcticDB library' \
		'make health     检查三个存储' \
		'make smoke      对三个存储做一次读写验收' \
		'make feed       订阅 OKX books5，把买卖一价写入 ClickHouse' \
		'make backfill   从 OKX 拉 BTC-USDT 30m/60m 历史 K 线写入 ClickHouse' \
		'make flow-backfill  从 Binance USD-M 拉 30m 主动买卖量写入 ClickHouse' \
		'make binance-backfill  从 Binance 现货拉 BTCUSDT 5m，并合成 10m' \
		'make backtest   用已入库 K 线跑 ICT+SMC 回测' \
		'make test       运行不依赖数据库的单元测试' \
		'make down       停止容器，保留数据卷'

install:
	uv sync

up:
	docker compose up -d --wait

down:
	docker compose down

bootstrap:
	uv run dataprice bootstrap

health:
	uv run dataprice health

smoke:
	uv run dataprice smoke

feed:
	uv run dataprice feed

backfill:
	uv run dataprice backfill

flow-backfill:
	uv run dataprice flow-backfill

binance-backfill:
	uv run dataprice binance-backfill --symbol BTCUSDT --months 36 --freq 5m

backtest:
	uv run dataprice backtest

test:
	uv run pytest
