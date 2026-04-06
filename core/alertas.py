"""Sistema de alertas via Telegram para el trading bot.

Envia notificaciones formateadas para ordenes ejecutadas, rechazadas,
errores de conexion, drawdown, heartbeat y resumenes diarios.
Si el bot no esta configurado (token o chat_id vacios), loguea los
mensajes en lugar de enviarlos.

Usa httpx sincrono para evitar problemas de event loop con asyncio.run().
"""

from __future__ import annotations

import httpx
import structlog

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = structlog.get_logger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class Alertas:
    """Cliente de alertas Telegram para el sistema de trading (sincrono)."""

    def __init__(self) -> None:
        """Inicializa el cliente de Telegram.

        Si TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID estan vacios, el bot
        queda deshabilitado y los mensajes se loguean en su lugar.
        """
        self._token: str = TELEGRAM_BOT_TOKEN
        self._chat_id: str = TELEGRAM_CHAT_ID
        self._enabled: bool = bool(self._token and self._chat_id)
        self._url: str = TELEGRAM_API_URL.format(token=self._token)

        if self._enabled:
            logger.info("alertas.telegram_habilitado", chat_id=self._chat_id)
        else:
            logger.warning(
                "alertas.telegram_deshabilitado",
                motivo="TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados",
            )

    def send(self, message: str, priority: str = "media") -> None:
        """Envia un mensaje por Telegram.

        Si el bot no esta configurado, loguea el mensaje. Nunca lanza
        excepciones; los errores de envio se loguean silenciosamente
        para no afectar el flujo de trading.

        Args:
            message: Texto del mensaje (soporta formato HTML de Telegram).
            priority: Nivel de prioridad (baja, media, alta, critica).
        """
        if not self._enabled:
            logger.info(
                "alertas.mensaje_local",
                priority=priority,
                message=message,
            )
            return

        try:
            resp = httpx.post(
                self._url,
                json={
                    "chat_id": self._chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                },
                timeout=10.0,
            )
            if resp.status_code == 200:
                logger.debug("alertas.mensaje_enviado", priority=priority)
            else:
                logger.error(
                    "alertas.error_envio_http",
                    status=resp.status_code,
                    body=resp.text[:300],
                )
        except Exception:
            logger.exception(
                "alertas.error_envio",
                priority=priority,
                message=message[:200],
            )

    # ------------------------------------------------------------------
    # Mensajes pre-formateados
    # ------------------------------------------------------------------

    def orden_ejecutada(
        self,
        strategy: str,
        ticker: str,
        operacion: str,
        cantidad: float,
        precio: float,
        pnl_dia: float,
    ) -> None:
        """Notifica una orden ejecutada exitosamente."""
        msg = (
            "\U0001f7e2 <b>ORDEN EJECUTADA</b>\n"
            f"Estrategia: {strategy}\n"
            f"Ticker: {ticker}\n"
            f"Op: {operacion} {cantidad:,.4f} @ ${precio:,.2f}\n"
            f"P&amp;L dia: ${pnl_dia:+,.2f} ARS"
        )
        self.send(msg, priority="alta")

    def orden_rechazada(
        self,
        strategy: str,
        ticker: str,
        motivo: str,
    ) -> None:
        """Notifica una orden rechazada por el risk manager o la API."""
        msg = (
            "\U0001f534 <b>ORDEN RECHAZADA</b>\n"
            f"Estrategia: {strategy}\n"
            f"Ticker: {ticker}\n"
            f"Motivo: {motivo}"
        )
        self.send(msg, priority="alta")

    def error_conexion(self, detalle: str) -> None:
        """Alerta sobre un error de conexion con PPI u otro servicio."""
        msg = (
            "\u26a0\ufe0f <b>ERROR DE CONEXION</b>\n"
            f"Detalle: {detalle}"
        )
        self.send(msg, priority="alta")

    def drawdown_superado(self, pct: float, limite: float) -> None:
        """Alerta critica: el drawdown diario supero el limite."""
        msg = (
            "\U0001f6a8 <b>DRAWDOWN DIARIO SUPERADO</b>\n"
            f"Actual: {pct * 100:.2f}%\n"
            f"Limite: {limite * 100:.2f}%\n"
            "Operaciones bloqueadas hasta manana."
        )
        self.send(msg, priority="critica")

    def heartbeat(
        self,
        capital_total: float,
        posiciones_abiertas: int,
        pnl_dia: float,
    ) -> None:
        """Heartbeat periodico indicando que el sistema esta vivo."""
        msg = (
            "\U0001f49a <b>HEARTBEAT</b>\n"
            f"Capital: ${capital_total:,.2f} ARS\n"
            f"Posiciones abiertas: {posiciones_abiertas}\n"
            f"P&amp;L dia: ${pnl_dia:+,.2f} ARS"
        )
        self.send(msg, priority="baja")

    def resumen_diario(
        self,
        pnl_ars: float,
        pnl_usd: float,
        trades: int,
        capital: float,
    ) -> None:
        """Resumen de fin de jornada."""
        msg = (
            "\U0001f4ca <b>RESUMEN DIARIO</b>\n"
            f"P&amp;L ARS: ${pnl_ars:+,.2f}\n"
            f"P&amp;L USD: ${pnl_usd:+,.2f}\n"
            f"Trades: {trades}\n"
            f"Capital: ${capital:,.2f} ARS"
        )
        self.send(msg, priority="media")

    def signal_generada(
        self,
        strategy: str,
        ticker: str,
        operacion: str,
        motivo: str,
    ) -> None:
        """Notifica que una estrategia genero una nueva senal."""
        msg = (
            "\U0001f4e1 <b>NUEVA SENAL</b>\n"
            f"Estrategia: {strategy}\n"
            f"Ticker: {ticker}\n"
            f"Op: {operacion}\n"
            f"Motivo: {motivo}"
        )
        self.send(msg, priority="media")
