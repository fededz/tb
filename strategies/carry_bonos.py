"""Estrategia de carry trade en bonos CER y Lecaps.

Compara la TIR de bonos CER cortos contra el costo de fondeo implicito
(tasa de cauciones). Si el spread es positivo y suficiente, compra el bono.
Monitorea variacion del tipo de cambio como stop loss.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import structlog

from strategies.base import Signal, Strategy

logger = structlog.get_logger(__name__)

# Parametros configurables
SPREAD_MINIMO: float = 0.05       # 5% sobre tasa de fondeo
MAX_DURATION: float = 2.0          # Duration modificada maxima en anios
STOP_LOSS_FX_PCT: float = 0.05    # Salir si dolar sube mas de 5% en el dia
TASA_FONDEO_DEFAULT: float = 0.30  # 30% anual — fallback si no se obtiene caucion
NOMINALES_BASE: float = 100_000.0

# Bonos CER cortos y Lecaps a evaluar
BONOS_UNIVERSO: list[str] = ["TX26", "T2X5", "T4X4", "TZXD5"]
LECAPS_UNIVERSO: list[str] = ["S31M5", "S30J5", "S29G5"]


class CarryBonos(Strategy):
    """Carry trade en bonos CER cortos y Lecaps.

    Evalua la TIR de bonos del universo contra el costo de fondeo.
    Si TIR > fondeo + spread_minimo y la duration es aceptable, compra.
    Monitorea variacion cambiaria para stop loss de emergencia.
    """

    name: str = "carry_bonos"
    frecuencia: str = "diaria"
    instrumentos: list[str] = BONOS_UNIVERSO + LECAPS_UNIVERSO

    def generate_signals(self) -> list[Signal]:
        """Genera senales de carry en bonos.

        Returns:
            Lista de senales de COMPRA o VENTA. Vacia si no hay oportunidad.
        """
        # Verificar stop loss por tipo de cambio
        if self._check_fx_stop_loss():
            return self._generate_exit_signals()

        tasa_fondeo = self._get_tasa_fondeo()

        self._log.info(
            "carry_bonos.tasa_fondeo",
            tasa=f"{tasa_fondeo:.4f}",
        )

        signals: list[Signal] = []

        for ticker in BONOS_UNIVERSO + LECAPS_UNIVERSO:
            signal = self._evaluate_bono(ticker, tasa_fondeo)
            if signal is not None:
                signals.append(signal)

        return signals

    def _evaluate_bono(self, ticker: str, tasa_fondeo: float) -> Signal | None:
        """Evalua un bono individual para oportunidad de carry.

        Args:
            ticker: Simbolo del bono.
            tasa_fondeo: Tasa de fondeo anualizada.

        Returns:
            Signal de COMPRA si hay oportunidad, None si no.
        """
        estimacion = self.ppi.get_estimated_bonds(ticker)

        if not estimacion:
            self._log.debug(
                "carry_bonos.sin_estimacion",
                ticker=ticker,
            )
            return None

        tir = estimacion.get("tir") or estimacion.get("yieldToMaturity")
        duration = estimacion.get("modifiedDuration") or estimacion.get("duration")

        if tir is None:
            self._log.debug("carry_bonos.sin_tir", ticker=ticker)
            return None

        tir = float(tir)

        if duration is not None:
            duration = float(duration)
            if duration > MAX_DURATION:
                self._log.debug(
                    "carry_bonos.duration_excedida",
                    ticker=ticker,
                    duration=duration,
                    max_duration=MAX_DURATION,
                )
                return None

        spread = tir - tasa_fondeo

        self._log.info(
            "carry_bonos.evaluacion",
            ticker=ticker,
            tir=f"{tir:.4f}",
            spread=f"{spread:.4f}",
            duration=duration,
        )

        tiene_posicion = self._tiene_posicion(ticker)

        if spread > SPREAD_MINIMO and not tiene_posicion:
            # Determinar tipo y plazo segun el instrumento
            tipo = "Bonos" if ticker in BONOS_UNIVERSO else "Letras"
            plazo = "A-48HS"

            return Signal(
                strategy=self.name,
                ticker=ticker,
                tipo=tipo,
                operacion="COMPRA",
                cantidad=NOMINALES_BASE,
                precio=None,
                plazo=plazo,
                motivo=(
                    f"TIR {tir:.2%} > fondeo {tasa_fondeo:.2%} + spread minimo "
                    f"{SPREAD_MINIMO:.2%}. Spread neto: {spread:.2%}. "
                    f"Duration: {duration}"
                ),
            )

        return None

    def _check_fx_stop_loss(self) -> bool:
        """Verifica si el tipo de cambio subio mas del umbral de stop loss.

        Compara el dolar MEP actual contra el cierre del dia anterior.

        Returns:
            True si se activo el stop loss por FX.
        """
        try:
            precio_al30_ars = self.ppi.get_current_price("AL30", "Bonos", "A-48HS")
            precio_al30d_usd = self.ppi.get_current_price("AL30D", "Bonos", "INMEDIATA")

            if not precio_al30_ars or not precio_al30d_usd or precio_al30d_usd == 0:
                return False

            mep_actual = precio_al30_ars / precio_al30d_usd

            # Obtener MEP de cierre anterior desde historico
            hoy = date.today()
            ayer = hoy - timedelta(days=1)
            # Retroceder si fue fin de semana
            while ayer.weekday() > 4:
                ayer -= timedelta(days=1)

            hist_ars = self.ppi.get_historical("AL30", "Bonos", "A-48HS", ayer, ayer)
            hist_usd = self.ppi.get_historical("AL30D", "Bonos", "INMEDIATA", ayer, ayer)

            if hist_ars.empty or hist_usd.empty:
                return False

            cierre_ars = float(hist_ars.iloc[-1].get("close", 0))
            cierre_usd = float(hist_usd.iloc[-1].get("close", 0))

            if cierre_usd == 0:
                return False

            mep_cierre = cierre_ars / cierre_usd
            variacion = (mep_actual - mep_cierre) / mep_cierre

            self._log.info(
                "carry_bonos.variacion_fx",
                mep_actual=mep_actual,
                mep_cierre=mep_cierre,
                variacion=f"{variacion:.4f}",
            )

            if variacion > STOP_LOSS_FX_PCT:
                self._log.warning(
                    "carry_bonos.stop_loss_fx_activado",
                    variacion=f"{variacion:.4f}",
                    umbral=f"{STOP_LOSS_FX_PCT:.4f}",
                )
                return True

        except Exception:
            self._log.exception("carry_bonos.error_check_fx_stop")

        return False

    def _generate_exit_signals(self) -> list[Signal]:
        """Genera senales de venta para todas las posiciones abiertas de esta estrategia.

        Returns:
            Lista de senales de VENTA.
        """
        signals: list[Signal] = []

        try:
            posiciones = self.portfolio.get_posiciones()
        except Exception:
            self._log.exception("carry_bonos.error_obteniendo_posiciones")
            return signals

        for ticker, posicion in posiciones.items():
            if getattr(posicion, "strategy", None) != self.name:
                continue

            cantidad = getattr(posicion, "cantidad", 0)
            if cantidad <= 0:
                continue

            tipo = "Bonos" if ticker in BONOS_UNIVERSO else "Letras"

            signals.append(
                Signal(
                    strategy=self.name,
                    ticker=ticker,
                    tipo=tipo,
                    operacion="VENTA",
                    cantidad=cantidad,
                    precio=None,
                    plazo="A-48HS",
                    motivo="Stop loss por variacion de tipo de cambio activado",
                )
            )

        return signals

    def _get_tasa_fondeo(self) -> float:
        """Obtiene la tasa de fondeo del mercado de cauciones.

        Intenta obtener la tasa actual. Si no puede, usa el default.

        Returns:
            Tasa de fondeo anualizada como float.
        """
        try:
            precio = self.ppi.get_current_price("CAUCION", "Cauciones", "INMEDIATA")
            if precio and precio > 0:
                # El precio de caucion a 1 dia se expresa como tasa diaria
                # Anualizar: (1 + tasa_diaria)^365 - 1
                tasa_diaria = precio / 100.0
                tasa_anual = (1 + tasa_diaria) ** 365 - 1
                self._log.info(
                    "carry_bonos.tasa_fondeo_mercado",
                    tasa_anual=f"{tasa_anual:.4f}",
                )
                return tasa_anual
        except Exception:
            self._log.exception("carry_bonos.error_obteniendo_tasa_fondeo")

        self._log.info(
            "carry_bonos.usando_tasa_fondeo_default",
            tasa=TASA_FONDEO_DEFAULT,
        )
        return TASA_FONDEO_DEFAULT

    def _tiene_posicion(self, ticker: str) -> bool:
        """Verifica si hay una posicion abierta en el ticker dado.

        Args:
            ticker: Ticker del bono a verificar.

        Returns:
            True si hay posicion abierta.
        """
        try:
            posiciones = self.portfolio.get_posiciones()
            return ticker in posiciones
        except Exception:
            self._log.exception(
                "carry_bonos.error_verificando_posicion",
                ticker=ticker,
            )
            return False
