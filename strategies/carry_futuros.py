"""Estrategia de carry trade en futuros de dolar.

Compara el precio spot del dolar MEP (AL30 ARS / AL30D USD) contra
el precio de futuros de dolar en ROFEX. Si la tasa implicita anualizada
supera un umbral, abre posicion long en el futuro para capturar la base.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import structlog

from strategies.base import Signal, Strategy

logger = structlog.get_logger(__name__)

# Parametros configurables
UMBRAL_ENTRADA_TASA: float = 0.40  # 40% anual
UMBRAL_SALIDA_TASA: float = 0.25   # 25% anual
MAX_DIAS_VENCIMIENTO: int = 90
CONTRATOS_BASE: float = 10.0


class CarryFuturos(Strategy):
    """Carry trade entre dolar MEP spot y futuros de dolar ROFEX.

    Calcula la tasa implicita anualizada entre el spot y el futuro.
    Si la tasa supera el umbral de entrada, compra futuros.
    Si cae debajo del umbral de salida, cierra la posicion.

    La tasa implicita se calcula como:
        (precio_futuro / precio_spot - 1) * (365 / dias_al_vencimiento)
    """

    name: str = "carry_futuros"
    frecuencia: str = "diaria"
    instrumentos: list[str] = ["AL30", "AL30D", "DLR"]

    def generate_signals(self) -> list[Signal]:
        """Genera senales de carry trade en futuros de dolar.

        Returns:
            Lista con una senal de COMPRA o VENTA, o lista vacia.
        """
        # Obtener precio spot del dolar MEP
        precio_al30_ars = self.ppi.get_current_price("AL30", "Bonos", "A-48HS")
        precio_al30d_usd = self.ppi.get_current_price("AL30D", "Bonos", "INMEDIATA")

        if not precio_al30_ars or not precio_al30d_usd or precio_al30d_usd == 0:
            self._log.warning(
                "carry_futuros.precios_spot_no_disponibles",
                al30_ars=precio_al30_ars,
                al30d_usd=precio_al30d_usd,
            )
            return []

        precio_spot_mep = precio_al30_ars / precio_al30d_usd
        self._log.info("carry_futuros.dolar_mep", precio=precio_spot_mep)

        # Buscar el futuro DLR mas proximo
        futuro = self._get_proximo_futuro()
        if futuro is None:
            self._log.warning("carry_futuros.no_hay_futuro_proximo")
            return []

        ticker_futuro = futuro["ticker"]
        dias_vencimiento = futuro["dias"]
        precio_futuro = futuro["precio"]

        if dias_vencimiento <= 0 or precio_futuro <= 0:
            self._log.warning(
                "carry_futuros.datos_futuro_invalidos",
                ticker=ticker_futuro,
                dias=dias_vencimiento,
                precio=precio_futuro,
            )
            return []

        # Calcular tasa implicita anualizada
        tasa_implicita = (precio_futuro / precio_spot_mep - 1) * (365 / dias_vencimiento)

        self._log.info(
            "carry_futuros.tasa_calculada",
            tasa_implicita=f"{tasa_implicita:.4f}",
            precio_spot=precio_spot_mep,
            precio_futuro=precio_futuro,
            ticker_futuro=ticker_futuro,
            dias_vencimiento=dias_vencimiento,
        )

        # Verificar si tenemos posicion abierta en este futuro
        tiene_posicion = self._tiene_posicion(ticker_futuro)

        signals: list[Signal] = []

        if tasa_implicita > UMBRAL_ENTRADA_TASA and not tiene_posicion:
            signals.append(
                Signal(
                    strategy=self.name,
                    ticker=ticker_futuro,
                    tipo="Futuros",
                    operacion="COMPRA",
                    cantidad=CONTRATOS_BASE,
                    precio=None,
                    plazo="INMEDIATA",
                    motivo=(
                        f"Tasa implicita {tasa_implicita:.2%} > umbral "
                        f"{UMBRAL_ENTRADA_TASA:.2%}. Spot MEP: {precio_spot_mep:.2f}, "
                        f"Futuro: {precio_futuro:.2f}, Dias: {dias_vencimiento}"
                    ),
                )
            )
        elif tasa_implicita < UMBRAL_SALIDA_TASA and tiene_posicion:
            signals.append(
                Signal(
                    strategy=self.name,
                    ticker=ticker_futuro,
                    tipo="Futuros",
                    operacion="VENTA",
                    cantidad=CONTRATOS_BASE,
                    precio=None,
                    plazo="INMEDIATA",
                    motivo=(
                        f"Tasa implicita {tasa_implicita:.2%} < umbral salida "
                        f"{UMBRAL_SALIDA_TASA:.2%}. Cerrando posicion."
                    ),
                )
            )

        return signals

    def _get_proximo_futuro(self) -> dict[str, Any] | None:
        """Busca el futuro de dolar ROFEX mas proximo dentro del maximo de dias.

        Intenta tickers con formato DLR/MMYY para los proximos meses.

        Returns:
            Dict con ticker, precio y dias al vencimiento, o None.
        """
        hoy = date.today()

        for meses_adelante in range(1, 7):
            # Calcular fecha de vencimiento aproximada (ultimo dia habil del mes)
            anio = hoy.year + (hoy.month + meses_adelante - 1) // 12
            mes = (hoy.month + meses_adelante - 1) % 12 + 1
            # Vencimiento tipico: ultimo viernes del mes
            ultimo_dia = date(anio, mes, 1) + timedelta(days=32)
            ultimo_dia = ultimo_dia.replace(day=1) - timedelta(days=1)

            dias_al_vencimiento = (ultimo_dia - hoy).days
            if dias_al_vencimiento > MAX_DIAS_VENCIMIENTO:
                continue
            if dias_al_vencimiento <= 0:
                continue

            # Formato ROFEX: DLR/MMYY (ej: DLR/JUN25)
            meses_nombre = [
                "ENE", "FEB", "MAR", "ABR", "MAY", "JUN",
                "JUL", "AGO", "SEP", "OCT", "NOV", "DIC",
            ]
            ticker = f"DLR/{meses_nombre[mes - 1]}{str(anio)[-2:]}"

            precio = self.ppi.get_current_price(ticker, "Futuros", "INMEDIATA")
            if precio and precio > 0:
                return {
                    "ticker": ticker,
                    "precio": precio,
                    "dias": dias_al_vencimiento,
                }

        return None

    def _tiene_posicion(self, ticker_futuro: str) -> bool:
        """Verifica si hay una posicion abierta en el futuro dado.

        Args:
            ticker_futuro: Ticker del futuro a verificar.

        Returns:
            True si hay posicion abierta.
        """
        try:
            posiciones = self.portfolio.get_posiciones()
            return ticker_futuro in posiciones
        except Exception:
            self._log.exception(
                "carry_futuros.error_verificando_posicion",
                ticker=ticker_futuro,
            )
            return False
