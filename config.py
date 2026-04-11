"""Configuracion central del sistema de trading."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


# --- PPI ---
PPI_PUBLIC_KEY: str = os.getenv("PPI_PUBLIC_KEY", "")
PPI_PRIVATE_KEY: str = os.getenv("PPI_PRIVATE_KEY", "")
PPI_ACCOUNT_NUMBER: str = os.getenv("PPI_ACCOUNT_NUMBER", "")
PPI_SANDBOX: bool = os.getenv("PPI_SANDBOX", "true").lower() == "true"
# Cuando DRY_RUN_GLOBAL=true, todas las ordenes se loguean pero no se envian a PPI
DRY_RUN_GLOBAL: bool = os.getenv("DRY_RUN_GLOBAL", "true").lower() == "true"

# --- Database ---
DB_HOST: str = os.getenv("DB_HOST", "localhost")
DB_PORT: int = int(os.getenv("DB_PORT", "5432"))
DB_NAME: str = os.getenv("DB_NAME", "trading")
DB_USER: str = os.getenv("DB_USER", "trading")
DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")

def get_db_url() -> str:
    return f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# --- Telegram ---
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
# Nivel minimo de alerta para enviar por Telegram: off | critica | alta | media | baja
TELEGRAM_MIN_PRIORITY: str = os.getenv("TELEGRAM_MIN_PRIORITY", "media")

# --- Risk ---
MAX_CAPITAL_POR_OPERACION_PCT: float = float(os.getenv("MAX_CAPITAL_POR_OPERACION_PCT", "0.05"))
MAX_POSICIONES_ABIERTAS: int = int(os.getenv("MAX_POSICIONES_ABIERTAS", "10"))
PERFIL_INICIAL: str = os.getenv("PERFIL_INICIAL", "moderado")

# --- Benchmark ---
BENCHMARK_INSTRUMENTO: str = os.getenv("BENCHMARK_INSTRUMENTO", "LECAP")
INFLACION_ANUAL_ESTIMADA: float = float(os.getenv("INFLACION_ANUAL_ESTIMADA", "0.35"))

# --- Research ---
TWITTER_ACCOUNTS: list[str] = [
    a.strip() for a in os.getenv("TWITTER_ACCOUNTS", "").split(",") if a.strip()
]
NITTER_BASE_URL: str = os.getenv("NITTER_BASE_URL", "https://nitter.net")
RSS_FEEDS: list[str] = [
    f.strip() for f in os.getenv("RSS_FEEDS", "").split(",") if f.strip()
]
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
RESEARCH_MODEL: str = os.getenv("RESEARCH_MODEL", "claude-haiku-4-5-20251001")
RESEARCH_INTERVAL_MIN: int = int(os.getenv("RESEARCH_INTERVAL_MIN", "30"))
RESEARCH_INTERVAL_MIN_MARKET_HOURS: int = int(os.getenv("RESEARCH_INTERVAL_MIN_MARKET_HOURS", "15"))

# --- Dashboard ---
DASHBOARD_USER: str = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD: str = os.getenv("DASHBOARD_PASSWORD", "")
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8080"))

# --- Horarios de mercado (hora Argentina, GMT-3) ---
BYMA_OPEN_HOUR: int = 11
BYMA_CLOSE_HOUR: int = 17
ROFEX_OPEN_HOUR: int = 10
ROFEX_CLOSE_HOUR: int = 17

# --- Perfiles de riesgo ---
PERFILES: dict = {
    "conservador": {
        "benchmark_pct": 0.60,
        "carry_pct": 0.25,
        "tendencia_pct": 0.10,
        "relative_value_pct": 0.05,
        "spread_minimo_benchmark": 0.15,
        "max_drawdown_diario_pct": 0.01,
    },
    "moderado": {
        "benchmark_pct": 0.40,
        "carry_pct": 0.25,
        "tendencia_pct": 0.25,
        "relative_value_pct": 0.10,
        "spread_minimo_benchmark": 0.10,
        "max_drawdown_diario_pct": 0.03,
    },
    "agresivo": {
        "benchmark_pct": 0.20,
        "carry_pct": 0.25,
        "tendencia_pct": 0.30,
        "relative_value_pct": 0.25,
        "spread_minimo_benchmark": 0.05,
        "max_drawdown_diario_pct": 0.05,
    },
}
