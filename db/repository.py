"""Repositorio de acceso a datos con psycopg2 y connection pooling.

Provee metodos CRUD para todas las tablas del sistema de trading.
Usa queries parametrizadas para seguridad y retorna diccionarios.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool
import structlog

from config import get_db_url

logger = structlog.get_logger(__name__)


def _parse_dsn(url: str) -> dict[str, Any]:
    """Convierte una URL postgresql:// en kwargs para psycopg2."""
    # postgresql://user:password@host:port/dbname
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "dbname": parsed.path.lstrip("/") if parsed.path else "trading",
        "user": parsed.username or "trading",
        "password": parsed.password or "",
    }


def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Convierte valores Decimal a float para facilitar el consumo downstream."""
    result: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            result[key] = float(value)
        else:
            result[key] = value
    return result


class Repository:
    """Repositorio principal de acceso a datos.

    Usa psycopg2 con SimpleConnectionPool para reutilizar conexiones.
    Todos los metodos publicos retornan dicts o listas de dicts.
    """

    def __init__(self, min_conn: int = 1, max_conn: int = 5) -> None:
        dsn_params = _parse_dsn(get_db_url())
        try:
            self._pool = psycopg2.pool.SimpleConnectionPool(
                minconn=min_conn,
                maxconn=max_conn,
                **dsn_params,
            )
            logger.info("pool_de_conexiones_creado", min=min_conn, max=max_conn)
        except psycopg2.Error as exc:
            logger.error("error_creando_pool", error=str(exc))
            raise

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    def _get_conn(self) -> Any:
        """Obtiene una conexion del pool."""
        return self._pool.getconn()

    def _put_conn(self, conn: Any) -> None:
        """Devuelve una conexion al pool."""
        self._pool.putconn(conn)

    def _execute(
        self,
        query: str,
        params: tuple | dict | None = None,
        *,
        fetch_one: bool = False,
        fetch_all: bool = False,
        returning: bool = False,
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Ejecuta una query con manejo automatico de conexion y errores.

        Args:
            query: SQL parametrizado.
            params: Parametros para la query.
            fetch_one: Si True, retorna una sola fila.
            fetch_all: Si True, retorna todas las filas.
            returning: Si True, hace fetchone despues de INSERT/UPDATE con RETURNING.

        Returns:
            dict, lista de dicts, o None segun los flags.
        """
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, params)
                if fetch_one or returning:
                    row = cur.fetchone()
                    conn.commit()
                    return _row_to_dict(dict(row)) if row else None
                if fetch_all:
                    rows = cur.fetchall()
                    conn.commit()
                    return [_row_to_dict(dict(r)) for r in rows]
                conn.commit()
                return None
        except psycopg2.Error as exc:
            conn.rollback()
            logger.error("error_ejecutando_query", query=query[:120], error=str(exc))
            raise
        finally:
            self._put_conn(conn)

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def insert_order(
        self,
        *,
        strategy: str,
        ticker: str,
        tipo: str,
        operacion: str,
        cantidad: float,
        precio: float | None,
        plazo: str,
        status: str = "PENDING",
        dry_run: bool = False,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        """Inserta una nueva orden y retorna el registro creado."""
        query = """
            INSERT INTO orders
                (external_id, strategy, ticker, tipo, operacion,
                 cantidad, precio, plazo, status, dry_run)
            VALUES
                (%(external_id)s, %(strategy)s, %(ticker)s, %(tipo)s, %(operacion)s,
                 %(cantidad)s, %(precio)s, %(plazo)s, %(status)s, %(dry_run)s)
            RETURNING *
        """
        params = {
            "external_id": external_id,
            "strategy": strategy,
            "ticker": ticker,
            "tipo": tipo,
            "operacion": operacion,
            "cantidad": cantidad,
            "precio": precio,
            "plazo": plazo,
            "status": status,
            "dry_run": dry_run,
        }
        result = self._execute(query, params, returning=True)
        logger.info("orden_insertada", id=result["id"], ticker=ticker, status=status)  # type: ignore[index]
        return result  # type: ignore[return-value]

    def update_order_status(
        self,
        order_id: int,
        status: str,
        *,
        external_id: str | None = None,
        executed_at: datetime | None = None,
        error_msg: str | None = None,
    ) -> dict[str, Any] | None:
        """Actualiza el estado de una orden existente."""
        query = """
            UPDATE orders
            SET status = %(status)s,
                external_id = COALESCE(%(external_id)s, external_id),
                executed_at = COALESCE(%(executed_at)s, executed_at),
                error_msg = COALESCE(%(error_msg)s, error_msg)
            WHERE id = %(order_id)s
            RETURNING *
        """
        params = {
            "status": status,
            "external_id": external_id,
            "executed_at": executed_at,
            "error_msg": error_msg,
            "order_id": order_id,
        }
        result = self._execute(query, params, returning=True)
        logger.info("orden_actualizada", id=order_id, status=status)
        return result

    def get_active_orders(self) -> list[dict[str, Any]]:
        """Retorna todas las ordenes con status PENDING."""
        query = "SELECT * FROM orders WHERE status = 'PENDING' ORDER BY created_at DESC"
        return self._execute(query, fetch_all=True) or []  # type: ignore[return-value]

    def get_order_by_id(self, order_id: int) -> dict[str, Any] | None:
        """Retorna una orden por su ID."""
        query = "SELECT * FROM orders WHERE id = %(order_id)s"
        return self._execute(query, {"order_id": order_id}, fetch_one=True)

    def get_ordenes_filtradas(
        self,
        *,
        strategy: str | None = None,
        ticker: str | None = None,
        desde: date | None = None,
        hasta: date | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Retorna ordenes filtradas por los criterios provistos.

        Todos los filtros son opcionales. Si no se provee ninguno,
        retorna todas las ordenes ordenadas por fecha descendente.
        """
        conditions: list[str] = []
        params: dict[str, Any] = {}

        if strategy is not None:
            conditions.append("strategy = %(strategy)s")
            params["strategy"] = strategy
        if ticker is not None:
            conditions.append("ticker = %(ticker)s")
            params["ticker"] = ticker
        if desde is not None:
            conditions.append("created_at >= %(desde)s")
            params["desde"] = desde
        if hasta is not None:
            conditions.append("created_at <= %(hasta)s")
            params["hasta"] = hasta
        if status is not None:
            conditions.append("status = %(status)s")
            params["status"] = status

        where_clause = " AND ".join(conditions) if conditions else "TRUE"
        query = f"SELECT * FROM orders WHERE {where_clause} ORDER BY created_at DESC"
        return self._execute(query, params, fetch_all=True) or []  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def insert_position(
        self,
        *,
        ticker: str,
        tipo: str,
        cantidad: float,
        precio_entrada: float,
        strategy: str,
    ) -> dict[str, Any]:
        """Inserta una nueva posicion abierta."""
        query = """
            INSERT INTO positions (ticker, tipo, cantidad, precio_entrada, strategy)
            VALUES (%(ticker)s, %(tipo)s, %(cantidad)s, %(precio_entrada)s, %(strategy)s)
            RETURNING *
        """
        params = {
            "ticker": ticker,
            "tipo": tipo,
            "cantidad": cantidad,
            "precio_entrada": precio_entrada,
            "strategy": strategy,
        }
        result = self._execute(query, params, returning=True)
        logger.info("posicion_abierta", id=result["id"], ticker=ticker)  # type: ignore[index]
        return result  # type: ignore[return-value]

    def close_position(
        self, position_id: int, pnl: float
    ) -> dict[str, Any] | None:
        """Cierra una posicion registrando P&L y timestamp."""
        query = """
            UPDATE positions
            SET closed_at = NOW(), pnl = %(pnl)s
            WHERE id = %(position_id)s AND closed_at IS NULL
            RETURNING *
        """
        params = {"pnl": pnl, "position_id": position_id}
        result = self._execute(query, params, returning=True)
        if result:
            logger.info("posicion_cerrada", id=position_id, pnl=pnl)
        else:
            logger.warning("posicion_no_encontrada_o_ya_cerrada", id=position_id)
        return result

    def get_posiciones_abiertas(self) -> list[dict[str, Any]]:
        """Retorna todas las posiciones que aun no fueron cerradas."""
        query = """
            SELECT * FROM positions
            WHERE closed_at IS NULL
            ORDER BY opened_at DESC
        """
        return self._execute(query, fetch_all=True) or []  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # PnL Diario
    # ------------------------------------------------------------------

    def insert_pnl_diario(
        self,
        *,
        fecha: date,
        pnl_ars: float | None = None,
        pnl_usd: float | None = None,
        capital_inicio: float | None = None,
        capital_fin: float | None = None,
        trades: int | None = None,
    ) -> dict[str, Any]:
        """Inserta o actualiza el P&L de un dia (upsert en fecha)."""
        query = """
            INSERT INTO pnl_diario (fecha, pnl_ars, pnl_usd, capital_inicio, capital_fin, trades)
            VALUES (%(fecha)s, %(pnl_ars)s, %(pnl_usd)s, %(capital_inicio)s, %(capital_fin)s, %(trades)s)
            ON CONFLICT (fecha) DO UPDATE SET
                pnl_ars = EXCLUDED.pnl_ars,
                pnl_usd = EXCLUDED.pnl_usd,
                capital_inicio = EXCLUDED.capital_inicio,
                capital_fin = EXCLUDED.capital_fin,
                trades = EXCLUDED.trades
            RETURNING *
        """
        params = {
            "fecha": fecha,
            "pnl_ars": pnl_ars,
            "pnl_usd": pnl_usd,
            "capital_inicio": capital_inicio,
            "capital_fin": capital_fin,
            "trades": trades,
        }
        result = self._execute(query, params, returning=True)
        logger.info("pnl_diario_registrado", fecha=str(fecha))
        return result  # type: ignore[return-value]

    def get_pnl_diario(self, fecha: date) -> dict[str, Any] | None:
        """Retorna el P&L de un dia especifico."""
        query = "SELECT * FROM pnl_diario WHERE fecha = %(fecha)s"
        return self._execute(query, {"fecha": fecha}, fetch_one=True)

    def get_pnl_range(
        self, desde: date, hasta: date
    ) -> list[dict[str, Any]]:
        """Retorna el P&L diario en un rango de fechas (inclusivo)."""
        query = """
            SELECT * FROM pnl_diario
            WHERE fecha >= %(desde)s AND fecha <= %(hasta)s
            ORDER BY fecha ASC
        """
        return self._execute(query, {"desde": desde, "hasta": hasta}, fetch_all=True) or []  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Market Data Cache
    # ------------------------------------------------------------------

    def cache_market_data(
        self,
        *,
        ticker: str,
        tipo: str,
        plazo: str,
        fecha: date,
        open: float | None = None,
        high: float | None = None,
        low: float | None = None,
        close: float | None = None,
        volume: float | None = None,
    ) -> dict[str, Any]:
        """Inserta o actualiza un registro de market data en cache (upsert)."""
        query = """
            INSERT INTO market_data_cache
                (ticker, tipo, plazo, fecha, open, high, low, close, volume)
            VALUES
                (%(ticker)s, %(tipo)s, %(plazo)s, %(fecha)s,
                 %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s)
            ON CONFLICT (ticker, tipo, plazo, fecha) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume
            RETURNING *
        """
        params = {
            "ticker": ticker,
            "tipo": tipo,
            "plazo": plazo,
            "fecha": fecha,
            "open": open,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
        return self._execute(query, params, returning=True)  # type: ignore[return-value]

    def get_cached_market_data(
        self,
        ticker: str,
        tipo: str,
        plazo: str,
        desde: date,
        hasta: date,
    ) -> list[dict[str, Any]]:
        """Retorna datos historicos cacheados para un instrumento en un rango."""
        query = """
            SELECT * FROM market_data_cache
            WHERE ticker = %(ticker)s
              AND tipo = %(tipo)s
              AND plazo = %(plazo)s
              AND fecha >= %(desde)s
              AND fecha <= %(hasta)s
            ORDER BY fecha ASC
        """
        params = {
            "ticker": ticker,
            "tipo": tipo,
            "plazo": plazo,
            "desde": desde,
            "hasta": hasta,
        }
        return self._execute(query, params, fetch_all=True) or []  # type: ignore[return-value]

    def delete_market_data_cache(
        self,
        ticker: str,
        tipo: str,
        plazo: str,
        fecha: date,
    ) -> None:
        """Elimina una entrada especifica del cache de market data."""
        query = """
            DELETE FROM market_data_cache
            WHERE ticker = %(ticker)s
              AND tipo = %(tipo)s
              AND plazo = %(plazo)s
              AND fecha = %(fecha)s
        """
        params = {
            "ticker": ticker,
            "tipo": tipo,
            "plazo": plazo,
            "fecha": fecha,
        }
        self._execute(query, params)
        logger.info(
            "market_data_cache_eliminado",
            ticker=ticker,
            fecha=str(fecha),
        )

    # ------------------------------------------------------------------
    # Risk Profile
    # ------------------------------------------------------------------

    def get_active_risk_profile(self) -> dict[str, Any] | None:
        """Retorna el perfil de riesgo actualmente activo."""
        query = "SELECT * FROM risk_profile WHERE activo = TRUE"
        return self._execute(query, fetch_one=True)

    def get_all_risk_profiles(self) -> list[dict[str, Any]]:
        """Retorna todos los perfiles de riesgo almacenados."""
        query = "SELECT * FROM risk_profile ORDER BY nombre"
        return self._execute(query, fetch_all=True) or []  # type: ignore[return-value]

    def set_active_risk_profile(
        self, nombre: str, *, updated_by: str = "sistema"
    ) -> dict[str, Any] | None:
        """Activa un perfil de riesgo por nombre, desactivando el anterior.

        Usa una transaccion para garantizar que solo un perfil quede activo.

        Args:
            nombre: Nombre del perfil a activar.
            updated_by: Quien realizo el cambio ('dashboard' o 'sistema').

        Returns:
            El perfil activado o None si el nombre no existe.
        """
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Desactivar el perfil activo actual
                cur.execute("UPDATE risk_profile SET activo = FALSE WHERE activo = TRUE")
                # Activar el nuevo perfil
                cur.execute(
                    """
                    UPDATE risk_profile
                    SET activo = TRUE, updated_at = NOW(), updated_by = %(updated_by)s
                    WHERE nombre = %(nombre)s
                    RETURNING *
                    """,
                    {"nombre": nombre, "updated_by": updated_by},
                )
                row = cur.fetchone()
                conn.commit()
                if row:
                    result = _row_to_dict(dict(row))
                    logger.info("perfil_riesgo_activado", nombre=nombre, by=updated_by)
                    return result
                logger.warning("perfil_riesgo_no_encontrado", nombre=nombre)
                return None
        except psycopg2.Error as exc:
            conn.rollback()
            logger.error("error_cambiando_perfil", nombre=nombre, error=str(exc))
            raise
        finally:
            self._put_conn(conn)

    # ------------------------------------------------------------------
    # Market Context
    # ------------------------------------------------------------------

    def insert_market_context(
        self,
        *,
        timestamp: datetime,
        riesgo_macro: str,
        sentimiento: float | None = None,
        sizing_mult: float = 1.0,
        eventos: dict | list | None = None,
        estrategias_pausadas: list[str] | None = None,
        resumen: str | None = None,
        fuentes_count: int | None = None,
    ) -> dict[str, Any]:
        """Inserta un nuevo registro de contexto de mercado."""
        query = """
            INSERT INTO market_context
                (timestamp, riesgo_macro, sentimiento, sizing_mult,
                 eventos, estrategias_pausadas, resumen, fuentes_count)
            VALUES
                (%(timestamp)s, %(riesgo_macro)s, %(sentimiento)s, %(sizing_mult)s,
                 %(eventos)s, %(estrategias_pausadas)s, %(resumen)s, %(fuentes_count)s)
            RETURNING *
        """
        params = {
            "timestamp": timestamp,
            "riesgo_macro": riesgo_macro,
            "sentimiento": sentimiento,
            "sizing_mult": sizing_mult,
            "eventos": json.dumps(eventos) if eventos is not None else None,
            "estrategias_pausadas": (
                json.dumps(estrategias_pausadas)
                if estrategias_pausadas is not None
                else None
            ),
            "resumen": resumen,
            "fuentes_count": fuentes_count,
        }
        result = self._execute(query, params, returning=True)
        logger.info(
            "contexto_mercado_insertado",
            riesgo=riesgo_macro,
            sizing=sizing_mult,
        )
        return result  # type: ignore[return-value]

    def get_latest_market_context(self) -> dict[str, Any] | None:
        """Retorna el contexto de mercado mas reciente."""
        query = """
            SELECT * FROM market_context
            ORDER BY timestamp DESC
            LIMIT 1
        """
        return self._execute(query, fetch_one=True)

    # ------------------------------------------------------------------
    # Source Predictions (Research Feedback)
    # ------------------------------------------------------------------

    def insert_source_prediction(self, data: dict[str, Any]) -> int:
        """Inserta una prediccion de impacto de una fuente del research agent.

        Args:
            data: Dict con los campos de source_predictions:
                - timestamp_evento (datetime)
                - fuente (str)
                - username (str)
                - contenido_resumen (str, opcional)
                - activos_afectados (list, opcional)
                - impacto_predicho (float)
                - ventana_min (int)

        Returns:
            ID de la prediccion insertada.
        """
        query = """
            INSERT INTO source_predictions
                (timestamp_evento, fuente, username, contenido_resumen,
                 activos_afectados, impacto_predicho, ventana_min)
            VALUES
                (%(timestamp_evento)s, %(fuente)s, %(username)s,
                 %(contenido_resumen)s, %(activos_afectados)s,
                 %(impacto_predicho)s, %(ventana_min)s)
            RETURNING id
        """
        params = {
            "timestamp_evento": data["timestamp_evento"],
            "fuente": data["fuente"],
            "username": data.get("username"),
            "contenido_resumen": data.get("contenido_resumen"),
            "activos_afectados": (
                json.dumps(data["activos_afectados"])
                if data.get("activos_afectados") is not None
                else None
            ),
            "impacto_predicho": data["impacto_predicho"],
            "ventana_min": data["ventana_min"],
        }
        result = self._execute(query, params, returning=True)
        pred_id: int = result["id"]  # type: ignore[index]
        logger.info(
            "prediccion_insertada",
            id=pred_id,
            fuente=data["fuente"],
            username=data.get("username"),
        )
        return pred_id

    def get_pending_predictions(self) -> list[dict[str, Any]]:
        """Retorna predicciones pendientes de medir cuya ventana ya expiro.

        Busca registros con medida=False y medicion_schedulada <= now().

        Returns:
            Lista de dicts con las predicciones pendientes.
        """
        query = """
            SELECT * FROM source_predictions
            WHERE medida = FALSE
              AND medicion_schedulada IS NOT NULL
              AND medicion_schedulada <= NOW()
            ORDER BY medicion_schedulada ASC
        """
        return self._execute(query, fetch_all=True) or []  # type: ignore[return-value]

    def get_prediction_by_id(
        self, prediction_id: int
    ) -> dict[str, Any] | None:
        """Retorna una prediccion por su ID.

        Args:
            prediction_id: ID de la prediccion.

        Returns:
            Dict con la prediccion, o None si no existe.
        """
        query = "SELECT * FROM source_predictions WHERE id = %(id)s"
        return self._execute(query, {"id": prediction_id}, fetch_one=True)

    def mark_prediction_measured(self, prediction_id: int) -> None:
        """Marca una prediccion como medida (medida=True).

        Args:
            prediction_id: ID de la prediccion a marcar.
        """
        query = """
            UPDATE source_predictions
            SET medida = TRUE
            WHERE id = %(id)s
        """
        self._execute(query, {"id": prediction_id})
        logger.info("prediccion_marcada_medida", id=prediction_id)

    def mark_prediction_contaminated(self, prediction_id: int) -> None:
        """Marca una prediccion como contaminada por otro evento.

        Args:
            prediction_id: ID de la prediccion a marcar.
        """
        query = """
            UPDATE source_predictions
            SET contaminada = TRUE
            WHERE id = %(id)s
        """
        self._execute(query, {"id": prediction_id})
        logger.info("prediccion_marcada_contaminada", id=prediction_id)

    def schedule_prediction_measurement(
        self, prediction_id: int, medicion_at: datetime
    ) -> None:
        """Programa el timestamp de medicion de una prediccion.

        Args:
            prediction_id: ID de la prediccion.
            medicion_at: Timestamp en que se debe medir.
        """
        query = """
            UPDATE source_predictions
            SET medicion_schedulada = %(medicion_at)s
            WHERE id = %(id)s
        """
        self._execute(
            query, {"id": prediction_id, "medicion_at": medicion_at}
        )
        logger.info(
            "medicion_schedulada",
            id=prediction_id,
            medicion_at=str(medicion_at),
        )

    def insert_source_accuracy(self, data: dict[str, Any]) -> None:
        """Inserta un registro de precision de medicion.

        Args:
            data: Dict con los campos de source_accuracy:
                - prediction_id (int)
                - username (str)
                - impacto_predicho (float)
                - impacto_real (float)
                - acierto_direccion (bool)
                - error_magnitud (float)
                - contaminada (bool)
        """
        query = """
            INSERT INTO source_accuracy
                (prediction_id, username, impacto_predicho, impacto_real,
                 acierto_direccion, error_magnitud, contaminada)
            VALUES
                (%(prediction_id)s, %(username)s, %(impacto_predicho)s,
                 %(impacto_real)s, %(acierto_direccion)s,
                 %(error_magnitud)s, %(contaminada)s)
        """
        self._execute(query, data)
        logger.info(
            "accuracy_registrada",
            prediction_id=data.get("prediction_id"),
            username=data.get("username"),
            acierto=data.get("acierto_direccion"),
        )

    def get_accuracy_for_source(
        self, username: str, since_date: date
    ) -> list[dict[str, Any]]:
        """Retorna registros de precision para una fuente desde una fecha.

        Args:
            username: Username de la fuente (ej: 'BancoCentral_AR').
            since_date: Fecha desde la cual buscar registros.

        Returns:
            Lista de dicts con los registros de accuracy.
        """
        query = """
            SELECT * FROM source_accuracy
            WHERE username = %(username)s
              AND medido_at >= %(since_date)s
            ORDER BY medido_at ASC
        """
        params = {"username": username, "since_date": since_date}
        return self._execute(query, params, fetch_all=True) or []  # type: ignore[return-value]

    def insert_weight_history(self, data: dict[str, Any]) -> None:
        """Inserta un registro en el historial de cambios de pesos.

        Args:
            data: Dict con los campos de source_weights_history:
                - username (str)
                - peso_anterior (float)
                - peso_nuevo (float)
                - win_rate (float)
                - n_eventos (int)
        """
        query = """
            INSERT INTO source_weights_history
                (username, peso_anterior, peso_nuevo, win_rate, n_eventos)
            VALUES
                (%(username)s, %(peso_anterior)s, %(peso_nuevo)s,
                 %(win_rate)s, %(n_eventos)s)
        """
        self._execute(query, data)
        logger.info(
            "peso_historial_registrado",
            username=data.get("username"),
            peso_nuevo=data.get("peso_nuevo"),
        )

    def get_market_contexts_in_range(
        self, desde: datetime, hasta: datetime
    ) -> list[dict[str, Any]]:
        """Retorna contextos de mercado en un rango de timestamps.

        Usado por el feedback engine para detectar contaminacion:
        si otro evento de alto impacto ocurrio durante la ventana de
        medicion de una prediccion, la medicion se marca contaminada.

        Args:
            desde: Timestamp de inicio (inclusivo).
            hasta: Timestamp de fin (inclusivo).

        Returns:
            Lista de dicts con los contextos de mercado en el rango.
        """
        query = """
            SELECT * FROM market_context
            WHERE timestamp >= %(desde)s AND timestamp <= %(hasta)s
            ORDER BY timestamp ASC
        """
        return self._execute(query, {"desde": desde, "hasta": hasta}, fetch_all=True) or []  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Cierra todas las conexiones del pool."""
        if self._pool and not self._pool.closed:
            self._pool.closeall()
            logger.info("pool_de_conexiones_cerrado")
