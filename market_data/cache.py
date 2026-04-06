"""Cache en memoria de precios actuales, actualizado desde WebSocket.

Almacena el ultimo precio conocido de cada instrumento con timestamp
para detectar datos obsoletos. Thread-safe mediante threading.Lock.
"""

import threading
import time
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class CacheEntry:
    """Entrada individual del cache de precios."""

    price: float
    volume: float | None
    timestamp: float  # time.time() epoch


class MarketDataCache:
    """Cache en memoria de precios de mercado en tiempo real.

    Cada instrumento se identifica por la clave "TICKER:TIPO:PLAZO".
    Las actualizaciones llegan desde el WebSocket via RealtimeHandler.
    """

    def __init__(self) -> None:
        self._data: dict[str, CacheEntry] = {}
        self._lock: threading.Lock = threading.Lock()
        logger.info("market_data_cache.inicializado")

    @staticmethod
    def _key(ticker: str, tipo: str, plazo: str) -> str:
        """Genera la clave interna del cache."""
        return f"{ticker}:{tipo}:{plazo}"

    def update(
        self,
        ticker: str,
        tipo: str,
        plazo: str,
        price: float,
        volume: float | None = None,
        timestamp: float | None = None,
    ) -> None:
        """Actualiza el precio de un instrumento en el cache.

        Args:
            ticker: Simbolo del instrumento (ej: "GGAL", "AL30", "DLR/JUN25").
            tipo: Tipo de instrumento (ej: "Acciones", "Bonos", "Futuros").
            plazo: Plazo de liquidacion (ej: "A-48HS", "INMEDIATA").
            price: Ultimo precio conocido.
            volume: Volumen operado (opcional).
            timestamp: Epoch del dato. Si es None, usa time.time().
        """
        key = self._key(ticker, tipo, plazo)
        ts = timestamp if timestamp is not None else time.time()
        with self._lock:
            self._data[key] = CacheEntry(price=price, volume=volume, timestamp=ts)
        logger.debug(
            "market_data_cache.actualizado",
            ticker=ticker,
            tipo=tipo,
            plazo=plazo,
            price=price,
        )

    def get_price(self, ticker: str, tipo: str, plazo: str) -> float | None:
        """Obtiene el ultimo precio conocido de un instrumento.

        Args:
            ticker: Simbolo del instrumento.
            tipo: Tipo de instrumento.
            plazo: Plazo de liquidacion.

        Returns:
            El precio como float, o None si no hay dato en cache.
        """
        key = self._key(ticker, tipo, plazo)
        with self._lock:
            entry = self._data.get(key)
        if entry is None:
            return None
        return entry.price

    def get_all(self) -> dict[str, dict]:
        """Retorna todos los precios en cache.

        Returns:
            Diccionario con claves "TICKER:TIPO:PLAZO" y valores con
            price, volume y timestamp.
        """
        with self._lock:
            return {
                key: {
                    "price": entry.price,
                    "volume": entry.volume,
                    "timestamp": entry.timestamp,
                }
                for key, entry in self._data.items()
            }

    def get_age_seconds(self, ticker: str, tipo: str, plazo: str) -> float | None:
        """Calcula la antiguedad en segundos del dato en cache.

        Args:
            ticker: Simbolo del instrumento.
            tipo: Tipo de instrumento.
            plazo: Plazo de liquidacion.

        Returns:
            Segundos desde la ultima actualizacion, o None si no hay dato.
        """
        key = self._key(ticker, tipo, plazo)
        with self._lock:
            entry = self._data.get(key)
        if entry is None:
            return None
        return time.time() - entry.timestamp

    def clear(self) -> None:
        """Limpia todo el cache."""
        with self._lock:
            self._data.clear()
        logger.info("market_data_cache.limpiado")
