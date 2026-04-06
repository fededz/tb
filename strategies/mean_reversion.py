"""Estrategia de mean reversion intraday en futuros de dolar.

Calcula el VWAP intraday y opera cuando el precio se desvia
significativamente, apostando al retorno a la media. Cierre obligatorio
de posiciones 15 minutos antes del cierre del mercado.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import structlog

from config import ROFEX_CLOSE_HOUR
from strategies.base import Signal, Strategy, TZ_AR

logger = structlog.get_logger(__name__)

# Parametros configurables
UMBRAL_DESVIACION: float = 0.003     # 0.3% del VWAP
CIERRE_ANTICIPADO_MIN: int = 15       # Cerrar posiciones 15 min antes del cierre
MAX_POSICION_INTRADAY: int = 3        # Maximo 3 contratos simultaneos
CONTRATOS_POR_SENAL: float = 1.0

# Meses ROFEX
MESES_ROFEX: list[str] = [
    "ENE", "FEB", "MAR", "ABR", "MAY", "JUN",
    "JUL", "AGO", "SEP", "OCT", "NOV", "DIC",
]


class MeanReversionIntraday(Strategy):
    """Mean reversion intraday sobre futuros de dolar.

    Calcula el VWAP del dia y genera senales cuando el precio se desvia
    mas de un umbral. Cierra todas las posiciones 15 minutos antes del
    cierre del mercado de ROFEX.

    - LONG si precio < VWAP * (1 - umbral)
    - SHORT si precio > VWAP * (1 + umbral)
    - Maximo 3 contratos intraday simultaneos.
    """

    name: str = "mean_reversion"
    frecuencia: str = "intraday"
    instrumentos: list[str] = ["DLR"]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Inicializa la estrategia con estado intraday.

        Mantiene un acumulador de precio*volumen y volumen total para
        calcular VWAP de forma incremental.
        """
        super().__init__(*args, **kwargs)
        self._vwap_fecha: date | None = None
        self._sum_price_volume: float = 0.0
        self._sum_volume: float = 0.0
        self._tick_count: int = 0

    def generate_signals(self) -> list[Signal]:
        """Genera senales de mean reversion intraday.

        Returns:
            Lista con una senal de COMPRA, VENTA o cierre de posiciones.
        """
        now_ar = datetime.now(TZ_AR)

        # Resolver ticker del futuro proximo
        ticker = self._resolve_dlr_ticker()
        if ticker is None:
            self._log.debug("mean_reversion.sin_ticker_dlr")
            return []

        # Verificar si debemos cerrar posiciones por horario
        minutos_al_cierre = (ROFEX_CLOSE_HOUR * 60) - (now_ar.hour * 60 + now_ar.minute)
        if minutos_al_cierre <= CIERRE_ANTICIPADO_MIN:
            self._log.info(
                "mean_reversion.cierre_anticipado",
                minutos_al_cierre=minutos_al_cierre,
            )
            return self._generate_close_all_signals(ticker)

        # Obtener precio actual
        precio_actual = self.ppi.get_current_price(ticker, "Futuros", "INMEDIATA")
        if not precio_actual or precio_actual <= 0:
            self._log.debug(
                "mean_reversion.sin_precio",
                ticker=ticker,
            )
            return []

        # Obtener volumen del book como proxy de volumen del tick
        book = self.ppi.get_book(ticker, "Futuros", "INMEDIATA")
        volumen_tick = self._extract_volume_from_book(book)

        # Actualizar VWAP
        vwap = self._update_vwap(precio_actual, volumen_tick)

        if vwap is None or vwap <= 0:
            self._log.debug(
                "mean_reversion.vwap_no_disponible",
                tick_count=self._tick_count,
            )
            return []

        desviacion = (precio_actual - vwap) / vwap

        self._log.info(
            "mean_reversion.analisis",
            ticker=ticker,
            precio=f"{precio_actual:.2f}",
            vwap=f"{vwap:.2f}",
            desviacion=f"{desviacion:.6f}",
            umbral=UMBRAL_DESVIACION,
            tick_count=self._tick_count,
        )

        # Verificar posiciones actuales
        contratos_abiertos = self._get_contratos_abiertos(ticker)

        signals: list[Signal] = []

        if desviacion < -UMBRAL_DESVIACION:
            # Precio por debajo del VWAP -> senal LONG
            if contratos_abiertos < MAX_POSICION_INTRADAY:
                signals.append(
                    Signal(
                        strategy=self.name,
                        ticker=ticker,
                        tipo="Futuros",
                        operacion="COMPRA",
                        cantidad=CONTRATOS_POR_SENAL,
                        precio=None,
                        plazo="INMEDIATA",
                        motivo=(
                            f"Precio {precio_actual:.2f} < VWAP {vwap:.2f} "
                            f"(desvio {desviacion:.4%}). Mean reversion LONG."
                        ),
                    )
                )

        elif desviacion > UMBRAL_DESVIACION:
            # Precio por encima del VWAP -> senal SHORT (vender)
            if contratos_abiertos < MAX_POSICION_INTRADAY:
                signals.append(
                    Signal(
                        strategy=self.name,
                        ticker=ticker,
                        tipo="Futuros",
                        operacion="VENTA",
                        cantidad=CONTRATOS_POR_SENAL,
                        precio=None,
                        plazo="INMEDIATA",
                        motivo=(
                            f"Precio {precio_actual:.2f} > VWAP {vwap:.2f} "
                            f"(desvio {desviacion:.4%}). Mean reversion SHORT."
                        ),
                    )
                )

        return signals

    def _update_vwap(self, precio: float, volumen: float) -> float | None:
        """Actualiza el calculo incremental del VWAP intraday.

        Resetea el acumulador si cambio el dia.

        Args:
            precio: Precio actual.
            volumen: Volumen del tick. Si es 0, usa 1 como proxy.

        Returns:
            VWAP actual, o None si no hay datos suficientes.
        """
        hoy = date.today()

        # Resetear al cambio de dia
        if self._vwap_fecha != hoy:
            self._sum_price_volume = 0.0
            self._sum_volume = 0.0
            self._tick_count = 0
            self._vwap_fecha = hoy

        # Usar volumen minimo de 1 si no hay dato
        vol = max(volumen, 1.0)

        self._sum_price_volume += precio * vol
        self._sum_volume += vol
        self._tick_count += 1

        if self._sum_volume <= 0:
            return None

        return self._sum_price_volume / self._sum_volume

    def _generate_close_all_signals(self, ticker: str) -> list[Signal]:
        """Genera senales para cerrar todas las posiciones abiertas intraday.

        Args:
            ticker: Ticker del futuro.

        Returns:
            Lista de senales de cierre.
        """
        signals: list[Signal] = []

        try:
            posiciones = self.portfolio.get_posiciones()
        except Exception:
            self._log.exception("mean_reversion.error_obteniendo_posiciones_cierre")
            return signals

        if ticker not in posiciones:
            return signals

        posicion = posiciones[ticker]
        if getattr(posicion, "strategy", None) != self.name:
            return signals

        cantidad = getattr(posicion, "cantidad", 0)
        if cantidad == 0:
            return signals

        # Si estamos long, vendemos. Si estamos short, compramos.
        if cantidad > 0:
            operacion = "VENTA"
        else:
            operacion = "COMPRA"

        signals.append(
            Signal(
                strategy=self.name,
                ticker=ticker,
                tipo="Futuros",
                operacion=operacion,
                cantidad=abs(cantidad),
                precio=None,
                plazo="INMEDIATA",
                motivo=(
                    f"Cierre obligatorio {CIERRE_ANTICIPADO_MIN} min antes "
                    f"del cierre de mercado. Cerrando {abs(cantidad)} contratos."
                ),
            )
        )

        return signals

    def _get_contratos_abiertos(self, ticker: str) -> int:
        """Obtiene el numero de contratos abiertos en el ticker.

        Args:
            ticker: Ticker del futuro.

        Returns:
            Cantidad absoluta de contratos abiertos para esta estrategia.
        """
        try:
            posiciones = self.portfolio.get_posiciones()
            if ticker not in posiciones:
                return 0

            posicion = posiciones[ticker]
            if getattr(posicion, "strategy", None) != self.name:
                return 0

            return abs(getattr(posicion, "cantidad", 0))
        except Exception:
            self._log.exception("mean_reversion.error_contando_contratos")
            return 0

    def _resolve_dlr_ticker(self) -> str | None:
        """Resuelve el ticker del futuro de dolar mas proximo con precio.

        Returns:
            Ticker completo (ej: DLR/JUN25) o None.
        """
        hoy = date.today()

        for meses_adelante in range(0, 4):
            anio = hoy.year + (hoy.month + meses_adelante - 1) // 12
            mes = (hoy.month + meses_adelante - 1) % 12 + 1

            ticker = f"DLR/{MESES_ROFEX[mes - 1]}{str(anio)[-2:]}"

            precio = self.ppi.get_current_price(ticker, "Futuros", "INMEDIATA")
            if precio and precio > 0:
                return ticker

        return None

    @staticmethod
    def _extract_volume_from_book(book: dict) -> float:
        """Extrae un proxy de volumen del order book.

        Suma las cantidades de los mejores bids y asks como estimacion.

        Args:
            book: Dict con estructura de order book.

        Returns:
            Volumen estimado. 0 si no hay datos.
        """
        if not book:
            return 0.0

        total = 0.0

        for side in ("bids", "asks", "ofertas_compra", "ofertas_venta"):
            entries = book.get(side, [])
            if isinstance(entries, list):
                for entry in entries[:3]:  # Top 3 niveles
                    if isinstance(entry, dict):
                        qty = entry.get("quantity") or entry.get("cantidad", 0)
                        total += float(qty)

        return total
