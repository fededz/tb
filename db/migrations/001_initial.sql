-- 001_initial.sql — Schema inicial del sistema de trading

CREATE TABLE IF NOT EXISTS orders (
    id              SERIAL PRIMARY KEY,
    external_id     VARCHAR(100),
    strategy        VARCHAR(50) NOT NULL,
    ticker          VARCHAR(20) NOT NULL,
    tipo            VARCHAR(30) NOT NULL,
    operacion       VARCHAR(10) NOT NULL,
    cantidad        NUMERIC(18,4) NOT NULL,
    precio          NUMERIC(18,4),
    plazo           VARCHAR(20) NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    dry_run         BOOLEAN DEFAULT false,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    executed_at     TIMESTAMPTZ,
    error_msg       TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id              SERIAL PRIMARY KEY,
    ticker          VARCHAR(20) NOT NULL,
    tipo            VARCHAR(30) NOT NULL,
    cantidad        NUMERIC(18,4) NOT NULL,
    precio_entrada  NUMERIC(18,4) NOT NULL,
    strategy        VARCHAR(50) NOT NULL,
    opened_at       TIMESTAMPTZ DEFAULT NOW(),
    closed_at       TIMESTAMPTZ,
    pnl             NUMERIC(18,4)
);

CREATE TABLE IF NOT EXISTS pnl_diario (
    fecha           DATE PRIMARY KEY,
    pnl_ars         NUMERIC(18,4),
    pnl_usd         NUMERIC(18,4),
    capital_inicio  NUMERIC(18,4),
    capital_fin     NUMERIC(18,4),
    trades          INTEGER
);

CREATE TABLE IF NOT EXISTS market_data_cache (
    ticker          VARCHAR(20) NOT NULL,
    tipo            VARCHAR(30) NOT NULL,
    plazo           VARCHAR(20) NOT NULL,
    fecha           DATE NOT NULL,
    open            NUMERIC(18,4),
    high            NUMERIC(18,4),
    low             NUMERIC(18,4),
    close           NUMERIC(18,4),
    volume          NUMERIC(18,4),
    PRIMARY KEY (ticker, tipo, plazo, fecha)
);

CREATE TABLE IF NOT EXISTS risk_profile (
    id              SERIAL PRIMARY KEY,
    nombre          VARCHAR(20) NOT NULL,
    benchmark_pct   NUMERIC(5,4) NOT NULL,
    carry_pct       NUMERIC(5,4) NOT NULL,
    tendencia_pct   NUMERIC(5,4) NOT NULL,
    relative_value_pct NUMERIC(5,4) NOT NULL,
    spread_minimo_benchmark NUMERIC(5,4) NOT NULL,
    max_drawdown_diario_pct NUMERIC(5,4) NOT NULL,
    activo          BOOLEAN DEFAULT false,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_by      VARCHAR(50)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_risk_profile_activo
    ON risk_profile(activo) WHERE activo = true;

CREATE TABLE IF NOT EXISTS market_context (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL,
    riesgo_macro    VARCHAR(10) NOT NULL,
    sentimiento     NUMERIC(4,2),
    sizing_mult     NUMERIC(4,2) DEFAULT 1.0,
    eventos         JSONB,
    estrategias_pausadas JSONB,
    resumen         TEXT,
    fuentes_count   INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_market_context_timestamp
    ON market_context(timestamp DESC);

-- Seed risk profiles
INSERT INTO risk_profile (nombre, benchmark_pct, carry_pct, tendencia_pct, relative_value_pct, spread_minimo_benchmark, max_drawdown_diario_pct, activo, updated_by)
VALUES
    ('conservador', 0.6000, 0.2500, 0.1000, 0.0500, 0.1500, 0.0100, false, 'sistema'),
    ('moderado',    0.4000, 0.2500, 0.2500, 0.1000, 0.1000, 0.0300, true,  'sistema'),
    ('agresivo',    0.2000, 0.2500, 0.3000, 0.2500, 0.0500, 0.0500, false, 'sistema')
ON CONFLICT DO NOTHING;
