"""Wrapper sobre ppi_client con reconexion automatica, rate limiting y logging.

Usa la API real del SDK ppi_client: instancia PPI(sandbox=...), login via
ppi.account.login_api(), y acceso a marketdata/orders/realtime como atributos
de la instancia.
"""

from __future__ import annotations

import threading
import time
from datetime import date
from typing import Any, Callable

import pandas as pd
import structlog

from ppi_client.models.estimate_bonds import EstimateBonds
from ppi_client.models.instrument import Instrument
from ppi_client.ppi import PPI

from config import PPI_ACCOUNT_NUMBER, PPI_PRIVATE_KEY, PPI_PUBLIC_KEY, PPI_SANDBOX

logger = structlog.get_logger(__name__)


class _RateLimiter:
    """Token bucket rate limiter para llamadas a la API."""

    def __init__(self, max_calls: float = 5.0, period: float = 1.0) -> None:
        self._max_calls = max_calls
        self._period = period
        self._tokens = max_calls
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Bloquea hasta que haya un token disponible."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(
                    self._max_calls,
                    self._tokens + elapsed * (self._max_calls / self._period),
                )
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

            time.sleep(0.05)


class PPIWrapper:
    """Wrapper sobre el SDK de PPI con reconexion, rate limiting y logging.

    Maneja la conexion REST y WebSocket contra la API de PPI Inversiones,
    implementando reconexion automatica con backoff exponencial y
    re-suscripcion de instrumentos tras reconexion.

    Expone ``ppi_client`` como property para acceso directo al SDK
    (por ejemplo, ``order_manager`` usa ``wrapper.ppi_client.orders``).
    """

    _MAX_BACKOFF_SECONDS: float = 60.0
    _INITIAL_BACKOFF_SECONDS: float = 1.0

    def __init__(self) -> None:
        self._ppi: PPI | None = None
        self._connected: bool = False
        self._subscribed_instruments: list[tuple[str, str, str]] = []
        self._market_data_callbacks: list[Callable[[dict], None]] = []
        self._account_data_callbacks: list[Callable[[dict], None]] = []
        self._rate_limiter = _RateLimiter(max_calls=5.0, period=1.0)
        self._reconnect_backoff: float = self._INITIAL_BACKOFF_SECONDS
        self._reconnecting: bool = False
        self._reconnect_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Property para acceso directo al SDK
    # ------------------------------------------------------------------

    @property
    def ppi_client(self) -> PPI:
        """Retorna la instancia de PPI para acceso directo al SDK.

        Util para que otros modulos (order_manager, etc.) accedan a
        ``ppi_client.orders``, ``ppi_client.configuration``, etc.

        Raises:
            RuntimeError: Si el wrapper no esta conectado.
        """
        self._ensure_connected()
        assert self._ppi is not None
        return self._ppi

    # ------------------------------------------------------------------
    # Conexion
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Conecta a la API de PPI usando las credenciales de config.

        Instancia PPI con el modo sandbox correspondiente, luego realiza
        login via ``ppi.account.login_api()``.

        Si PPI_SANDBOX es False, loguea una advertencia prominente.
        No-op si ya esta conectado.
        """
        if self._connected and self._ppi is not None:
            return

        if not PPI_SANDBOX:
            logger.critical(
                "MODO LIVE ACTIVO - ORDENES REALES EN CURSO",
                sandbox=False,
            )

        sandbox_label = "SANDBOX" if PPI_SANDBOX else "LIVE"
        logger.info(
            "conectando_a_ppi",
            modo=sandbox_label,
            cuenta=PPI_ACCOUNT_NUMBER,
        )

        try:
            self._ppi = PPI(sandbox=PPI_SANDBOX)
            self._ppi.account.login_api(PPI_PUBLIC_KEY, PPI_PRIVATE_KEY)
            self._connected = True
            self._reconnect_backoff = self._INITIAL_BACKOFF_SECONDS
            logger.info("conexion_exitosa", modo=sandbox_label)
        except Exception:
            logger.exception("error_conectando_a_ppi")
            raise

    # ------------------------------------------------------------------
    # WebSocket - suscripciones y callbacks
    # ------------------------------------------------------------------

    def subscribe_instrument(self, ticker: str, tipo: str, plazo: str) -> None:
        """Suscribe a un instrumento via WebSocket para datos en tiempo real.

        Args:
            ticker: Simbolo del instrumento (ej: 'GGAL', 'AL30', 'DLR/JUN25').
            tipo: Tipo de instrumento (ej: 'Acciones', 'Bonos', 'Futuros').
            plazo: Plazo de liquidacion (ej: 'INMEDIATA', 'A-48HS').
        """
        self._ensure_connected()
        assert self._ppi is not None

        instrument_key = (ticker, tipo, plazo)
        if instrument_key not in self._subscribed_instruments:
            self._subscribed_instruments.append(instrument_key)

        try:
            self._rate_limiter.acquire()
            self._ppi.realtime.subscribe_to_element(
                Instrument(ticker, tipo, plazo)
            )
            logger.info(
                "instrumento_suscripto",
                ticker=ticker,
                tipo=tipo,
                plazo=plazo,
            )
        except Exception:
            logger.exception(
                "error_suscribiendo_instrumento",
                ticker=ticker,
                tipo=tipo,
                plazo=plazo,
            )

    def on_market_data(self, callback: Callable[[dict], None]) -> None:
        """Registra un callback para recibir datos de mercado en tiempo real.

        Args:
            callback: Funcion que recibe un dict con los datos del tick.
        """
        self._market_data_callbacks.append(callback)

    def on_account_data(self, callback: Callable[[dict], None]) -> None:
        """Registra un callback para recibir datos de cuenta en tiempo real.

        Args:
            callback: Funcion que recibe un dict con los datos de la cuenta.
        """
        self._account_data_callbacks.append(callback)

    def start_realtime(self) -> None:
        """Inicia las conexiones WebSocket de datos en tiempo real.

        Configura los handlers de market data y account data con sus
        respectivos connect/disconnect/data handlers, luego arranca
        el event loop via ``start_connections()``.
        """
        self._ensure_connected()
        assert self._ppi is not None

        try:
            self._ppi.realtime.connect_to_market_data(
                self._handle_ws_connect,
                self._handle_disconnect,
                self._handle_market_data,
            )
            self._ppi.realtime.connect_to_account(
                self._handle_ws_connect,
                self._handle_disconnect,
                self._handle_account_data,
            )

            # Suscribir a datos de cuenta
            self._ppi.realtime.subscribe_to_account_data(PPI_ACCOUNT_NUMBER)

            self._ppi.realtime.start_connections()
            logger.info("websocket_conectado")
        except Exception:
            logger.exception("error_conectando_websocket")

    # ------------------------------------------------------------------
    # Market data REST
    # ------------------------------------------------------------------

    def get_current_price(self, ticker: str, tipo: str, plazo: str) -> float:
        """Obtiene el precio actual de un instrumento via REST.

        Usa ``ppi.marketdata.current()`` para obtener los datos de mercado
        actuales del instrumento.

        Args:
            ticker: Simbolo del instrumento.
            tipo: Tipo de instrumento.
            plazo: Plazo de liquidacion.

        Returns:
            Precio actual como float. Retorna 0.0 si no se puede obtener.
        """
        self._ensure_connected()
        assert self._ppi is not None

        try:
            self._rate_limiter.acquire()
            result = self._ppi.marketdata.current(ticker, tipo, plazo)
            logger.info(
                "precio_obtenido",
                ticker=ticker,
                tipo=tipo,
                plazo=plazo,
            )
            if result and isinstance(result, dict):
                return float(result.get("price", 0.0))
            if result and isinstance(result, list) and len(result) > 0:
                return float(result[0].get("price", 0.0))
            return 0.0
        except Exception:
            logger.exception(
                "error_obteniendo_precio",
                ticker=ticker,
                tipo=tipo,
                plazo=plazo,
            )
            return 0.0

    def get_historical(
        self,
        ticker: str,
        tipo: str,
        plazo: str,
        desde: date,
        hasta: date,
    ) -> pd.DataFrame:
        """Obtiene datos historicos OHLCV de un instrumento.

        Usa ``ppi.marketdata.search()`` con rango de fechas para obtener
        datos historicos.

        Args:
            ticker: Simbolo del instrumento.
            tipo: Tipo de instrumento.
            plazo: Plazo de liquidacion.
            desde: Fecha de inicio.
            hasta: Fecha de fin.

        Returns:
            DataFrame con datos historicos.
            DataFrame vacio si hay error.
        """
        self._ensure_connected()
        assert self._ppi is not None

        try:
            self._rate_limiter.acquire()
            result = self._ppi.marketdata.search(
                ticker,
                tipo,
                plazo,
                desde,
                hasta,
            )
            logger.info(
                "historico_obtenido",
                ticker=ticker,
                tipo=tipo,
                plazo=plazo,
                desde=str(desde),
                hasta=str(hasta),
            )
            if result:
                df = pd.DataFrame(result)
                return df
            return pd.DataFrame()
        except Exception:
            logger.exception(
                "error_obteniendo_historico",
                ticker=ticker,
                tipo=tipo,
                plazo=plazo,
            )
            return pd.DataFrame()

    def get_book(self, ticker: str, tipo: str, plazo: str) -> dict:
        """Obtiene el libro de ordenes (order book) de un instrumento.

        Args:
            ticker: Simbolo del instrumento.
            tipo: Tipo de instrumento.
            plazo: Plazo de liquidacion.

        Returns:
            Dict con bids y asks. Dict vacio si hay error.
        """
        self._ensure_connected()
        assert self._ppi is not None

        try:
            self._rate_limiter.acquire()
            result = self._ppi.marketdata.book(ticker, tipo, plazo)
            logger.info(
                "book_obtenido",
                ticker=ticker,
                tipo=tipo,
                plazo=plazo,
            )
            return result if isinstance(result, dict) else {}
        except Exception:
            logger.exception(
                "error_obteniendo_book",
                ticker=ticker,
                tipo=tipo,
                plazo=plazo,
            )
            return {}

    def get_intraday(self, ticker: str, tipo: str, plazo: str) -> list:
        """Obtiene datos intraday de un instrumento.

        Args:
            ticker: Simbolo del instrumento.
            tipo: Tipo de instrumento.
            plazo: Plazo de liquidacion.

        Returns:
            Lista con datos intraday. Lista vacia si hay error.
        """
        self._ensure_connected()
        assert self._ppi is not None

        try:
            self._rate_limiter.acquire()
            result = self._ppi.marketdata.intraday(ticker, tipo, plazo)
            logger.info(
                "intraday_obtenido",
                ticker=ticker,
                tipo=tipo,
                plazo=plazo,
            )
            return result if isinstance(result, list) else []
        except Exception:
            logger.exception(
                "error_obteniendo_intraday",
                ticker=ticker,
                tipo=tipo,
                plazo=plazo,
            )
            return []

    # ------------------------------------------------------------------
    # Account data
    # ------------------------------------------------------------------

    def get_balance(self) -> dict:
        """Obtiene el balance disponible de la cuenta.

        Usa ``ppi.account.get_available_balance()`` con el numero de cuenta
        configurado.

        Returns:
            Dict con informacion de balance. Dict vacio si hay error.
        """
        self._ensure_connected()
        assert self._ppi is not None

        try:
            self._rate_limiter.acquire()
            result = self._ppi.account.get_available_balance(PPI_ACCOUNT_NUMBER)
            logger.info("balance_obtenido")
            return result if isinstance(result, dict) else {}
        except Exception:
            logger.exception("error_obteniendo_balance")
            return {}

    def get_balance_and_positions(self) -> dict:
        """Obtiene el balance y posiciones de la cuenta.

        Usa ``ppi.account.get_balance_and_positions()`` con el numero de
        cuenta configurado.

        Returns:
            Dict con balance y posiciones. Dict vacio si hay error.
        """
        self._ensure_connected()
        assert self._ppi is not None

        try:
            self._rate_limiter.acquire()
            result = self._ppi.account.get_balance_and_positions(
                PPI_ACCOUNT_NUMBER
            )
            logger.info("balance_y_posiciones_obtenido")
            return result if isinstance(result, dict) else {}
        except Exception:
            logger.exception("error_obteniendo_balance_y_posiciones")
            return {}

    def get_estimated_bonds(self, estimate: EstimateBonds) -> dict:
        """Obtiene datos de estimacion de bonos (TIR, duration, etc).

        Args:
            estimate: Objeto EstimateBonds con los parametros de estimacion.

        Returns:
            Dict con datos de estimacion. Dict vacio si hay error.
        """
        self._ensure_connected()
        assert self._ppi is not None

        try:
            self._rate_limiter.acquire()
            result = self._ppi.marketdata.estimate_bonds(estimate)
            logger.info("estimacion_bonos_obtenida", estimate=str(estimate))
            return result if isinstance(result, dict) else {}
        except Exception:
            logger.exception("error_obteniendo_estimacion_bonos")
            return {}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        """Verifica que el wrapper esta conectado. Lanza RuntimeError si no."""
        if not self._connected or self._ppi is None:
            raise RuntimeError(
                "PPIWrapper no esta conectado. Llamar a connect() primero."
            )

    def _handle_ws_connect(self, *args: Any, **kwargs: Any) -> None:
        """Handler de conexion exitosa del WebSocket."""
        logger.info("websocket_connect_handler", args=str(args))

    def _handle_market_data(self, data: Any) -> None:
        """Handler interno para datos de mercado recibidos por WebSocket."""
        parsed = data if isinstance(data, dict) else {"raw": data}

        for callback in self._market_data_callbacks:
            try:
                callback(parsed)
            except Exception:
                logger.exception("error_en_callback_market_data")

    def _handle_account_data(self, data: Any) -> None:
        """Handler interno para datos de cuenta recibidos por WebSocket."""
        parsed = data if isinstance(data, dict) else {"raw": data}

        for callback in self._account_data_callbacks:
            try:
                callback(parsed)
            except Exception:
                logger.exception("error_en_callback_account_data")

    def _handle_disconnect(self, *args: Any, **kwargs: Any) -> None:
        """Handler de desconexion del WebSocket. Inicia reconexion con backoff."""
        logger.warning("websocket_desconectado", args=str(args))

        with self._reconnect_lock:
            if self._reconnecting:
                return
            self._reconnecting = True

        thread = threading.Thread(
            target=self._reconnect_loop,
            daemon=True,
            name="ppi-reconnect",
        )
        thread.start()

    def _reconnect_loop(self) -> None:
        """Loop de reconexion con backoff exponencial.

        Re-autentica, reconecta WebSocket y re-suscribe todos los
        instrumentos previamente suscriptos.
        """
        backoff = self._INITIAL_BACKOFF_SECONDS

        while True:
            logger.info(
                "intentando_reconexion",
                backoff_seconds=backoff,
            )
            time.sleep(backoff)

            try:
                # Re-login para refrescar tokens
                assert self._ppi is not None
                self._ppi.account.login_api(PPI_PUBLIC_KEY, PPI_PRIVATE_KEY)

                # Reconectar WebSocket
                self._ppi.realtime.connect_to_market_data(
                    self._handle_ws_connect,
                    self._handle_disconnect,
                    self._handle_market_data,
                )
                self._ppi.realtime.connect_to_account(
                    self._handle_ws_connect,
                    self._handle_disconnect,
                    self._handle_account_data,
                )
                self._ppi.realtime.subscribe_to_account_data(
                    PPI_ACCOUNT_NUMBER
                )
                self._ppi.realtime.start_connections()

                # Re-suscribir instrumentos
                self._resubscribe_all()

                self._reconnect_backoff = self._INITIAL_BACKOFF_SECONDS
                logger.info("reconexion_exitosa")

                with self._reconnect_lock:
                    self._reconnecting = False
                return
            except Exception:
                logger.exception(
                    "error_en_reconexion",
                    proximo_intento_seconds=min(
                        backoff * 2, self._MAX_BACKOFF_SECONDS
                    ),
                )
                backoff = min(backoff * 2, self._MAX_BACKOFF_SECONDS)

    def _resubscribe_all(self) -> None:
        """Re-suscribe todos los instrumentos despues de una reconexion."""
        assert self._ppi is not None

        for ticker, tipo, plazo in self._subscribed_instruments:
            try:
                self._rate_limiter.acquire()
                self._ppi.realtime.subscribe_to_element(
                    Instrument(ticker, tipo, plazo)
                )
                logger.info(
                    "re_suscripcion",
                    ticker=ticker,
                    tipo=tipo,
                    plazo=plazo,
                )
            except Exception:
                logger.exception(
                    "error_re_suscribiendo",
                    ticker=ticker,
                    tipo=tipo,
                    plazo=plazo,
                )
