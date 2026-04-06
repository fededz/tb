"""Estado en tiempo real del portafolio.

Se actualiza con cada notificacion de orden ejecutada via WebSocket.
Mantiene un cache en memoria de las posiciones abiertas y calcula P&L.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Optional

import structlog

if TYPE_CHECKING:
    from core.ppi_wrapper import PPIWrapper
    from db.repository import Repository

logger = structlog.get_logger(__name__)


@dataclass
class Posicion:
    """Representa una posicion abierta en el portafolio."""

    ticker: str
    tipo: str
    cantidad: float
    precio_entrada: float
    strategy: str
    opened_at: datetime
    pnl_latente: Optional[float] = None


class Portfolio:
    """Estado en tiempo real del portafolio.

    Sincronizado con la base de datos al iniciar y actualizado en memoria
    con cada notificacion de orden ejecutada.
    """

    def __init__(self, repository: Repository, ppi: PPIWrapper) -> None:
        """Inicializa el portafolio.

        Args:
            repository: Acceso a la base de datos para leer/escribir posiciones.
            ppi: Wrapper de PPI para consultar precios y balance.
        """
        self._repository = repository
        self._ppi = ppi
        self._posiciones: dict[str, Posicion] = {}

    def _make_key(self, ticker: str, tipo: str) -> str:
        """Genera la clave unica para una posicion.

        Args:
            ticker: Simbolo del instrumento.
            tipo: Tipo de instrumento (accion, bono, futuro, etc.).

        Returns:
            Clave en formato 'ticker:tipo'.
        """
        return f"{ticker}:{tipo}"

    def load_from_db(self) -> None:
        """Carga las posiciones abiertas desde la base de datos al iniciar.

        Sincroniza el estado en memoria con la DB para recuperar posiciones
        tras un reinicio del sistema.
        """
        try:
            positions = self._repository.get_posiciones_abiertas()
            self._posiciones.clear()
            for pos in positions:
                key = self._make_key(pos["ticker"], pos["tipo"])
                self._posiciones[key] = Posicion(
                    ticker=pos["ticker"],
                    tipo=pos["tipo"],
                    cantidad=float(pos["cantidad"]),
                    precio_entrada=float(pos["precio_entrada"]),
                    strategy=pos["strategy"],
                    opened_at=pos["opened_at"],
                    pnl_latente=None,
                )
            logger.info(
                "posiciones_cargadas_desde_db",
                cantidad=len(self._posiciones),
            )
        except Exception:
            logger.exception("error_cargando_posiciones_desde_db")
            raise

    def get_posiciones(self) -> dict[str, Posicion]:
        """Retorna todas las posiciones abiertas.

        Returns:
            Diccionario con clave 'ticker:tipo' y valor Posicion.
        """
        return dict(self._posiciones)

    def get_pnl_diario(self) -> float:
        """Obtiene el P&L realizado del dia desde la base de datos.

        Returns:
            P&L realizado en ARS para la fecha de hoy. 0.0 si no hay datos.
        """
        try:
            pnl = self._repository.get_pnl_diario(date.today())
            if pnl is not None:
                return float(pnl.get("pnl_ars") or 0.0)
            return 0.0
        except Exception:
            logger.exception("error_obteniendo_pnl_diario")
            return 0.0

    def get_pnl_total(self) -> float:
        """Calcula el P&L total: realizado del dia + no realizado de posiciones abiertas.

        Actualiza el pnl_latente de cada posicion consultando precios actuales.

        Returns:
            P&L total en ARS (realizado + no realizado).
        """
        pnl_realizado = self.get_pnl_diario()
        pnl_no_realizado = 0.0

        for key, pos in self._posiciones.items():
            try:
                precio_actual = self._ppi.get_current_price(
                    pos.ticker, pos.tipo, "INMEDIATA"
                )
                latente = (precio_actual - pos.precio_entrada) * pos.cantidad
                pos.pnl_latente = latente
                pnl_no_realizado += latente
            except Exception:
                logger.warning(
                    "error_obteniendo_precio_actual",
                    ticker=pos.ticker,
                    tipo=pos.tipo,
                )

        return pnl_realizado + pnl_no_realizado

    def update_from_execution(self, order_data: dict) -> None:
        """Actualiza el portafolio cuando se ejecuta una orden.

        Si la operacion es COMPRA, incrementa la posicion existente o crea una nueva.
        Si la operacion es VENTA, reduce la posicion existente y la cierra si llega a cero.

        Args:
            order_data: Diccionario con datos de la orden ejecutada. Campos esperados:
                - ticker (str)
                - tipo (str)
                - operacion (str): 'COMPRA' o 'VENTA'
                - cantidad (float)
                - precio (float)
                - strategy (str)
        """
        ticker = order_data["ticker"]
        tipo = order_data["tipo"]
        operacion = order_data["operacion"]
        cantidad = float(order_data["cantidad"])
        precio = float(order_data["precio"])
        strategy = order_data["strategy"]
        key = self._make_key(ticker, tipo)

        if operacion == "COMPRA":
            self._handle_compra(key, ticker, tipo, cantidad, precio, strategy)
        elif operacion == "VENTA":
            self._handle_venta(key, cantidad, precio)
        else:
            logger.error("operacion_desconocida", operacion=operacion)
            return

        logger.info(
            "portafolio_actualizado",
            ticker=ticker,
            tipo=tipo,
            operacion=operacion,
            cantidad=cantidad,
            precio=precio,
            posiciones_abiertas=len(self._posiciones),
        )

    def _handle_compra(
        self,
        key: str,
        ticker: str,
        tipo: str,
        cantidad: float,
        precio: float,
        strategy: str,
    ) -> None:
        """Procesa una operacion de compra.

        Si ya existe una posicion, calcula el precio promedio ponderado.
        Si no existe, crea una nueva posicion.

        Args:
            key: Clave unica de la posicion.
            ticker: Simbolo del instrumento.
            tipo: Tipo de instrumento.
            cantidad: Cantidad comprada.
            precio: Precio de ejecucion.
            strategy: Nombre de la estrategia.
        """
        if key in self._posiciones:
            pos = self._posiciones[key]
            cantidad_total = pos.cantidad + cantidad
            # Precio promedio ponderado
            pos.precio_entrada = (
                (pos.precio_entrada * pos.cantidad) + (precio * cantidad)
            ) / cantidad_total
            pos.cantidad = cantidad_total
        else:
            self._posiciones[key] = Posicion(
                ticker=ticker,
                tipo=tipo,
                cantidad=cantidad,
                precio_entrada=precio,
                strategy=strategy,
                opened_at=datetime.now(),
                pnl_latente=None,
            )

    def _handle_venta(self, key: str, cantidad: float, precio: float) -> None:
        """Procesa una operacion de venta.

        Reduce la cantidad de la posicion. Si llega a cero o menos, la cierra.

        Args:
            key: Clave unica de la posicion.
            cantidad: Cantidad vendida.
            precio: Precio de ejecucion.
        """
        if key not in self._posiciones:
            logger.warning(
                "venta_sin_posicion_abierta",
                key=key,
                cantidad=cantidad,
            )
            return

        pos = self._posiciones[key]
        pos.cantidad -= cantidad

        if pos.cantidad <= 0:
            pnl = (precio - pos.precio_entrada) * cantidad
            logger.info(
                "posicion_cerrada",
                key=key,
                pnl=pnl,
            )
            del self._posiciones[key]

    def get_capital_total(self) -> float:
        """Obtiene el capital total desde la API de PPI.

        Returns:
            Capital total de la cuenta en ARS.
        """
        try:
            balance = self._ppi.get_balance()
            capital = float(balance.get("total", 0.0))
            logger.debug("capital_total_obtenido", capital=capital)
            return capital
        except Exception:
            logger.exception("error_obteniendo_capital_total")
            return 0.0

    def get_capital_disponible(self) -> float:
        """Calcula el capital disponible para nuevas operaciones.

        Capital disponible = capital total - capital comprometido en posiciones abiertas.

        Returns:
            Capital disponible en ARS.
        """
        capital_total = self.get_capital_total()
        capital_en_posiciones = sum(
            pos.cantidad * pos.precio_entrada
            for pos in self._posiciones.values()
        )
        disponible = capital_total - capital_en_posiciones
        logger.debug(
            "capital_disponible_calculado",
            capital_total=capital_total,
            capital_en_posiciones=capital_en_posiciones,
            disponible=disponible,
        )
        return max(disponible, 0.0)

    def get_posiciones_count(self) -> int:
        """Retorna la cantidad de posiciones abiertas.

        Returns:
            Numero de posiciones abiertas actualmente.
        """
        return len(self._posiciones)
