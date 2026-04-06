"""Estrategia de trend following en futuros.

Usa cruce de medias moviles (MA20 y MA50) confirmado con filtro ATR
para generar senales direccionales en futuros de ROFEX.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import structlog

from strategies.base import Signal, Strategy

logger = structlog.get_logger(__name__)

# Parametros configurables
MA_RAPIDA: int = 20
MA_LENTA: int = 50
ATR_PERIODO: int = 14
ATR_MULTIPLICADOR: float = 1.5
# Umbral minimo de ATR relativo para filtrar mercados laterales.
# Solo se genera senal si ATR/precio > este valor.
ATR_FILTRO_MINIMO: float = 0.005  # 0.5% del precio
CONTRATOS_BASE: float = 5.0

# Instrumentos futuros a operar
INSTRUMENTOS_FUTUROS: list[dict[str, str]] = [
    {"ticker_base": "DLR", "tipo": "Futuros", "plazo": "INMEDIATA"},
    {"ticker_base": "RFX20", "tipo": "Futuros", "plazo": "INMEDIATA"},
    {"ticker_base": "SOJ", "tipo": "Futuros", "plazo": "INMEDIATA"},
]

# Meses ROFEX para ticker lookup
MESES_ROFEX: list[str] = [
    "ENE", "FEB", "MAR", "ABR", "MAY", "JUN",
    "JUL", "AGO", "SEP", "OCT", "NOV", "DIC",
]


class TrendFollowing(Strategy):
    """Trend following en futuros con cruce de medias moviles.

    Genera senal LONG cuando la media rapida (20 ruedas) cruza por encima
    de la media lenta (50 ruedas). Genera senal SHORT o cierre cuando la
    media rapida cruza por debajo. El filtro ATR descarta senales en
    mercados con baja volatilidad / laterales.

    Stop loss dinamico: precio +/- 1.5 * ATR.
    """

    name: str = "trend_following"
    frecuencia: str = "diaria"
    instrumentos: list[str] = ["DLR", "RFX20", "SOJ"]

    def generate_signals(self) -> list[Signal]:
        """Genera senales de trend following para cada instrumento.

        Returns:
            Lista de senales. Vacia si no hay cruces o ATR es insuficiente.
        """
        signals: list[Signal] = []

        for instrumento in INSTRUMENTOS_FUTUROS:
            ticker = self._resolve_ticker(instrumento["ticker_base"])
            if ticker is None:
                self._log.debug(
                    "trend_following.ticker_no_resuelto",
                    base=instrumento["ticker_base"],
                )
                continue

            signal = self._evaluate_instrument(
                ticker=ticker,
                tipo=instrumento["tipo"],
                plazo=instrumento["plazo"],
                ticker_base=instrumento["ticker_base"],
            )
            if signal is not None:
                signals.append(signal)

        return signals

    def _evaluate_instrument(
        self,
        ticker: str,
        tipo: str,
        plazo: str,
        ticker_base: str,
    ) -> Signal | None:
        """Evalua un instrumento individual para senal de tendencia.

        Args:
            ticker: Ticker completo del futuro (ej: DLR/JUN25).
            tipo: Tipo de instrumento.
            plazo: Plazo de liquidacion.
            ticker_base: Nombre base del futuro (ej: DLR).

        Returns:
            Signal si hay cruce confirmado, None si no.
        """
        # Necesitamos al menos MA_LENTA + 1 ruedas de datos
        dias_necesarios = MA_LENTA + ATR_PERIODO + 5  # Margen por feriados
        hasta = date.today()
        desde = hasta - timedelta(days=int(dias_necesarios * 1.5))  # Compensar fines de semana

        df = self.ppi.get_historical(ticker, tipo, plazo, desde, hasta)

        if df.empty:
            self._log.debug(
                "trend_following.sin_datos_historicos",
                ticker=ticker,
            )
            return None

        # Normalizar columnas
        df = self._normalize_dataframe(df)

        if len(df) < MA_LENTA + 1:
            self._log.debug(
                "trend_following.datos_insuficientes",
                ticker=ticker,
                ruedas=len(df),
                requeridas=MA_LENTA + 1,
            )
            return None

        # Calcular medias moviles
        df["ma_rapida"] = df["close"].rolling(window=MA_RAPIDA).mean()
        df["ma_lenta"] = df["close"].rolling(window=MA_LENTA).mean()

        # Calcular ATR
        df["atr"] = self._calculate_atr(df, ATR_PERIODO)

        # Eliminar filas con NaN
        df = df.dropna(subset=["ma_rapida", "ma_lenta", "atr"])

        if len(df) < 2:
            return None

        # Detectar cruce en las ultimas dos ruedas
        current = df.iloc[-1]
        previous = df.iloc[-2]

        ma_rapida_hoy = current["ma_rapida"]
        ma_lenta_hoy = current["ma_lenta"]
        ma_rapida_ayer = previous["ma_rapida"]
        ma_lenta_ayer = previous["ma_lenta"]
        atr_actual = current["atr"]
        precio_actual = current["close"]

        # Filtro ATR: descartar si volatilidad es muy baja
        if precio_actual > 0:
            atr_relativo = atr_actual / precio_actual
            if atr_relativo < ATR_FILTRO_MINIMO:
                self._log.debug(
                    "trend_following.atr_insuficiente",
                    ticker=ticker,
                    atr_relativo=f"{atr_relativo:.6f}",
                    minimo=ATR_FILTRO_MINIMO,
                )
                return None

        self._log.info(
            "trend_following.analisis",
            ticker=ticker,
            ma_rapida=f"{ma_rapida_hoy:.2f}",
            ma_lenta=f"{ma_lenta_hoy:.2f}",
            atr=f"{atr_actual:.2f}",
            precio=f"{precio_actual:.2f}",
        )

        tiene_posicion = self._tiene_posicion(ticker)

        # Cruce alcista: MA rapida cruza por encima de MA lenta
        cruce_alcista = (ma_rapida_ayer <= ma_lenta_ayer) and (ma_rapida_hoy > ma_lenta_hoy)
        # Cruce bajista: MA rapida cruza por debajo de MA lenta
        cruce_bajista = (ma_rapida_ayer >= ma_lenta_ayer) and (ma_rapida_hoy < ma_lenta_hoy)

        stop_loss = precio_actual - ATR_MULTIPLICADOR * atr_actual

        if cruce_alcista and not tiene_posicion:
            return Signal(
                strategy=self.name,
                ticker=ticker,
                tipo=tipo,
                operacion="COMPRA",
                cantidad=CONTRATOS_BASE,
                precio=None,
                plazo=plazo,
                motivo=(
                    f"Cruce alcista MA{MA_RAPIDA}/{MA_LENTA} en {ticker_base}. "
                    f"MA rapida: {ma_rapida_hoy:.2f}, MA lenta: {ma_lenta_hoy:.2f}. "
                    f"ATR: {atr_actual:.2f}. Stop loss: {stop_loss:.2f}"
                ),
            )

        if cruce_bajista and tiene_posicion:
            return Signal(
                strategy=self.name,
                ticker=ticker,
                tipo=tipo,
                operacion="VENTA",
                cantidad=CONTRATOS_BASE,
                precio=None,
                plazo=plazo,
                motivo=(
                    f"Cruce bajista MA{MA_RAPIDA}/{MA_LENTA} en {ticker_base}. "
                    f"MA rapida: {ma_rapida_hoy:.2f}, MA lenta: {ma_lenta_hoy:.2f}. "
                    f"Cerrando posicion."
                ),
            )

        return None

    @staticmethod
    def _calculate_atr(df: pd.DataFrame, periodo: int) -> pd.Series:
        """Calcula el Average True Range (ATR).

        Args:
            df: DataFrame con columnas high, low, close.
            periodo: Ventana del ATR.

        Returns:
            Series con el ATR.
        """
        high = df["high"]
        low = df["low"]
        close_prev = df["close"].shift(1)

        tr1 = high - low
        tr2 = (high - close_prev).abs()
        tr3 = (low - close_prev).abs()

        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return true_range.rolling(window=periodo).mean()

    @staticmethod
    def _normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
        """Normaliza nombres de columnas del DataFrame historico.

        Args:
            df: DataFrame crudo de la API.

        Returns:
            DataFrame con columnas normalizadas: date, open, high, low, close, volume.
        """
        col_map: dict[str, str] = {}
        for col in df.columns:
            lower = col.lower().strip()
            if lower in ("date", "fecha"):
                col_map[col] = "date"
            elif lower in ("open", "apertura"):
                col_map[col] = "open"
            elif lower in ("high", "maximo"):
                col_map[col] = "high"
            elif lower in ("low", "minimo"):
                col_map[col] = "low"
            elif lower in ("close", "cierre"):
                col_map[col] = "close"
            elif lower in ("volume", "volumen"):
                col_map[col] = "volume"

        df = df.rename(columns=col_map)

        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)

        return df

    def _resolve_ticker(self, ticker_base: str) -> str | None:
        """Resuelve el ticker base a un ticker de futuro concreto.

        Busca el proximo vencimiento disponible con precio.

        Args:
            ticker_base: Nombre base del futuro (ej: DLR, RFX20, SOJ).

        Returns:
            Ticker completo (ej: DLR/JUN25) o None si no encuentra.
        """
        hoy = date.today()

        for meses_adelante in range(0, 6):
            anio = hoy.year + (hoy.month + meses_adelante - 1) // 12
            mes = (hoy.month + meses_adelante - 1) % 12 + 1

            ticker = f"{ticker_base}/{MESES_ROFEX[mes - 1]}{str(anio)[-2:]}"

            precio = self.ppi.get_current_price(ticker, "Futuros", "INMEDIATA")
            if precio and precio > 0:
                return ticker

        return None

    def _tiene_posicion(self, ticker: str) -> bool:
        """Verifica si hay posicion abierta en el ticker.

        Args:
            ticker: Ticker a verificar.

        Returns:
            True si hay posicion abierta.
        """
        try:
            posiciones = self.portfolio.get_posiciones()
            return ticker in posiciones
        except Exception:
            self._log.exception(
                "trend_following.error_verificando_posicion",
                ticker=ticker,
            )
            return False
