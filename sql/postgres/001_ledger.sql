-- L3 事务库。账户、资金、持仓、订单、成交、策略参数、权限。
-- 已发布的 001 不要改列；后续变更另加迁移文件。
-- 下单热路径只允许使用启动时载入内存的快照，不允许在下单时查询这些表。

CREATE SCHEMA IF NOT EXISTS ledger;

CREATE TABLE IF NOT EXISTS ledger.schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ledger.accounts (
    account_id  BIGSERIAL PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    broker      TEXT NOT NULL,
    currency    CHAR(3) NOT NULL DEFAULT 'CNY',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT accounts_status_chk CHECK (status IN ('active', 'frozen', 'closed')),
    CONSTRAINT accounts_currency_chk CHECK (currency ~ '^[A-Z]{3}$')
);

CREATE TABLE IF NOT EXISTS ledger.app_users (
    user_id     BIGSERIAL PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT app_users_role_chk CHECK (role IN ('admin', 'trader', 'researcher', 'auditor'))
);

CREATE TABLE IF NOT EXISTS ledger.strategies (
    strategy_id     BIGSERIAL PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    params          JSONB NOT NULL DEFAULT '{}'::jsonb,
    params_version  INTEGER NOT NULL DEFAULT 1,
    is_live         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ledger.strategy_param_history (
    strategy_id     BIGINT NOT NULL REFERENCES ledger.strategies (strategy_id),
    version         INTEGER NOT NULL,
    params          JSONB NOT NULL,
    effective_from  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, version)
);

CREATE TABLE IF NOT EXISTS ledger.cash_balances (
    account_id  BIGINT PRIMARY KEY REFERENCES ledger.accounts (account_id),
    currency    CHAR(3) NOT NULL,
    available   NUMERIC(20, 4) NOT NULL,
    frozen      NUMERIC(20, 4) NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cash_nonneg_chk CHECK (available >= 0 AND frozen >= 0)
);

CREATE TABLE IF NOT EXISTS ledger.cash_journal (
    journal_id      BIGSERIAL PRIMARY KEY,
    account_id      BIGINT NOT NULL REFERENCES ledger.accounts (account_id),
    amount          NUMERIC(20, 4) NOT NULL,
    available_after NUMERIC(20, 4) NOT NULL,
    reason          TEXT NOT NULL,
    ref_type        TEXT,
    ref_id          TEXT,
    ingest_ts_ns    BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS cash_journal_account_idx
    ON ledger.cash_journal (account_id, journal_id);

CREATE TABLE IF NOT EXISTS ledger.positions (
    account_id  BIGINT NOT NULL REFERENCES ledger.accounts (account_id),
    symbol      TEXT NOT NULL,
    qty         NUMERIC(20, 4) NOT NULL,
    avg_cost    NUMERIC(20, 6) NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, symbol)
);

CREATE TABLE IF NOT EXISTS ledger.risk_limits (
    account_id          BIGINT PRIMARY KEY REFERENCES ledger.accounts (account_id),
    halted              BOOLEAN NOT NULL DEFAULT FALSE,
    max_order_notional  NUMERIC(20, 4) NOT NULL,
    max_position_qty    NUMERIC(20, 4),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT risk_notional_chk CHECK (max_order_notional > 0),
    CONSTRAINT risk_pos_chk CHECK (max_position_qty IS NULL OR max_position_qty > 0)
);

CREATE TABLE IF NOT EXISTS ledger.orders (
    order_id          BIGSERIAL PRIMARY KEY,
    client_order_id   TEXT NOT NULL UNIQUE,
    account_id        BIGINT NOT NULL REFERENCES ledger.accounts (account_id),
    strategy_id       BIGINT REFERENCES ledger.strategies (strategy_id),
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    qty               NUMERIC(20, 4) NOT NULL,
    price             NUMERIC(20, 6),
    order_type        TEXT NOT NULL,
    status            TEXT NOT NULL,
    exchange_ts_ns    BIGINT,
    local_recv_ts_ns  BIGINT,
    ingest_ts_ns      BIGINT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT orders_side_chk CHECK (side IN ('buy', 'sell')),
    CONSTRAINT orders_type_chk CHECK (order_type IN ('limit', 'market')),
    CONSTRAINT orders_status_chk CHECK (
        status IN ('new', 'accepted', 'partial', 'filled', 'canceled', 'rejected')
    ),
    CONSTRAINT orders_qty_chk CHECK (qty > 0)
);

CREATE INDEX IF NOT EXISTS orders_account_idx
    ON ledger.orders (account_id, order_id);

CREATE TABLE IF NOT EXISTS ledger.fills (
    fill_id           BIGSERIAL PRIMARY KEY,
    order_id          BIGINT NOT NULL REFERENCES ledger.orders (order_id),
    qty               NUMERIC(20, 4) NOT NULL,
    price             NUMERIC(20, 6) NOT NULL,
    fee               NUMERIC(20, 6) NOT NULL DEFAULT 0,
    exchange_ts_ns    BIGINT,
    local_recv_ts_ns  BIGINT,
    ingest_ts_ns      BIGINT NOT NULL,
    CONSTRAINT fills_qty_chk CHECK (qty > 0),
    CONSTRAINT fills_price_chk CHECK (price > 0),
    CONSTRAINT fills_fee_chk CHECK (fee >= 0)
);

CREATE INDEX IF NOT EXISTS fills_order_idx
    ON ledger.fills (order_id);

INSERT INTO ledger.schema_migrations (version)
VALUES ('001_ledger')
ON CONFLICT (version) DO NOTHING;
