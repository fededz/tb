"""Monitor de heartbeat y resumen diario.

Envia notificaciones periodicas a Telegram para confirmar que el sistema
esta operativo, y genera un resumen al cierre de cada jornada.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from core.alertas import Alertas
    from core.portfolio import Portfolio
    from db.repository import Repository

logger = structlog.get_logger(__name__)


class HeartbeatMonitor:
    """Monitor que envia heartbeats periodicos y resumenes diarios."""

    def __init__(
        self,
        portfolio: Portfolio,
        alertas: Alertas,
        repository: Repository | None = None,
    ) -> None:
        self._portfolio = portfolio
        self._alertas = alertas
        self._repository = repository

    def send_heartbeat(self) -> None:
        """Envia un heartbeat con metricas basicas del sistema."""
        try:
            capital_total = self._portfolio.get_capital_total()
        except Exception:
            logger.exception("heartbeat_error_capital")
            capital_total = 0.0

        try:
            posiciones_abiertas = self._portfolio.get_posiciones_count()
        except Exception:
            logger.exception("heartbeat_error_posiciones")
            posiciones_abiertas = 0

        try:
            pnl_dia = self._portfolio.get_pnl_diario()
        except Exception:
            logger.exception("heartbeat_error_pnl")
            pnl_dia = 0.0

        logger.info(
            "heartbeat",
            capital=capital_total,
            posiciones=posiciones_abiertas,
            pnl_dia=pnl_dia,
        )

        self._alertas.heartbeat(
            capital_total=capital_total,
            posiciones_abiertas=posiciones_abiertas,
            pnl_dia=pnl_dia,
        )

    def send_resumen_diario(self) -> None:
        """Envia el resumen de cierre de jornada."""
        pnl_ars: float = 0.0
        pnl_usd: float = 0.0
        trades: int = 0
        capital: float = 0.0

        if self._repository is not None:
            try:
                pnl_data = self._repository.get_pnl_diario(date.today())
                if pnl_data is not None:
                    pnl_ars = float(pnl_data.get("pnl_ars", 0.0) or 0.0)
                    pnl_usd = float(pnl_data.get("pnl_usd", 0.0) or 0.0)
                    trades = int(pnl_data.get("trades", 0) or 0)
                    capital = float(pnl_data.get("capital_fin", 0.0) or 0.0)
            except Exception:
                logger.exception("resumen_error_obteniendo_pnl_db")

        if capital == 0.0:
            try:
                capital = self._portfolio.get_capital_total()
            except Exception:
                logger.exception("resumen_error_capital_fallback")

        if pnl_ars == 0.0:
            try:
                pnl_ars = self._portfolio.get_pnl_diario()
            except Exception:
                logger.exception("resumen_error_pnl_fallback")

        logger.info(
            "resumen_diario",
            pnl_ars=pnl_ars,
            pnl_usd=pnl_usd,
            trades=trades,
            capital=capital,
        )

        self._alertas.resumen_diario(
            pnl_ars=pnl_ars,
            pnl_usd=pnl_usd,
            trades=trades,
            capital=capital,
        )
