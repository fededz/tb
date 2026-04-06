"""Validaciones de riesgo antes de cada orden.

Cada orden pasa por el RiskManager antes de ejecutarse.
Si falla cualquier validacion, la orden se rechaza y se alerta por Telegram.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

import structlog
from zoneinfo import ZoneInfo

import config

if TYPE_CHECKING:
    from core.alertas import Alertas
    from core.portfolio import Portfolio
    from db.repository import Repository

logger = structlog.get_logger(__name__)

ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


@dataclass
class OrderIntent:
    """Intencion de orden antes de ser validada y enviada.

    Contiene todos los datos necesarios para que el RiskManager
    evalue si la orden puede ejecutarse.
    """

    ticker: str
    tipo: str
    operacion: str  # "COMPRA" | "VENTA"
    cantidad: float
    precio: float
    plazo: str
    strategy: str


class RiskManager:
    """Validador de riesgo que filtra todas las ordenes antes de ejecucion.

    Ejecuta una serie de validaciones secuenciales. Si cualquiera falla,
    la orden se rechaza y se envia una alerta por Telegram.
    """

    def __init__(
        self,
        portfolio: Portfolio,
        repository: Repository,
        alertas: Alertas,
    ) -> None:
        """Inicializa el RiskManager.

        Args:
            portfolio: Estado actual del portafolio.
            repository: Acceso a la base de datos.
            alertas: Bot de Telegram para enviar alertas de rechazo.
        """
        self._portfolio = portfolio
        self._repository = repository
        self._alertas = alertas

    def validate(self, order: OrderIntent) -> tuple[bool, str]:
        """Ejecuta todas las validaciones de riesgo sobre una orden.

        Las validaciones se ejecutan en orden de menor a mayor costo computacional.
        Se detiene en la primera que falla.

        Args:
            order: Intencion de orden a validar.

        Returns:
            Tupla (aprobada, motivo). Si aprobada es True, motivo es 'ok'.
            Si aprobada es False, motivo describe la razon del rechazo.
        """
        validaciones = [
            self._check_horario_mercado,
            self._check_capital_disponible,
            self._check_capital_por_operacion,
            self._check_posiciones_abiertas,
            self._check_drawdown_diario,
            self._check_market_context,
        ]

        for validacion in validaciones:
            aprobada, motivo = validacion(order)
            if not aprobada:
                logger.warning(
                    "orden_rechazada_por_riesgo",
                    ticker=order.ticker,
                    operacion=order.operacion,
                    strategy=order.strategy,
                    motivo=motivo,
                )
                self._alertar_rechazo(order, motivo)
                return False, motivo

        logger.info(
            "orden_aprobada_por_riesgo",
            ticker=order.ticker,
            operacion=order.operacion,
            cantidad=order.cantidad,
            strategy=order.strategy,
        )
        return True, "ok"

    def _check_capital_disponible(self, order: OrderIntent) -> tuple[bool, str]:
        """Verifica que haya capital suficiente para la orden.

        Args:
            order: Intencion de orden.

        Returns:
            Tupla (aprobada, motivo).
        """
        if order.operacion == "VENTA":
            return True, "ok"

        monto_orden = order.cantidad * order.precio
        capital_disponible = self.get_capital_disponible()

        if monto_orden > capital_disponible:
            return (
                False,
                f"Capital insuficiente: orden {monto_orden:.2f} ARS > "
                f"disponible {capital_disponible:.2f} ARS",
            )
        return True, "ok"

    def _check_capital_por_operacion(self, order: OrderIntent) -> tuple[bool, str]:
        """Verifica que la orden no supere el porcentaje maximo de capital por operacion.

        Args:
            order: Intencion de orden.

        Returns:
            Tupla (aprobada, motivo).
        """
        if order.operacion == "VENTA":
            return True, "ok"

        monto_orden = order.cantidad * order.precio
        capital_total = self._portfolio.get_capital_total()

        if capital_total <= 0:
            return False, "No se pudo obtener el capital total"

        pct_orden = monto_orden / capital_total
        max_pct = config.MAX_CAPITAL_POR_OPERACION_PCT

        if pct_orden > max_pct:
            return (
                False,
                f"Orden supera limite por operacion: {pct_orden:.1%} > "
                f"{max_pct:.1%} del capital total",
            )
        return True, "ok"

    def _check_posiciones_abiertas(self, order: OrderIntent) -> tuple[bool, str]:
        """Verifica que no se supere el maximo de posiciones abiertas.

        Solo aplica para compras que abren una posicion nueva.

        Args:
            order: Intencion de orden.

        Returns:
            Tupla (aprobada, motivo).
        """
        if order.operacion == "VENTA":
            return True, "ok"

        posiciones_actuales = self.get_posiciones_abiertas()
        max_posiciones = config.MAX_POSICIONES_ABIERTAS

        # Si ya tenemos posicion en este ticker, no cuenta como nueva
        posiciones = self._portfolio.get_posiciones()
        key = f"{order.ticker}:{order.tipo}"
        if key in posiciones:
            return True, "ok"

        if posiciones_actuales >= max_posiciones:
            return (
                False,
                f"Maximo de posiciones alcanzado: {posiciones_actuales} >= "
                f"{max_posiciones}",
            )
        return True, "ok"

    def _check_drawdown_diario(self, order: OrderIntent) -> tuple[bool, str]:
        """Verifica que el drawdown diario no haya sido superado.

        Args:
            order: Intencion de orden.

        Returns:
            Tupla (aprobada, motivo).
        """
        if not self.check_drawdown_diario():
            return (
                False,
                "Drawdown diario maximo superado, operaciones bloqueadas",
            )
        return True, "ok"

    def _check_horario_mercado(self, order: OrderIntent) -> tuple[bool, str]:
        """Verifica que estemos dentro del horario de mercado.

        BYMA: 11:00 - 17:00 lunes a viernes (Argentina).
        ROFEX: 10:00 - 17:00 lunes a viernes (Argentina).

        Args:
            order: Intencion de orden.

        Returns:
            Tupla (aprobada, motivo).
        """
        ahora = datetime.now(ARGENTINA_TZ)

        # Lunes=0, Viernes=4
        if ahora.weekday() > 4:
            return False, "Fuera de horario: fin de semana"

        hora = ahora.hour

        # Determinar horario segun tipo de instrumento/plazo
        if order.plazo == "INMEDIATA" or "DLR" in order.ticker or "RFX" in order.ticker or "SOJ" in order.ticker:
            # ROFEX: 10:00 - 17:00
            open_hour = config.ROFEX_OPEN_HOUR
            close_hour = config.ROFEX_CLOSE_HOUR
            mercado = "ROFEX"
        else:
            # BYMA: 11:00 - 17:00
            open_hour = config.BYMA_OPEN_HOUR
            close_hour = config.BYMA_CLOSE_HOUR
            mercado = "BYMA"

        if hora < open_hour or hora >= close_hour:
            return (
                False,
                f"Fuera de horario de {mercado}: {ahora.strftime('%H:%M')} "
                f"(horario: {open_hour}:00 - {close_hour}:00)",
            )
        return True, "ok"

    def _check_market_context(self, order: OrderIntent) -> tuple[bool, str]:
        """Verifica el contexto de mercado del research agent.

        Si el riesgo macro es critico, no se permiten nuevas compras.
        Si la estrategia esta pausada por el research agent, se rechaza.

        Args:
            order: Intencion de orden.

        Returns:
            Tupla (aprobada, motivo).
        """
        if order.operacion == "VENTA":
            return True, "ok"

        try:
            context = self._repository.get_latest_market_context()
            if context is None:
                # Sin contexto disponible, se permite operar
                return True, "ok"

            if context.get("riesgo_macro") == "critico":
                return (
                    False,
                    "Riesgo macro CRITICO: operaciones nuevas bloqueadas",
                )

            pausadas = context.get("estrategias_pausadas") or []
            if order.strategy in pausadas:
                return (
                    False,
                    f"Estrategia '{order.strategy}' pausada por research agent",
                )

        except Exception:
            logger.warning("error_consultando_market_context")
            # Ante error de contexto, permitir operar
            return True, "ok"

        return True, "ok"

    def check_drawdown_diario(self) -> bool:
        """Verifica si el drawdown diario esta dentro de los limites.

        Compara el P&L del dia contra el capital de inicio del dia,
        usando el max_drawdown_diario_pct del perfil de riesgo activo.

        Returns:
            True si estamos dentro del limite de drawdown.
            False si el drawdown fue superado.
        """
        try:
            pnl_diario = self._portfolio.get_pnl_diario()
            capital_total = self._portfolio.get_capital_total()

            if capital_total <= 0:
                logger.warning("capital_total_cero_en_check_drawdown")
                return False

            # Obtener max drawdown del perfil activo
            max_dd = self._get_max_drawdown_pct()

            drawdown_actual = abs(min(pnl_diario, 0.0)) / capital_total

            if drawdown_actual >= max_dd:
                logger.error(
                    "drawdown_diario_superado",
                    drawdown_actual=f"{drawdown_actual:.2%}",
                    max_drawdown=f"{max_dd:.2%}",
                    pnl_diario=pnl_diario,
                )
                return False

            return True

        except Exception:
            logger.exception("error_verificando_drawdown_diario")
            # Ante error, bloquear por seguridad
            return False

    def _get_max_drawdown_pct(self) -> float:
        """Obtiene el porcentaje maximo de drawdown diario del perfil activo.

        Intenta leer el perfil activo desde la DB. Si no hay perfil activo,
        usa el perfil inicial de config.

        Returns:
            Porcentaje maximo de drawdown diario (ej: 0.03 para 3%).
        """
        try:
            perfil = self._repository.get_active_risk_profile()
            if perfil is not None:
                return float(perfil["max_drawdown_diario_pct"])
        except Exception:
            logger.warning("error_obteniendo_perfil_activo_para_drawdown")

        # Fallback al perfil inicial de config
        perfil_config = config.PERFILES.get(config.PERFIL_INICIAL, {})
        return perfil_config.get("max_drawdown_diario_pct", 0.03)

    def get_capital_disponible(self) -> float:
        """Obtiene el capital disponible para nuevas operaciones.

        Returns:
            Capital disponible en ARS.
        """
        return self._portfolio.get_capital_disponible()

    def get_posiciones_abiertas(self) -> int:
        """Obtiene la cantidad de posiciones abiertas.

        Returns:
            Numero de posiciones abiertas.
        """
        return self._portfolio.get_posiciones_count()

    def adjust_size_for_context(self, cantidad: float) -> float:
        """Ajusta el tamano de una orden segun el contexto de mercado.

        Aplica el sizing_multiplier del research agent. Si el riesgo macro
        es alto, reduce el tamano; si es critico, devuelve 0.

        Args:
            cantidad: Cantidad original de la orden.

        Returns:
            Cantidad ajustada por el sizing_multiplier.
        """
        try:
            context = self._repository.get_latest_market_context()
            if context is None:
                logger.debug("sin_contexto_de_mercado_para_sizing")
                return cantidad

            multiplier = float(context.get("sizing_mult") or 1.0)

            if context.get("riesgo_macro") == "critico":
                logger.warning("sizing_cero_por_riesgo_critico")
                return 0.0

            adjusted = cantidad * multiplier
            if multiplier < 1.0:
                logger.info(
                    "tamano_ajustado_por_contexto",
                    cantidad_original=cantidad,
                    cantidad_ajustada=adjusted,
                    sizing_multiplier=multiplier,
                    riesgo_macro=context.riesgo_macro,
                )
            return adjusted

        except Exception:
            logger.warning("error_ajustando_sizing_por_contexto")
            return cantidad

    def _alertar_rechazo(self, order: OrderIntent, motivo: str) -> None:
        """Envia una alerta por Telegram cuando una orden es rechazada.

        Args:
            order: Orden rechazada.
            motivo: Razon del rechazo.
        """
        mensaje = (
            f"ORDEN RECHAZADA\n"
            f"Estrategia: {order.strategy}\n"
            f"Ticker: {order.ticker}\n"
            f"Op: {order.operacion} {order.cantidad} @ ${order.precio:,.2f}\n"
            f"Motivo: {motivo}"
        )
        try:
            self._alertas.send(mensaje, priority="alta")
        except Exception:
            logger.exception("error_enviando_alerta_rechazo")
