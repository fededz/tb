"""Scheduler de estrategias de trading con APScheduler.

Configura y ejecuta todos los jobs del sistema: estrategias de trading,
heartbeat, y actualizaciones del research agent, usando CronTrigger
con timezone de Argentina.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config

logger = structlog.get_logger(__name__)

TIMEZONE = "America/Argentina/Buenos_Aires"

# Definicion de jobs de estrategias segun CLAUDE.md
STRATEGY_JOBS: list[dict[str, str]] = [
    {"strategy": "mean_reversion", "minute": "*/5", "hour": "10-16", "day_of_week": "mon-fri"},
    {"strategy": "carry_futuros", "minute": "30", "hour": "17", "day_of_week": "mon-fri"},
    {"strategy": "carry_bonos", "minute": "35", "hour": "17", "day_of_week": "mon-fri"},
    {"strategy": "trend_following", "minute": "40", "hour": "17", "day_of_week": "mon-fri"},
    {"strategy": "pares", "minute": "45", "hour": "17", "day_of_week": "mon-fri"},
    {"strategy": "momentum_acciones", "minute": "10", "hour": "10", "day_of_week": "mon"},
]


class StrategyProtocol(Protocol):
    """Protocolo minimo que debe cumplir una estrategia para ser scheduleable."""

    name: str

    def run(self) -> None: ...


class TradingScheduler:
    """Scheduler principal del sistema de trading.

    Administra todos los jobs periodicos: ejecucion de estrategias,
    heartbeat de monitoring, y actualizaciones del research agent.
    Usa APScheduler con BackgroundScheduler y CronTrigger.
    """

    def __init__(
        self,
        strategies: dict[str, StrategyProtocol],
        research_updater: Callable[[], None] | None = None,
        heartbeat_fn: Callable[[], None] | None = None,
        feedback_process_fn: Callable[[], None] | None = None,
        feedback_recalc_fn: Callable[[], None] | None = None,
    ) -> None:
        """Inicializa el scheduler con las estrategias y funciones auxiliares.

        Args:
            strategies: Diccionario nombre -> instancia de estrategia.
            research_updater: Funcion que actualiza el contexto de mercado del research agent.
            heartbeat_fn: Funcion que envia el heartbeat de monitoring.
            feedback_process_fn: Funcion que procesa mediciones pendientes del feedback engine.
            feedback_recalc_fn: Funcion que recalcula pesos de fuentes (semanal).
        """
        self._strategies = strategies
        self._research_updater = research_updater
        self._heartbeat_fn = heartbeat_fn
        self._feedback_process_fn = feedback_process_fn
        self._feedback_recalc_fn = feedback_recalc_fn
        self._scheduler = BackgroundScheduler(timezone=TIMEZONE)

    def _run_strategy(self, strategy_name: str) -> None:
        """Ejecuta una estrategia envuelta en try/except para nunca crashear el scheduler.

        Args:
            strategy_name: Nombre de la estrategia a ejecutar.
        """
        strategy = self._strategies.get(strategy_name)
        if strategy is None:
            logger.warning("estrategia_no_encontrada", strategy=strategy_name)
            return

        logger.info("estrategia_ejecutando", strategy=strategy_name)
        try:
            strategy.run()
            logger.info("estrategia_completada", strategy=strategy_name)
        except Exception:
            logger.exception("estrategia_error", strategy=strategy_name)

    def _run_heartbeat(self) -> None:
        """Ejecuta el heartbeat envuelto en try/except."""
        if self._heartbeat_fn is None:
            return

        try:
            self._heartbeat_fn()
            logger.debug("heartbeat_enviado")
        except Exception:
            logger.exception("heartbeat_error")

    def _run_research_update(self) -> None:
        """Ejecuta la actualizacion del research agent envuelta en try/except."""
        if self._research_updater is None:
            return

        try:
            self._research_updater()
            logger.info("research_actualizado")
        except Exception:
            logger.exception("research_update_error")

    def _run_feedback_process(self) -> None:
        """Procesa mediciones pendientes del feedback engine."""
        if self._feedback_process_fn is None:
            return

        try:
            self._feedback_process_fn()
            logger.debug("feedback_mediciones_procesadas")
        except Exception:
            logger.exception("feedback_process_error")

    def _run_feedback_recalc(self) -> None:
        """Recalcula pesos de fuentes basado en track record."""
        if self._feedback_recalc_fn is None:
            return

        try:
            self._feedback_recalc_fn()
            logger.info("feedback_pesos_recalculados")
        except Exception:
            logger.exception("feedback_recalc_error")

    def start(self) -> None:
        """Configura todos los jobs con CronTrigger e inicia el scheduler.

        Jobs configurados:
        - Estrategias de trading segun STRATEGY_JOBS
        - Heartbeat cada 30 minutos
        - Research update segun intervalos de config (mas frecuente en horario de mercado)
        """
        # --- Jobs de estrategias ---
        for job_def in STRATEGY_JOBS:
            strategy_name = job_def["strategy"]

            if strategy_name not in self._strategies:
                logger.warning(
                    "estrategia_no_registrada_skip",
                    strategy=strategy_name,
                )
                continue

            trigger = CronTrigger(
                minute=job_def["minute"],
                hour=job_def["hour"],
                day_of_week=job_def["day_of_week"],
                timezone=TIMEZONE,
            )
            self._scheduler.add_job(
                self._run_strategy,
                trigger=trigger,
                args=[strategy_name],
                id=f"strategy_{strategy_name}",
                name=f"Estrategia: {strategy_name}",
                replace_existing=True,
                misfire_grace_time=60,
            )
            logger.info(
                "job_estrategia_registrado",
                strategy=strategy_name,
                cron=f"{job_def['minute']} {job_def['hour']} * * {job_def['day_of_week']}",
            )

        # --- Heartbeat cada 30 minutos ---
        if self._heartbeat_fn is not None:
            heartbeat_trigger = CronTrigger(
                minute="*/30",
                timezone=TIMEZONE,
            )
            self._scheduler.add_job(
                self._run_heartbeat,
                trigger=heartbeat_trigger,
                id="heartbeat",
                name="Heartbeat monitor",
                replace_existing=True,
                misfire_grace_time=120,
            )
            logger.info("job_heartbeat_registrado", cron="*/30 * * * *")

        # --- Research update ---
        if self._research_updater is not None:
            # Durante horario de mercado: intervalo mas frecuente
            research_market_trigger = CronTrigger(
                minute=f"*/{config.RESEARCH_INTERVAL_MIN_MARKET_HOURS}",
                hour=f"{config.ROFEX_OPEN_HOUR}-{config.ROFEX_CLOSE_HOUR - 1}",
                day_of_week="mon-fri",
                timezone=TIMEZONE,
            )
            self._scheduler.add_job(
                self._run_research_update,
                trigger=research_market_trigger,
                id="research_update_market",
                name="Research update (horario mercado)",
                replace_existing=True,
                misfire_grace_time=120,
            )

            # Fuera de horario de mercado: intervalo normal
            research_off_trigger = CronTrigger(
                minute=f"*/{config.RESEARCH_INTERVAL_MIN}",
                hour=f"0-{config.ROFEX_OPEN_HOUR - 1},{config.ROFEX_CLOSE_HOUR}-23",
                day_of_week="mon-fri",
                timezone=TIMEZONE,
            )
            self._scheduler.add_job(
                self._run_research_update,
                trigger=research_off_trigger,
                id="research_update_offhours",
                name="Research update (fuera de horario)",
                replace_existing=True,
                misfire_grace_time=300,
            )
            logger.info(
                "job_research_registrado",
                intervalo_mercado_min=config.RESEARCH_INTERVAL_MIN_MARKET_HOURS,
                intervalo_fuera_min=config.RESEARCH_INTERVAL_MIN,
            )

        # --- Feedback: procesar mediciones pendientes cada 5 min en horario de mercado ---
        if self._feedback_process_fn is not None:
            feedback_trigger = CronTrigger(
                minute="*/5",
                hour=f"{config.ROFEX_OPEN_HOUR}-{config.ROFEX_CLOSE_HOUR}",
                day_of_week="mon-fri",
                timezone=TIMEZONE,
            )
            self._scheduler.add_job(
                self._run_feedback_process,
                trigger=feedback_trigger,
                id="feedback_process",
                name="Feedback: procesar mediciones",
                replace_existing=True,
                misfire_grace_time=120,
            )
            logger.info("job_feedback_process_registrado", cron="*/5 10-17 * * mon-fri")

        # --- Feedback: recalcular pesos cada lunes a las 9:00 ---
        if self._feedback_recalc_fn is not None:
            recalc_trigger = CronTrigger(
                minute="0",
                hour="9",
                day_of_week="mon",
                timezone=TIMEZONE,
            )
            self._scheduler.add_job(
                self._run_feedback_recalc,
                trigger=recalc_trigger,
                id="feedback_recalc_weights",
                name="Feedback: recalcular pesos semanal",
                replace_existing=True,
                misfire_grace_time=3600,
            )
            logger.info("job_feedback_recalc_registrado", cron="0 9 * * mon")

        self._scheduler.start()
        logger.info(
            "scheduler_iniciado",
            jobs_count=len(self._scheduler.get_jobs()),
        )

    def stop(self) -> None:
        """Detiene el scheduler de forma ordenada, esperando que los jobs en curso terminen."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)
            logger.info("scheduler_detenido")
        else:
            logger.warning("scheduler_no_estaba_corriendo")

    def get_jobs(self) -> list[dict[str, Any]]:
        """Retorna informacion de todos los jobs registrados.

        Returns:
            Lista de dicts con id, name, next_run_time de cada job.
        """
        jobs = []
        for job in self._scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run_time": str(job.next_run_time) if job.next_run_time else None,
            })
        return jobs
