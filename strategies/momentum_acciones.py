"""Estrategia de momentum semanal en acciones del Merval.

Cada lunes rankea las acciones del universo por retorno de las ultimas
4 semanas y compra las top N, vendiendo las bottom N o cerrando posiciones
que ya no estan en el ranking superior.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import structlog

from strategies.base import Signal, Strategy

logger = structlog.get_logger(__name__)

# Parametros configurables
LOOKBACK_SEMANAS: int = 4
TOP_N: int = 5
BOTTOM_N: int = 5
PLAZO: str = "A-48HS"
NOMINALES_BASE: float = 10_000.0

# Universo de acciones Merval
UNIVERSO: list[str] = [
    "GGAL", "BBAR", "BMA", "YPF", "PAMP",
    "TXAR", "ALUA", "CRES", "SUPV", "TECO2",
    "COME", "BYMA", "CVH", "EDN", "HARG",
    "LOMA", "MIRG", "TRAN", "VALO", "CEPU",
]


class MomentumAcciones(Strategy):
    """Momentum semanal en acciones del Merval.

    Cada lunes calcula el retorno de las ultimas 4 semanas para cada accion
    del universo, rankea de mayor a menor y:
    - Compra las top N (default 5) que no estan en cartera.
    - Vende posiciones existentes que ya no estan en el top N.

    El rebalanceo es semanal con plazo A-48HS.
    """

    name: str = "momentum_acciones"
    frecuencia: str = "semanal"
    instrumentos: list[str] = UNIVERSO

    def generate_signals(self) -> list[Signal]:
        """Genera senales de momentum basadas en retornos de 4 semanas.

        Returns:
            Lista de senales de COMPRA y VENTA para rebalanceo.
        """
        rankings = self._calculate_rankings()

        if not rankings:
            self._log.warning("momentum.sin_rankings_disponibles")
            return []

        # Ordenar por retorno descendente
        rankings.sort(key=lambda x: x["retorno"], reverse=True)

        top_tickers = [r["ticker"] for r in rankings[:TOP_N]]
        bottom_tickers = [r["ticker"] for r in rankings[-BOTTOM_N:]]

        self._log.info(
            "momentum.rankings",
            top=top_tickers,
            bottom=bottom_tickers,
            total_evaluados=len(rankings),
        )

        # Log completo del ranking
        for i, r in enumerate(rankings):
            self._log.debug(
                "momentum.ranking_detalle",
                posicion=i + 1,
                ticker=r["ticker"],
                retorno=f"{r['retorno']:.4f}",
            )

        signals: list[Signal] = []

        # Obtener posiciones actuales de esta estrategia
        posiciones_actuales = self._get_posiciones_estrategia()

        # Vender posiciones que ya no estan en top N
        for ticker in posiciones_actuales:
            if ticker not in top_tickers:
                cantidad = posiciones_actuales[ticker]
                if cantidad > 0:
                    signals.append(
                        Signal(
                            strategy=self.name,
                            ticker=ticker,
                            tipo="Acciones",
                            operacion="VENTA",
                            cantidad=cantidad,
                            precio=None,
                            plazo=PLAZO,
                            motivo=f"Fuera del top {TOP_N} de momentum. Rebalanceo semanal.",
                        )
                    )

        # Comprar top N que no estan en cartera
        for ticker in top_tickers:
            if ticker not in posiciones_actuales:
                retorno_info = next(
                    (r for r in rankings if r["ticker"] == ticker), None
                )
                retorno_str = (
                    f"{retorno_info['retorno']:.2%}" if retorno_info else "N/A"
                )
                signals.append(
                    Signal(
                        strategy=self.name,
                        ticker=ticker,
                        tipo="Acciones",
                        operacion="COMPRA",
                        cantidad=NOMINALES_BASE,
                        precio=None,
                        plazo=PLAZO,
                        motivo=(
                            f"Top {TOP_N} momentum. Retorno {LOOKBACK_SEMANAS} semanas: "
                            f"{retorno_str}."
                        ),
                    )
                )

        return signals

    def _calculate_rankings(self) -> list[dict[str, float | str]]:
        """Calcula el retorno de cada accion en las ultimas N semanas.

        Returns:
            Lista de dicts con ticker y retorno. Solo incluye acciones
            con datos suficientes.
        """
        rankings: list[dict[str, float | str]] = []
        hoy = date.today()
        desde = hoy - timedelta(weeks=LOOKBACK_SEMANAS + 1)  # Margen

        for ticker in UNIVERSO:
            retorno = self._get_retorno(ticker, desde, hoy)
            if retorno is not None:
                rankings.append({"ticker": ticker, "retorno": retorno})
            else:
                self._log.debug(
                    "momentum.sin_datos_para_ticker",
                    ticker=ticker,
                )

        if len(rankings) < TOP_N:
            self._log.warning(
                "momentum.rankings_insuficientes",
                disponibles=len(rankings),
                requeridos=TOP_N,
            )
            return []

        return rankings

    def _get_retorno(
        self, ticker: str, desde: date, hasta: date
    ) -> float | None:
        """Calcula el retorno simple de un ticker en el periodo.

        Args:
            ticker: Simbolo de la accion.
            desde: Fecha de inicio del periodo.
            hasta: Fecha de fin del periodo.

        Returns:
            Retorno como float (ej: 0.15 = 15%), o None si no hay datos.
        """
        try:
            df = self.ppi.get_historical(ticker, "Acciones", PLAZO, desde, hasta)

            if df.empty:
                return None

            # Normalizar columna de cierre
            close_col = None
            for col in df.columns:
                if col.lower().strip() in ("close", "cierre"):
                    close_col = col
                    break

            if close_col is None:
                return None

            precios = pd.to_numeric(df[close_col], errors="coerce").dropna()

            if len(precios) < 5:  # Minimo de datos razonable
                return None

            precio_inicio = float(precios.iloc[0])
            precio_fin = float(precios.iloc[-1])

            if precio_inicio <= 0:
                return None

            retorno = (precio_fin - precio_inicio) / precio_inicio
            return retorno

        except Exception:
            self._log.exception(
                "momentum.error_calculando_retorno",
                ticker=ticker,
            )
            return None

    def _get_posiciones_estrategia(self) -> dict[str, float]:
        """Obtiene las posiciones abiertas de esta estrategia.

        Returns:
            Dict ticker -> cantidad para posiciones de esta estrategia.
        """
        try:
            posiciones = self.portfolio.get_posiciones()
            resultado: dict[str, float] = {}

            for ticker, posicion in posiciones.items():
                if getattr(posicion, "strategy", None) == self.name:
                    cantidad = getattr(posicion, "cantidad", 0)
                    if cantidad > 0:
                        resultado[ticker] = cantidad

            return resultado
        except Exception:
            self._log.exception("momentum.error_obteniendo_posiciones")
            return {}
