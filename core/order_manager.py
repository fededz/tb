"""Gestor de ordenes para el flujo de dos pasos de PPI.

Maneja el ciclo completo: budget -> aceptar disclaimers -> confirm,
con persistencia en DB para idempotencia y alertas via Telegram.
Usa los modelos reales del SDK ppi_client (OrderBudget, OrderConfirm,
Disclaimer, Order).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

import structlog

from ppi_client.models.disclaimer import Disclaimer
from ppi_client.models.order import Order
from ppi_client.models.order_budget import OrderBudget
from ppi_client.models.order_confirm import OrderConfirm

from config import PPI_ACCOUNT_NUMBER

if TYPE_CHECKING:
    from core.alertas import Alertas
    from core.ppi_wrapper import PPIWrapper
    from db.repository import Repository

logger = structlog.get_logger(__name__)


class OrderManager:
    """Gestiona el envio de ordenes al mercado via PPI.

    Implementa el flujo de dos pasos (budget + confirm) usando los modelos
    reales del SDK ppi_client y garantiza idempotencia revisando la DB
    antes de crear ordenes duplicadas.

    Attributes:
        _ppi: Wrapper sobre el SDK de PPI.
        _repository: Repositorio de acceso a la base de datos.
        _alertas: Cliente de alertas Telegram.
        _account_number: Numero de cuenta PPI.
    """

    def __init__(
        self,
        ppi: PPIWrapper,
        repository: Repository,
        alertas: Alertas,
    ) -> None:
        """Inicializa el OrderManager.

        Args:
            ppi: Instancia de PPIWrapper con conexion activa.
            repository: Repositorio de acceso a la base de datos.
            alertas: Instancia de Alertas para notificaciones.
        """
        self._ppi = ppi
        self._repository = repository
        self._alertas = alertas
        self._account_number: str = PPI_ACCOUNT_NUMBER

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run_async(self, coro: Any) -> None:
        """Ejecuta una coroutine de forma sincronica.

        Detecta si ya hay un event loop corriendo para elegir
        entre ensure_future y asyncio.run.

        Args:
            coro: Coroutine a ejecutar.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            asyncio.ensure_future(coro)
        else:
            asyncio.run(coro)

    @staticmethod
    def _resolve_operation_type(precio: float | None) -> str:
        """Determina el tipo de operacion segun si se especifica precio.

        Args:
            precio: Precio limite o None para mercado.

        Returns:
            'PRECIO_LIMITE' o 'PRECIO_MERCADO'.
        """
        return "PRECIO_MERCADO" if precio is None else "PRECIO_LIMITE"

    # ------------------------------------------------------------------
    # Envio de ordenes
    # ------------------------------------------------------------------

    def send_order(
        self,
        ticker: str,
        tipo: str,
        operacion: str,
        cantidad: float,
        precio: float | None,
        plazo: str,
        strategy: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Envia una orden al mercado siguiendo el flujo budget -> confirm.

        Verifica idempotencia: si ya existe una orden PENDING para el mismo
        ticker y estrategia, no crea una nueva.

        El flujo real usa los modelos del SDK ppi_client:
        1. Crea un OrderBudget y llama a ppi.orders.budget()
        2. Extrae disclaimers de la respuesta y los acepta automaticamente
        3. Crea un OrderConfirm con los disclaimers aceptados y confirma

        Args:
            ticker: Codigo del instrumento (ej: 'GGAL', 'DLR/JUN25').
            tipo: Tipo de instrumento PPI (ej: 'Acciones', 'Bonos', 'FuturosARS').
            operacion: 'COMPRA' o 'VENTA'.
            cantidad: Cantidad de papeles o nominales.
            precio: Precio limite. None para precio de mercado.
            plazo: Plazo de liquidacion (ej: 'A-48HS', 'INMEDIATA').
            strategy: Nombre de la estrategia que origina la orden.
            dry_run: Si True, solo registra en DB sin enviar a PPI.

        Returns:
            Diccionario con el resultado de la operacion incluyendo
            'status', 'order_id' y opcionalmente 'external_id'.
        """
        log = logger.bind(
            ticker=ticker,
            operacion=operacion,
            cantidad=cantidad,
            precio=precio,
            strategy=strategy,
            dry_run=dry_run,
        )

        # --- Chequeo de idempotencia ---
        existing = self._find_pending_order(ticker, strategy)
        if existing is not None:
            log.warning(
                "order_manager.orden_pendiente_existente",
                existing_id=existing["id"],
            )
            return {
                "status": "ALREADY_PENDING",
                "order_id": existing["id"],
                "message": (
                    f"Ya existe una orden PENDING (id={existing['id']}) "
                    f"para {ticker}/{strategy}"
                ),
            }

        # --- Crear registro en DB ---
        order = self._repository.insert_order(
            strategy=strategy,
            ticker=ticker,
            tipo=tipo,
            operacion=operacion,
            cantidad=cantidad,
            precio=precio,
            plazo=plazo,
            status="PENDING",
            dry_run=dry_run,
        )
        order_id: int = order["id"]
        log = log.bind(order_id=order_id)
        log.info("order_manager.orden_creada")

        # --- Dry run: solo registrar ---
        if dry_run:
            self._repository.update_order_status(order_id, "DRY_RUN")
            log.info("order_manager.dry_run_registrado")
            return {
                "status": "DRY_RUN",
                "order_id": order_id,
            }

        # --- Flujo real: budget -> disclaimers -> confirm ---
        try:
            result = self._execute_budget_confirm(
                order_id=order_id,
                ticker=ticker,
                tipo=tipo,
                operacion=operacion,
                cantidad=cantidad,
                precio=precio,
                plazo=plazo,
                log=log,
            )

            # Actualizar DB con resultado exitoso
            external_id = self._extract_external_id(result)
            self._repository.update_order_status(
                order_id,
                "EXECUTED",
                external_id=external_id,
                executed_at=datetime.now(timezone.utc),
            )

            log.info(
                "order_manager.orden_ejecutada",
                external_id=external_id,
            )

            # Alerta Telegram
            self._alertas.orden_ejecutada(
                strategy=strategy,
                ticker=ticker,
                operacion=operacion,
                cantidad=cantidad,
                precio=precio if precio is not None else 0.0,
                pnl_dia=0.0,
            )

            return {
                "status": "EXECUTED",
                "order_id": order_id,
                "external_id": external_id,
            }

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            self._repository.update_order_status(
                order_id,
                "REJECTED",
                error_msg=error_msg,
            )

            log.exception("order_manager.error_envio", error=error_msg)

            self._alertas.orden_rechazada(
                strategy=strategy,
                ticker=ticker,
                motivo=error_msg,
            )

            return {
                "status": "REJECTED",
                "order_id": order_id,
                "error": error_msg,
            }

    def _execute_budget_confirm(
        self,
        *,
        order_id: int,
        ticker: str,
        tipo: str,
        operacion: str,
        cantidad: float,
        precio: float | None,
        plazo: str,
        log: Any,
    ) -> Any:
        """Ejecuta el flujo de dos pasos budget -> confirm contra la API de PPI.

        Paso 1: Solicitar presupuesto con OrderBudget.
        Paso 2: Extraer disclaimers, aceptarlos automaticamente.
        Paso 3: Confirmar la orden con OrderConfirm.

        Args:
            order_id: ID interno de la orden en DB (usado como externalId).
            ticker: Codigo del instrumento.
            tipo: Tipo de instrumento PPI.
            operacion: 'COMPRA' o 'VENTA'.
            cantidad: Cantidad de papeles.
            precio: Precio limite o None para mercado.
            plazo: Plazo de liquidacion.
            log: Logger con contexto bindeado.

        Returns:
            Respuesta del confirm de PPI.

        Raises:
            Exception: Si el budget o confirm fallan.
        """
        operation_type = self._resolve_operation_type(precio)
        effective_price = precio if precio is not None else 0

        # Paso 1: Budget
        log.info("order_manager.budget_solicitando", operation_type=operation_type)
        budget_order = OrderBudget(
            accountNumber=self._account_number,
            quantity=cantidad,
            price=effective_price,
            ticker=ticker,
            instrumentType=tipo,
            quantityType="PAPELES",
            operationType=operation_type,
            operationTerm=plazo,
            operationMaxDate=None,
            operation=operacion,
            settlement=plazo,
            activationPrice=None,
        )
        budget_result = self._ppi.ppi_client.orders.budget(budget_order)
        log.info(
            "order_manager.budget_recibido",
            budget_keys=(
                list(budget_result.keys())
                if isinstance(budget_result, dict)
                else str(type(budget_result))
            ),
        )

        # Paso 2: Extraer y aceptar disclaimers
        disclaimers = self._extract_disclaimers(budget_result)
        if disclaimers:
            log.info(
                "order_manager.disclaimers_aceptados",
                cantidad_disclaimers=len(disclaimers),
            )

        # Paso 3: Confirm
        log.info("order_manager.confirm_enviando")
        confirm_order = OrderConfirm(
            accountNumber=self._account_number,
            quantity=cantidad,
            price=effective_price,
            ticker=ticker,
            instrumentType=tipo,
            quantityType="PAPELES",
            operationType=operation_type,
            operationTerm=plazo,
            operationMaxDate=None,
            operation=operacion,
            settlement=plazo,
            disclaimers=disclaimers,
            externalId=str(order_id),
            activationPrice=None,
        )
        confirm_result = self._ppi.ppi_client.orders.confirm(confirm_order)
        log.info(
            "order_manager.orden_confirmada",
            confirmed=str(confirm_result)[:500],
        )

        return confirm_result

    @staticmethod
    def _extract_disclaimers(budget_result: Any) -> list[Disclaimer]:
        """Extrae y acepta automaticamente los disclaimers del resultado de budget.

        Args:
            budget_result: Respuesta de ppi.orders.budget().

        Returns:
            Lista de Disclaimer con accepted=True, o lista vacia.
        """
        disclaimers: list[Disclaimer] = []

        if not isinstance(budget_result, dict):
            return disclaimers

        raw_disclaimers = budget_result.get("disclaimers")
        if not raw_disclaimers:
            return disclaimers

        for d in raw_disclaimers:
            code = d.get("code") if isinstance(d, dict) else getattr(d, "code", None)
            if code is not None:
                disclaimers.append(Disclaimer(code, True))

        return disclaimers

    # ------------------------------------------------------------------
    # Cancelacion
    # ------------------------------------------------------------------

    def cancel_order(self, order_id: int) -> dict[str, Any]:
        """Cancela una orden por su ID interno.

        Busca la orden en DB y, si tiene external_id, envia la
        cancelacion a PPI usando el modelo Order del SDK.
        Actualiza el estado a CANCELLED.

        Args:
            order_id: ID interno de la orden en la tabla orders.

        Returns:
            Diccionario con el resultado de la cancelacion.
        """
        log = logger.bind(order_id=order_id)

        order = self._repository.get_order_by_id(order_id)
        if order is None:
            log.warning("order_manager.cancel_orden_no_encontrada")
            return {"status": "NOT_FOUND", "order_id": order_id}

        if order["status"] not in ("PENDING", "EXECUTED"):
            log.warning(
                "order_manager.cancel_estado_invalido",
                current_status=order["status"],
            )
            return {
                "status": "INVALID_STATE",
                "order_id": order_id,
                "current_status": order["status"],
            }

        try:
            external_id = order.get("external_id")
            if external_id:
                cancel_order = Order(
                    account_number=self._account_number,
                    id=order_id,
                    externalId=external_id,
                )
                self._ppi.ppi_client.orders.cancel_order(cancel_order)
                log.info(
                    "order_manager.cancel_enviada_ppi",
                    external_id=external_id,
                )

            self._repository.update_order_status(order_id, "CANCELLED")

            log.info("order_manager.orden_cancelada")
            return {"status": "CANCELLED", "order_id": order_id}

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            log.exception("order_manager.cancel_error", error=error_msg)
            return {
                "status": "CANCEL_ERROR",
                "order_id": order_id,
                "error": error_msg,
            }

    def cancel_all(self) -> None:
        """Cancela todas las ordenes activas (PENDING).

        Itera sobre todas las ordenes activas y las cancela una por una.
        Los errores individuales se loguean pero no interrumpen el proceso.
        """
        active_orders = self.get_active_orders()
        logger.info(
            "order_manager.cancel_all_iniciando",
            cantidad=len(active_orders),
        )

        for order in active_orders:
            self.cancel_order(order["id"])

        logger.info("order_manager.cancel_all_completado")

    def get_active_orders(self) -> list[dict[str, Any]]:
        """Obtiene todas las ordenes activas del sistema.

        Consulta tanto la DB local como las ordenes activas en PPI
        y retorna las de la DB (fuente de verdad para el sistema).

        Returns:
            Lista de ordenes con status PENDING.
        """
        return self._repository.get_active_orders()

    def get_active_orders_ppi(self) -> Any:
        """Obtiene las ordenes activas directamente desde la API de PPI.

        Util para reconciliacion entre el estado local y el broker.

        Returns:
            Respuesta de ppi.orders.get_active_orders().
        """
        try:
            result = self._ppi.ppi_client.orders.get_active_orders(
                self._account_number
            )
            logger.info(
                "order_manager.ordenes_activas_ppi",
                cantidad=len(result) if isinstance(result, list) else "N/A",
            )
            return result
        except Exception as exc:
            logger.exception(
                "order_manager.error_obteniendo_ordenes_ppi",
                error=str(exc),
            )
            return []

    # ------------------------------------------------------------------
    # Metodos internos
    # ------------------------------------------------------------------

    def _find_pending_order(
        self, ticker: str, strategy: str
    ) -> dict[str, Any] | None:
        """Busca una orden PENDING existente para el mismo ticker/estrategia.

        Se usa para garantizar idempotencia: evita crear ordenes duplicadas
        si el sistema se reinicia entre budget y confirm.

        Args:
            ticker: Codigo del instrumento.
            strategy: Nombre de la estrategia.

        Returns:
            La orden PENDING existente como dict, o None si no hay.
        """
        active = self._repository.get_active_orders()
        for order in active:
            if order["ticker"] == ticker and order["strategy"] == strategy:
                return order
        return None

    @staticmethod
    def _extract_external_id(confirmed: Any) -> str | None:
        """Extrae el ID externo de la respuesta de confirmacion de PPI.

        Args:
            confirmed: Respuesta del metodo ppi.orders.confirm().

        Returns:
            El external_id como string, o None si no se puede extraer.
        """
        if isinstance(confirmed, dict):
            for key in ("id", "orderId", "order_id", "externalId"):
                if key in confirmed:
                    return str(confirmed[key])
        if hasattr(confirmed, "id"):
            return str(confirmed.id)
        if hasattr(confirmed, "orderId"):
            return str(confirmed.orderId)
        return None
