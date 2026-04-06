-- 002_research_feedback.sql — Tablas para feedback loop del research agent

CREATE TABLE IF NOT EXISTS source_predictions (
    id                  SERIAL PRIMARY KEY,
    timestamp_evento    TIMESTAMPTZ NOT NULL,
    fuente              VARCHAR(100) NOT NULL,
    username            VARCHAR(50),
    contenido_resumen   TEXT,
    activos_afectados   JSONB,
    impacto_predicho    NUMERIC(4,2),
    ventana_min         INTEGER NOT NULL,
    medicion_schedulada TIMESTAMPTZ,
    medida              BOOLEAN DEFAULT false,
    contaminada         BOOLEAN DEFAULT false,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_source_predictions_pending
    ON source_predictions(medicion_schedulada) WHERE medida = false;

CREATE TABLE IF NOT EXISTS source_accuracy (
    id                  SERIAL PRIMARY KEY,
    prediction_id       INTEGER REFERENCES source_predictions(id),
    username            VARCHAR(50) NOT NULL,
    impacto_predicho    NUMERIC(4,2),
    impacto_real        NUMERIC(4,2),
    acierto_direccion   BOOLEAN,
    error_magnitud      NUMERIC(4,2),
    contaminada         BOOLEAN DEFAULT false,
    medido_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS source_weights_history (
    id          SERIAL PRIMARY KEY,
    username    VARCHAR(50) NOT NULL,
    peso_anterior NUMERIC(4,2),
    peso_nuevo  NUMERIC(4,2),
    win_rate    NUMERIC(4,2),
    n_eventos   INTEGER,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
