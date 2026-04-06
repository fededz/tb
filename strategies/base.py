"""Clase base abstracta para todas las estrategias de trading.

Define la interfaz comun, el flujo de ejecucion (generate_signals -> validate
-> execute -> alert) y la integracion con el contexto de mercado del research
agent.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, time, timezone, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from config import (
    BYMA_CLOSE_HOUR,
    BYMA_OPEN_HOUR,
    ROFEX_CLOSE_HOUR,
    ROFEX_OPEN_HOUR,
)

if TYPE_CHECKING:
    from core.alertas import Alertas
    from core.order_manager import OrderManager
    from core.ppi_wrapper import PPIWrapper
    from core.risk_manager import OrderIntent
    from market_data.historical import HistoricalData

logger = structlog.get_logger(__name__)

# Timezone Argentina (UTC-3)
TZ_AR = timezone(timedelta(hours=-3))


@dataclass
class Signal:
    """Senal de trading generada por una estrategia.

    Attributes:
        strategy: Nombre de la estrategia que genero la senal.
        ticker: Simbolo del instrumento.
        tipo: Tipo de instrumento (Acciones, Bonos, Futuros, etc.).
        operacion: Direccion de la operacion: "COMPRA" o "VENTA".
        cantidad: Cantidad de unidades / contratos.
        precio: Precio limite. None indica precio de mercado.
        plazo: Plazo de liquidacion (INMEDIATA, A-24HS, A-48HS, etc.).
        motivo: Justificacion textual de la senal.
    """

    strategy: str
    ticker: str
    tipo: str
    operacion: str
    cantidad: float
    precio: float | None
    plazo: str
    motivo: str


class Strategy(ABC):
    """Clase base abstracta para estrategias de trading.

    Implementa el flujo comun de ejecucion:
    1. Verificar si la estrategia debe correr (horario, dia, contexto).
    2. Generar senales via ``generate_signals``.
    3. Validar cada senal con el risk manager.
    4. Ejecutar ordenes validas via el order manager.
    5. Enviar alertas por Telegram.

    Las subclases deben implementar ``generate_signals`` con la logica
    especifica de cada estrategia.
    """

    name: str = "base"
    frecuencia: str = "diaria"
    instrumentos: list[str] = []

    def __init__(
        self,
        ppi: PPIWrapper,
        portfolio: Any,
        risk_manager: Any,
        order_manager: Any,
        repository: Any,
        alertas: Alertas,
        historical_data: HistoricalData,
    ) -> None:
        """Inicializa la estrategia con sus dependencias.

        Args:
            ppi: Wrapper de la API de PPI para datos de mercado.
            portfolio: Gestor de portafolio (posiciones, capital).
            risk_manager: Validador de riesgo pre-orden.
            order_manager: Gestor de envio de ordenes (budget -> confirm).
            repository: Acceso a base de datos.
            alertas: Cliente de alertas Telegram.
            historical_data: Proveedor de datos historicos.
        """
        self.ppi = ppi
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.order_manager = order_manager
        self.repository = repository
        self.alertas = alertas
        self.historical_data = historical_data
        self._log = logger.bind(strategy=self.name)

    # ------------------------------------------------------------------
    # Metodos abstractos
    # ------------------------------------------------------------------

    @abstractmethod
    def generate_signals(self) -> list[Signal]:
        """Genera senales de trading basadas en la logica de la estrategia.

        Returns:
            Lista de senales. Lista vacia si no hay oportunidad.
        """
        ...

    # ------------------------------------------------------------------
    # Verificacion de horario
    # ------------------------------------------------------------------

    def should_run(self) -> bool:
        """Determina si la estrategia debe ejecutarse ahora.

        Verifica dia de la semana (lunes a viernes) y horario de mercado
        segun la frecuencia de la estrategia. Las estrategias intraday
        usan horario de ROFEX; las demas usan horario de BYMA.

        Returns:
            True si se cumplen las condiciones de ejecucion.
        """
        now_ar = datetime.now(TZ_AR)

        # Solo dias habiles (lunes=0 a viernes=4)
        if now_ar.weekday() > 4:
            self._log.info("should_run.fin_de_semana", dia=now_ar.strftime("%A"))
            return False

        hora = now_ar.hour

        if self.frecuencia == "intraday":
            if not (ROFEX_OPEN_HOUR <= hora < ROFEX_CLOSE_HOUR):
                self._log.debug(
                    "should_run.fuera_de_horario_rofex",
                    hora=hora,
                    apertura=ROFEX_OPEN_HOUR,
                    cierre=ROFEX_CLOSE_HOUR,
                )
                return False
        elif self.frecuencia == "semanal":
            # Solo lunes
            if now_ar.weekday() != 0:
                self._log.debug("should_run.no_es_lunes", dia=now_ar.strftime("%A"))
                return False
        elif self.frecuencia in ("diaria", "mensual"):
            if not (BYMA_OPEN_HOUR <= hora < BYMA_CLOSE_HOUR):
                self._log.debug(
                    "should_run.fuera_de_horario_byma",
                    hora=hora,
                    apertura=BYMA_OPEN_HOUR,
                    cierre=BYMA_CLOSE_HOUR,
                )
                return False

        return True

    # ------------------------------------------------------------------
    # Contexto de mercado (research agent)
    # ------------------------------------------------------------------

    def _get_market_context(self) -> dict[str, Any] | None:
        """Obtiene el contexto de mercado mas reciente del research agent.

        Returns:
            Dict con el contexto o None si no hay contexto disponible.
        """
        try:
            ctx = self.repository.get_latest_market_context()
            return ctx
        except Exception:
            self._log.exception("error_obteniendo_contexto_mercado")
            return None

    def _is_paused_by_research(self, context: dict[str, Any] | None) -> bool:
        """Verifica si esta estrategia fue pausada por el research agent.

        Args:
            context: Contexto de mercado actual.

        Returns:
            True si la estrategia esta pausada.
        """
        if context is None:
            return False

        riesgo = context.get("riesgo_macro", "bajo")
        if riesgo == "critico":
            self._log.warning(
                "estrategia_pausada.riesgo_critico",
                riesgo_macro=riesgo,
            )
            return True

        pausadas = context.get("estrategias_pausadas") or []
        if self.name in pausadas:
            self._log.warning(
                "estrategia_pausada.por_research",
                estrategias_pausadas=pausadas,
            )
            return True

        return False

    def _get_sizing_multiplier(self, context: dict[str, Any] | None) -> float:
        """Obtiene el multiplicador de sizing del contexto de mercado.

        Args:
            context: Contexto de mercado actual.

        Returns:
            Multiplicador entre 0.0 y 1.0. Default 1.0 si no hay contexto.
        """
        if context is None:
            return 1.0
        return float(context.get("sizing_mult", 1.0))

    # ------------------------------------------------------------------
    # Flujo de ejecucion
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Ejecuta el ciclo completo de la estrategia.

        Flujo:
        1. Verifica si debe correr (should_run).
        2. Consulta contexto de mercado y verifica pausas.
        3. Genera senales.
        4. Aplica sizing_multiplier a las cantidades.
        5. Valida cada senal con el risk manager.
        6. Ejecuta ordenes validas.
        7. Envia alertas por Telegram.
        """
        if not self.should_run():
            self._log.debug("run.skip.should_run_false")
            return

        context = self._get_market_context()

        if self._is_paused_by_research(context):
            self._log.info("run.skip.pausada_por_research")
            return

        sizing_multiplier = self._get_sizing_multiplier(context)

        try:
            signals = self.generate_signals()
        except Exception:
            self._log.exception("run.error_generando_senales")
            return

        if not signals:
            self._log.debug("run.sin_senales")
            return

        self._log.info(
            "run.senales_generadas",
            cantidad=len(signals),
            sizing_multiplier=sizing_multiplier,
        )

        for signal in signals:
            self._process_signal(signal, sizing_multiplier)

    def _process_signal(self, signal: Signal, sizing_multiplier: float) -> None:
        """Procesa una senal individual: valida, ejecuta y alerta.

        Args:
            signal: Senal de trading a procesar.
            sizing_multiplier: Multiplicador de sizing del research agent.
        """
        # Aplicar sizing multiplier
        adjusted_qty = signal.cantidad * sizing_multiplier
        if adjusted_qty <= 0:
            self._log.info(
                "signal.skip.cantidad_cero_post_sizing",
                ticker=signal.ticker,
                sizing_multiplier=sizing_multiplier,
            )
            return

        signal.cantidad = adjusted_qty

        # Alertar senal generada
        try:
            asyncio.get_event_loop().run_until_complete(
                self.alertas.signal_generada(
                    strategy=signal.strategy,
                    ticker=signal.ticker,
                    operacion=signal.operacion,
                    motivo=signal.motivo,
                )
            )
        except RuntimeError:
            # No hay event loop corriendo, crear uno temporal
            asyncio.run(
                self.alertas.signal_generada(
                    strategy=signal.strategy,
                    ticker=signal.ticker,
                    operacion=signal.operacion,
                    motivo=signal.motivo,
                )
            )
        except Exception:
            self._log.exception("signal.error_alerta_senal")

        # Validar con risk manager — construir OrderIntent desde Signal
        try:
            from core.risk_manager import OrderIntent

            order_intent = OrderIntent(
                ticker=signal.ticker,
                tipo=signal.tipo,
                operacion=signal.operacion,
                cantidad=signal.cantidad,
                precio=signal.precio if signal.precio is not None else 0.0,
                plazo=signal.plazo,
                strategy=signal.strategy,
            )
            valid, motivo_rechazo = self.risk_manager.validate(order_intent)
        except Exception:
            self._log.exception("signal.error_validando_riesgo", ticker=signal.ticker)
            return

        if not valid:
            self._log.warning(
                "signal.rechazada_por_risk",
                ticker=signal.ticker,
                motivo=motivo_rechazo,
            )
            try:
                asyncio.get_event_loop().run_until_complete(
                    self.alertas.orden_rechazada(
                        strategy=signal.strategy,
                        ticker=signal.ticker,
                        motivo=motivo_rechazo,
                    )
                )
            except RuntimeError:
                asyncio.run(
                    self.alertas.orden_rechazada(
                        strategy=signal.strategy,
                        ticker=signal.ticker,
                        motivo=motivo_rechazo,
                    )
                )
            except Exception:
                self._log.exception("signal.error_alerta_rechazo")
            return

        # Ejecutar orden
        try:
            from config import DRY_RUN_GLOBAL
            result = self.order_manager.send_order(
                ticker=signal.ticker,
                tipo=signal.tipo,
                operacion=signal.operacion,
                cantidad=signal.cantidad,
                precio=signal.precio,
                plazo=signal.plazo,
                strategy=signal.strategy,
                dry_run=DRY_RUN_GLOBAL,
            )
            self._log.info(
                "signal.orden_enviada",
                ticker=signal.ticker,
                operacion=signal.operacion,
                cantidad=signal.cantidad,
                result=result,
            )
        except Exception:
            self._log.exception(
                "signal.error_enviando_orden",
                ticker=signal.ticker,
            )
