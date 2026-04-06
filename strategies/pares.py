"""Estrategia de spread trading (pairs trading) entre activos correlacionados.

Monitorea el z-score del spread entre pares de acciones con alta correlacion
historica. Abre posiciones market-neutral cuando el spread se desvia
significativamente de su media.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import structlog

from strategies.base import Signal, Strategy

logger = structlog.get_logger(__name__)

# Parametros configurables
ZSCORE_ENTRADA: float = 2.0
ZSCORE_SALIDA: float = 0.5
LOOKBACK_RUEDAS: int = 60
PLAZO: str = "A-48HS"
NOMINALES_BASE: float = 10_000.0

# Pares con alta correlacion historica
PARES: list[tuple[str, str]] = [
    ("GGAL", "BMA"),    # Bancos
    ("PAMP", "TRAN"),   # Utilities
    ("GGAL", "SUPV"),   # Bancos
]


class ParesStrategy(Strategy):
    """Spread trading entre pares de acciones correlacionadas.

    Calcula el z-score del spread (ratio de precios) entre pares definidos
    usando una ventana de 60 ruedas. Genera senales cuando el z-score
    supera los umbrales de entrada, y cierra cuando vuelve a la zona neutral.

    z-score = (spread_actual - media_spread) / std_spread

    Si z-score > 2.0: activo A esta caro vs B -> vender A, comprar B.
    Si z-score < -2.0: activo A esta barato vs B -> comprar A, vender B.
    Cerrar cuando z-score vuelve a +/- 0.5.
    """

    name: str = "pares"
    frecuencia: str = "diaria"
    instrumentos: list[str] = list(
        {ticker for par in PARES for ticker in par}
    )

    def generate_signals(self) -> list[Signal]:
        """Genera senales de pairs trading para todos los pares configurados.

        Returns:
            Lista de senales. Puede contener multiples senales si varios
            pares presentan oportunidad.
        """
        signals: list[Signal] = []

        for ticker_a, ticker_b in PARES:
            pair_signals = self._evaluate_pair(ticker_a, ticker_b)
            signals.extend(pair_signals)

        return signals

    def _evaluate_pair(self, ticker_a: str, ticker_b: str) -> list[Signal]:
        """Evalua un par de acciones para oportunidad de spread trading.

        Args:
            ticker_a: Primer ticker del par.
            ticker_b: Segundo ticker del par.

        Returns:
            Lista de senales (0 o 2 senales: una por cada pata del par).
        """
        pair_label = f"{ticker_a}/{ticker_b}"

        # Obtener datos historicos
        hoy = date.today()
        # Pedir mas dias por fines de semana y feriados
        desde = hoy - timedelta(days=int(LOOKBACK_RUEDAS * 1.6))

        df_a = self.ppi.get_historical(ticker_a, "Acciones", PLAZO, desde, hoy)
        df_b = self.ppi.get_historical(ticker_b, "Acciones", PLAZO, desde, hoy)

        if df_a.empty or df_b.empty:
            self._log.debug(
                "pares.sin_datos_historicos",
                par=pair_label,
                datos_a=len(df_a),
                datos_b=len(df_b),
            )
            return []

        # Normalizar y alinear por fecha
        df_a = self._normalize_dataframe(df_a)
        df_b = self._normalize_dataframe(df_b)

        if "date" not in df_a.columns or "date" not in df_b.columns:
            self._log.debug("pares.sin_columna_fecha", par=pair_label)
            return []

        # Merge por fecha
        merged = pd.merge(
            df_a[["date", "close"]].rename(columns={"close": "close_a"}),
            df_b[["date", "close"]].rename(columns={"close": "close_b"}),
            on="date",
            how="inner",
        )

        if len(merged) < LOOKBACK_RUEDAS:
            self._log.debug(
                "pares.datos_insuficientes",
                par=pair_label,
                ruedas=len(merged),
                requeridas=LOOKBACK_RUEDAS,
            )
            return []

        # Usar las ultimas LOOKBACK_RUEDAS ruedas
        merged = merged.tail(LOOKBACK_RUEDAS).reset_index(drop=True)

        # Calcular spread como ratio log de precios
        if (merged["close_b"] <= 0).any():
            self._log.warning("pares.precios_invalidos", par=pair_label)
            return []

        spread = np.log(merged["close_a"] / merged["close_b"])

        media = spread.mean()
        std = spread.std()

        if std == 0 or np.isnan(std):
            self._log.debug("pares.std_cero", par=pair_label)
            return []

        zscore_actual = (spread.iloc[-1] - media) / std

        self._log.info(
            "pares.zscore",
            par=pair_label,
            zscore=f"{zscore_actual:.4f}",
            spread_actual=f"{spread.iloc[-1]:.4f}",
            media=f"{media:.4f}",
            std=f"{std:.4f}",
        )

        # Verificar posiciones abiertas en este par
        tiene_posicion_par = self._tiene_posicion_par(ticker_a, ticker_b)

        signals: list[Signal] = []

        if tiene_posicion_par:
            # Cerrar si z-score volvio a zona neutral
            if abs(zscore_actual) < ZSCORE_SALIDA:
                signals.extend(
                    self._generate_close_signals(
                        ticker_a, ticker_b, zscore_actual
                    )
                )
        else:
            # Abrir posicion si z-score supera umbral
            if zscore_actual > ZSCORE_ENTRADA:
                # A esta caro vs B: vender A, comprar B
                signals.append(
                    Signal(
                        strategy=self.name,
                        ticker=ticker_a,
                        tipo="Acciones",
                        operacion="VENTA",
                        cantidad=NOMINALES_BASE,
                        precio=None,
                        plazo=PLAZO,
                        motivo=(
                            f"Par {pair_label}: z-score {zscore_actual:.2f} > "
                            f"{ZSCORE_ENTRADA}. {ticker_a} caro vs {ticker_b}."
                        ),
                    )
                )
                signals.append(
                    Signal(
                        strategy=self.name,
                        ticker=ticker_b,
                        tipo="Acciones",
                        operacion="COMPRA",
                        cantidad=NOMINALES_BASE,
                        precio=None,
                        plazo=PLAZO,
                        motivo=(
                            f"Par {pair_label}: z-score {zscore_actual:.2f} > "
                            f"{ZSCORE_ENTRADA}. Comprando {ticker_b} (pata larga)."
                        ),
                    )
                )
            elif zscore_actual < -ZSCORE_ENTRADA:
                # A esta barato vs B: comprar A, vender B
                signals.append(
                    Signal(
                        strategy=self.name,
                        ticker=ticker_a,
                        tipo="Acciones",
                        operacion="COMPRA",
                        cantidad=NOMINALES_BASE,
                        precio=None,
                        plazo=PLAZO,
                        motivo=(
                            f"Par {pair_label}: z-score {zscore_actual:.2f} < "
                            f"-{ZSCORE_ENTRADA}. {ticker_a} barato vs {ticker_b}."
                        ),
                    )
                )
                signals.append(
                    Signal(
                        strategy=self.name,
                        ticker=ticker_b,
                        tipo="Acciones",
                        operacion="VENTA",
                        cantidad=NOMINALES_BASE,
                        precio=None,
                        plazo=PLAZO,
                        motivo=(
                            f"Par {pair_label}: z-score {zscore_actual:.2f} < "
                            f"-{ZSCORE_ENTRADA}. Vendiendo {ticker_b} (pata corta)."
                        ),
                    )
                )

        return signals

    def _generate_close_signals(
        self, ticker_a: str, ticker_b: str, zscore: float
    ) -> list[Signal]:
        """Genera senales para cerrar ambas patas de un par abierto.

        Args:
            ticker_a: Primer ticker del par.
            ticker_b: Segundo ticker del par.
            zscore: Z-score actual del spread.

        Returns:
            Lista de senales de cierre (COMPRA la pata vendida, VENTA la comprada).
        """
        pair_label = f"{ticker_a}/{ticker_b}"
        signals: list[Signal] = []

        try:
            posiciones = self.portfolio.get_posiciones()
        except Exception:
            self._log.exception("pares.error_obteniendo_posiciones_cierre")
            return signals

        for ticker in (ticker_a, ticker_b):
            if ticker in posiciones:
                posicion = posiciones[ticker]
                cantidad = abs(getattr(posicion, "cantidad", 0))
                if cantidad <= 0:
                    continue

                # Si la cantidad es positiva, estamos long -> vender
                # Si es negativa, estamos short -> comprar
                cantidad_raw = getattr(posicion, "cantidad", 0)
                operacion = "VENTA" if cantidad_raw > 0 else "COMPRA"

                signals.append(
                    Signal(
                        strategy=self.name,
                        ticker=ticker,
                        tipo="Acciones",
                        operacion=operacion,
                        cantidad=cantidad,
                        precio=None,
                        plazo=PLAZO,
                        motivo=(
                            f"Par {pair_label}: z-score {zscore:.2f} en zona "
                            f"neutral (< {ZSCORE_SALIDA}). Cerrando posicion."
                        ),
                    )
                )

        return signals

    def _tiene_posicion_par(self, ticker_a: str, ticker_b: str) -> bool:
        """Verifica si hay posiciones abiertas en alguna pata del par.

        Args:
            ticker_a: Primer ticker.
            ticker_b: Segundo ticker.

        Returns:
            True si hay posicion en al menos una pata.
        """
        try:
            posiciones = self.portfolio.get_posiciones()
            for ticker in (ticker_a, ticker_b):
                if ticker in posiciones:
                    pos = posiciones[ticker]
                    if getattr(pos, "strategy", None) == self.name:
                        return True
            return False
        except Exception:
            self._log.exception("pares.error_verificando_posiciones")
            return False

    @staticmethod
    def _normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
        """Normaliza nombres de columnas del DataFrame historico.

        Args:
            df: DataFrame crudo de la API.

        Returns:
            DataFrame con columnas normalizadas y tipos correctos.
        """
        col_map: dict[str, str] = {}
        for col in df.columns:
            lower = col.lower().strip()
            if lower in ("date", "fecha"):
                col_map[col] = "date"
            elif lower in ("close", "cierre"):
                col_map[col] = "close"
            elif lower in ("open", "apertura"):
                col_map[col] = "open"
            elif lower in ("high", "maximo"):
                col_map[col] = "high"
            elif lower in ("low", "minimo"):
                col_map[col] = "low"
            elif lower in ("volume", "volumen"):
                col_map[col] = "volume"

        df = df.rename(columns=col_map)

        if "close" in df.columns:
            df["close"] = pd.to_numeric(df["close"], errors="coerce")

        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)

        return df
