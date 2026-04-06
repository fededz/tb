"""Punto de entrada principal del sistema de trading algoritmico."""

import logging
import signal
import sys
import time

import structlog

from config import (
    PPI_SANDBOX, PERFIL_INICIAL, RSS_FEEDS, TWITTER_ACCOUNTS, NITTER_BASE_URL,
)
from core.ppi_wrapper import PPIWrapper
from core.order_manager import OrderManager
from core.portfolio import Portfolio
from core.risk_manager import RiskManager
from core.alertas import Alertas
from db.repository import Repository
from market_data.cache import MarketDataCache
from market_data.realtime import RealtimeHandler
from market_data.historical import HistoricalData
from monitoring.heartbeat import HeartbeatMonitor
from research.context import ContextReader
from research.collector import ResearchCollector
from research.rss_reader import RSSReader
from research.twitter_scraper import TwitterScraper
from research.analyzer import ResearchAnalyzer
from research.context import ContextWriter
from scheduler.jobs import TradingScheduler
from strategies.carry_futuros import CarryFuturos
from strategies.carry_bonos import CarryBonos
from strategies.trend_following import TrendFollowing
from strategies.momentum_acciones import MomentumAcciones
from strategies.pares import ParesStrategy
from strategies.mean_reversion import MeanReversionIntraday

import logging
logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

log = structlog.get_logger(__name__)


def main() -> None:
    """Inicializa y arranca todos los componentes del sistema."""
    # --- Advertencia de modo live ---
    if not PPI_SANDBOX:
        log.critical(
            "⚠️  MODO LIVE ACTIVO — ORDENES REALES EN CURSO",
            sandbox=False,
        )

    log.info("Iniciando sistema de trading", sandbox=PPI_SANDBOX)

    # --- Componentes base ---
    repository = Repository()
    alertas = Alertas()
    ppi = PPIWrapper()

    try:
        ppi.connect()
    except Exception:
        log.exception("Error conectando a PPI — abortando")
        alertas.error_conexion("No se pudo conectar a PPI al inicio")
        sys.exit(1)

    # --- Market data ---
    cache = MarketDataCache()
    realtime = RealtimeHandler(ppi, cache)
    historical = HistoricalData(ppi, repository)

    # --- Portfolio y risk ---
    portfolio = Portfolio(repository, ppi)
    portfolio.load_from_db()
    risk_manager = RiskManager(portfolio, repository, alertas)
    order_manager = OrderManager(ppi, repository, alertas)

    # --- Research ---
    rss_reader = RSSReader(feeds=RSS_FEEDS)
    twitter_scraper = TwitterScraper(accounts=TWITTER_ACCOUNTS, nitter_url=NITTER_BASE_URL)
    collector = ResearchCollector(twitter_scraper, rss_reader)
    analyzer = ResearchAnalyzer()
    context_writer = ContextWriter(repository)
    context_reader = ContextReader(repository)

    # --- Feedback engine ---
    from research.feedback import FeedbackEngine
    feedback_engine = FeedbackEngine(repository, ppi)

    def research_update() -> None:
        """Actualiza el contexto de mercado."""
        try:
            noticias = collector.collect_all()
            if noticias:
                contexto = analyzer.analyze(noticias)
                context_writer.save_context(contexto)
                log.info("Contexto de mercado actualizado", fuentes=len(noticias))
        except Exception:
            log.exception("Error actualizando contexto de mercado")

    # --- Estrategias ---
    common_deps = dict(
        ppi=ppi,
        portfolio=portfolio,
        risk_manager=risk_manager,
        order_manager=order_manager,
        repository=repository,
        alertas=alertas,
        historical_data=historical,
    )

    strategies = {
        "carry_futuros": CarryFuturos(**common_deps),
        "carry_bonos": CarryBonos(**common_deps),
        "trend_following": TrendFollowing(**common_deps),
        "momentum_acciones": MomentumAcciones(**common_deps),
        "pares": ParesStrategy(**common_deps),
        "mean_reversion": MeanReversionIntraday(**common_deps),
    }

    # --- Monitoring ---
    heartbeat = HeartbeatMonitor(portfolio, alertas)

    # --- Scheduler ---
    scheduler = TradingScheduler(
        strategies=strategies,
        research_updater=research_update,
        heartbeat_fn=heartbeat.send_heartbeat,
        feedback_process_fn=feedback_engine.process_pending_measurements,
        feedback_recalc_fn=feedback_engine.recalculate_weights,
    )

    # --- Arrancar servicios ---
    realtime.start()
    scheduler.start()

    log.info(
        "Sistema de trading iniciado",
        estrategias=list(strategies.keys()),
        perfil=PERFIL_INICIAL,
        sandbox=PPI_SANDBOX,
    )
    alertas.send(
        f"{'🟡 SANDBOX' if PPI_SANDBOX else '🔴 LIVE'} — "
        f"Sistema iniciado con {len(strategies)} estrategias, perfil {PERFIL_INICIAL}"
    )

    # --- Graceful shutdown ---
    running = True

    def shutdown(signum, frame) -> None:
        nonlocal running
        log.info("Señal de shutdown recibida", signal=signum)
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Deteniendo sistema...")
        scheduler.stop()
        realtime.stop()
        alertas.send("Sistema de trading detenido")
        log.info("Sistema detenido correctamente")


if __name__ == "__main__":
    main()
