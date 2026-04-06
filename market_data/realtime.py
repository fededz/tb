"""Handler de WebSocket para datos de mercado en tiempo real.

Gestiona las suscripciones a instrumentos y alimenta el MarketDataCache
con cada tick recibido. Permite registrar callbacks externos para que
las estrategias reaccionen a nuevos datos.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from market_data.cache import MarketDataCache

if TYPE_CHECKING:
    from core.ppi_wrapper import PPIWrapper

logger = structlog.get_logger(__name__)


class RealtimeHandler:
    """Maneja la conexion WebSocket de market data y distribuye actualizaciones.

    El PPIWrapper ya se encarga de la reconexion automatica con backoff
    exponencial. Este handler gestiona la lista de suscripciones y el
    flujo de datos hacia el cache y los callbacks registrados.
    """

    def __init__(self, ppi: PPIWrapper, cache: MarketDataCache) -> None:
        """Inicializa el handler de datos en tiempo real.

        Args:
            ppi: Wrapper de la API de PPI con WebSocket ya configurado.
            cache: Cache en memoria donde se almacenan los precios.
        """
        self._ppi = ppi
        self._cache = cache
        self._callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._subscriptions: list[dict[str, str]] = []
        self._lock: threading.Lock = threading.Lock()
        self._running: bool = False
        logger.info("realtime_handler.inicializado")

    def start(self) -> None:
        """Conecta el WebSocket y configura el callback de market data.

        Llama a ppi.connect() si no esta conectado, y re-suscribe a todos
        los instrumentos previamente registrados.
        """
        if self._running:
            logger.warning("realtime_handler.ya_corriendo")
            return

        try:
            self._ppi.connect()
            self._running = True
            logger.info("realtime_handler.iniciado")

            # Re-suscribir instrumentos existentes (util tras reconexion)
            with self._lock:
                subs = list(self._subscriptions)
            for sub in subs:
                self._do_subscribe(sub["ticker"], sub["tipo"], sub["plazo"])

        except Exception:
            logger.exception("realtime_handler.error_al_iniciar")
            self._running = False
            raise

    def stop(self) -> None:
        """Detiene la recepcion de datos en tiempo real."""
        self._running = False
        logger.info("realtime_handler.detenido")

    def subscribe(self, ticker: str, tipo: str, plazo: str) -> None:
        """Suscribe a un instrumento para recibir datos en tiempo real.

        Si el instrumento ya esta suscripto, no duplica la suscripcion.

        Args:
            ticker: Simbolo del instrumento (ej: "GGAL", "DLR/JUN25").
            tipo: Tipo de instrumento (ej: "Acciones", "Futuros").
            plazo: Plazo de liquidacion (ej: "A-48HS", "INMEDIATA").
        """
        sub_entry = {"ticker": ticker, "tipo": tipo, "plazo": plazo}
        with self._lock:
            # Evitar duplicados
            if sub_entry in self._subscriptions:
                logger.debug(
                    "realtime_handler.ya_suscripto",
                    ticker=ticker,
                    tipo=tipo,
                    plazo=plazo,
                )
                return
            self._subscriptions.append(sub_entry)

        if self._running:
            self._do_subscribe(ticker, tipo, plazo)

    def _do_subscribe(self, ticker: str, tipo: str, plazo: str) -> None:
        """Ejecuta la suscripcion real contra la API de PPI.

        Args:
            ticker: Simbolo del instrumento.
            tipo: Tipo de instrumento.
            plazo: Plazo de liquidacion.
        """
        try:
            self._ppi.subscribe_instrument(ticker, tipo, plazo)
            logger.info(
                "realtime_handler.suscripto",
                ticker=ticker,
                tipo=tipo,
                plazo=plazo,
            )
        except Exception:
            logger.exception(
                "realtime_handler.error_suscripcion",
                ticker=ticker,
                tipo=tipo,
                plazo=plazo,
            )

    def on_market_data(self, data: dict[str, Any]) -> None:
        """Callback invocado por el WebSocket cuando llega un tick de mercado.

        Actualiza el cache y notifica a todos los callbacks registrados.

        Args:
            data: Diccionario con los datos del tick. Se espera que contenga
                  al menos 'ticker', 'tipo', 'plazo' y 'price'.
        """
        if not self._running:
            return

        ticker = data.get("ticker", "")
        tipo = data.get("tipo", "")
        plazo = data.get("plazo", "")
        price = data.get("price")
        volume = data.get("volume")
        timestamp = data.get("timestamp")

        if not ticker or price is None:
            logger.warning("realtime_handler.dato_incompleto", data=data)
            return

        # Actualizar cache
        self._cache.update(
            ticker=ticker,
            tipo=tipo,
            plazo=plazo,
            price=float(price),
            volume=float(volume) if volume is not None else None,
            timestamp=float(timestamp) if timestamp is not None else None,
        )

        # Notificar callbacks externos
        with self._lock:
            callbacks = list(self._callbacks)

        for callback in callbacks:
            try:
                callback(data)
            except Exception:
                logger.exception(
                    "realtime_handler.error_en_callback",
                    ticker=ticker,
                )

    def register_callback(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Registra un callback externo que se invoca con cada tick.

        Los callbacks reciben el diccionario raw del tick. Se ejecutan
        de forma sincronica en el thread del WebSocket, por lo que deben
        ser rapidos. Para procesamiento pesado, encolar en otro thread.

        Args:
            fn: Funcion que acepta un dict con datos del tick.
        """
        with self._lock:
            self._callbacks.append(fn)
        logger.info("realtime_handler.callback_registrado", total=len(self._callbacks))

    def get_subscribed_instruments(self) -> list[dict[str, str]]:
        """Retorna la lista de instrumentos suscriptos.

        Returns:
            Lista de dicts con claves 'ticker', 'tipo', 'plazo'.
        """
        with self._lock:
            return list(self._subscriptions)
