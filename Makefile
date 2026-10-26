.DEFAULT_GOAL := help

.PHONY: help install up down bootstrap smoke feed test health

help:
	@printf '%s\n' \
		'make install    安装 Python 依赖' \
		'make up         启动 ClickHouse 和 PostgreSQL' \
		'make bootstrap  建表并创建 ArcticDB library' \
		'make health     检查三个存储' \
		'make smoke      对三个存储做一次读写验收' \
		'make feed       订阅 OKX books5，把买卖一价写入 ClickHouse' \
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

test:
	uv run pytest
