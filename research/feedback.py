"""Motor de feedback automatico para el research agent.

Mide si las predicciones del research agent sobre impacto de noticias
fueron correctas comparando contra datos reales de mercado de PPI.
Recalcula los pesos de cada fuente semanalmente segun su tasa de acierto.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

from core.ppi_wrapper import PPIWrapper
from db.repository import Repository

logger = structlog.get_logger(__name__)

# Rango permitido para pesos de fuentes
_MIN_PESO = 0.1
_MAX_PESO = 1.0

# Path al archivo de configuracion de fuentes
_SOURCES_PATH = Path(__file__).parent / "sources.json"


class FeedbackEngine:
    """Motor de feedback que mide la precision del research agent.

    Compara las predicciones de impacto de mercado contra datos reales
    obtenidos de PPI, detecta contaminacion por eventos concurrentes,
    y recalcula pesos de fuentes semanalmente.
    """

    def __init__(self, repository: Repository, ppi: PPIWrapper) -> None:
        """Inicializa el engine con acceso a DB y market data.

        Args:
            repository: Instancia del repositorio de datos.
            ppi: Wrapper de PPI para obtener precios historicos.
        """
        self._repository = repository
        self._ppi = ppi

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def schedule_measurement(self, prediction_id: int, ventana_min: int) -> None:
        """Programa una medicion para una prediccion despues de ventana_min minutos.

        Calcula el timestamp de medicion y lo guarda en
        source_predictions.medicion_schedulada.

        Args:
            prediction_id: ID de la prediccion en source_predictions.
            ventana_min: Minutos a esperar antes de medir el impacto real.
        """
        try:
            medicion_at = datetime.now(timezone.utc) + timedelta(minutes=ventana_min)
            self._repository.schedule_prediction_measurement(
                prediction_id, medicion_at
            )
            logger.info(
                "medicion_programada",
                prediction_id=prediction_id,
                ventana_min=ventana_min,
                medicion_at=medicion_at.isoformat(),
            )
        except Exception:
            logger.exception(
                "error_programando_medicion",
                prediction_id=prediction_id,
            )

    # ------------------------------------------------------------------
    # Medicion de impacto
    # ------------------------------------------------------------------

    def measure_impact(self, prediction_id: int) -> dict[str, Any]:
        """Mide el impacto real de mercado para una prediccion.

        Flujo:
        1. Lee la prediccion de la DB (timestamp, activos, impacto predicho).
        2. Para cada activo afectado, obtiene precio al momento del evento
           y precio al momento de la medicion.
        3. Calcula el cambio porcentual real.
        4. Compara la direccion: si ambos tienen el mismo signo, es acierto.
        5. Calcula error de magnitud.
        6. Verifica contaminacion por eventos concurrentes.
        7. Guarda resultados en source_accuracy.
        8. Marca la prediccion como medida.

        Args:
            prediction_id: ID de la prediccion a medir.

        Returns:
            Dict con los resultados de la medicion:
            - prediction_id, impacto_predicho, impacto_real,
              acierto_direccion, error_magnitud, contaminada.
            Dict vacio si hay error.
        """
        try:
            prediction = self._repository.get_prediction_by_id(prediction_id)
            if prediction is None:
                logger.warning(
                    "prediccion_no_encontrada", prediction_id=prediction_id
                )
                return {}

            if prediction.get("medida"):
                logger.debug(
                    "prediccion_ya_medida", prediction_id=prediction_id
                )
                return {}

            timestamp_evento: datetime = prediction["timestamp_evento"]
            activos_afectados = prediction.get("activos_afectados") or []
            if isinstance(activos_afectados, str):
                activos_afectados = json.loads(activos_afectados)

            impacto_predicho: float = float(prediction.get("impacto_predicho", 0.0))
            ventana_min: int = int(prediction.get("ventana_min", 30))
            username: str = prediction.get("username", "desconocido")

            # Verificar contaminacion
            contaminada = self.detect_contamination(timestamp_evento, ventana_min)
            if contaminada:
                self._repository.mark_prediction_contaminated(prediction_id)
                logger.info(
                    "prediccion_contaminada",
                    prediction_id=prediction_id,
                    ventana_min=ventana_min,
                )

            # Calcular impacto real promediando el cambio de todos los activos
            cambios: list[float] = []
            for activo in activos_afectados:
                cambio = self._get_price_change(activo, timestamp_evento, ventana_min)
                if cambio is not None:
                    cambios.append(cambio)

            if not cambios:
                logger.warning(
                    "sin_datos_de_precio_para_medir",
                    prediction_id=prediction_id,
                    activos=activos_afectados,
                )
                # Marcar como medida igualmente para no reintentar
                self._repository.mark_prediction_measured(prediction_id)
                return {}

            impacto_real = sum(cambios) / len(cambios)

            # Comparar direccion
            acierto_direccion = _misma_direccion(impacto_predicho, impacto_real)
            error_magnitud = abs(impacto_predicho - impacto_real)

            # Guardar en source_accuracy
            self._repository.insert_source_accuracy(
                {
                    "prediction_id": prediction_id,
                    "username": username,
                    "impacto_predicho": impacto_predicho,
                    "impacto_real": round(impacto_real, 4),
                    "acierto_direccion": acierto_direccion,
                    "error_magnitud": round(error_magnitud, 4),
                    "contaminada": contaminada,
                }
            )

            # Marcar prediccion como medida
            self._repository.mark_prediction_measured(prediction_id)

            result = {
                "prediction_id": prediction_id,
                "username": username,
                "impacto_predicho": impacto_predicho,
                "impacto_real": round(impacto_real, 4),
                "acierto_direccion": acierto_direccion,
                "error_magnitud": round(error_magnitud, 4),
                "contaminada": contaminada,
                "n_activos_medidos": len(cambios),
            }

            logger.info("impacto_medido", **result)
            return result

        except Exception:
            logger.exception(
                "error_midiendo_impacto", prediction_id=prediction_id
            )
            return {}

    # ------------------------------------------------------------------
    # Deteccion de contaminacion
    # ------------------------------------------------------------------

    def detect_contamination(
        self, timestamp: datetime, ventana_min: int
    ) -> bool:
        """Verifica si otro evento de alto impacto ocurrio durante la ventana.

        Consulta la tabla market_context buscando eventos con severidad
        'alta' o 'critica' entre timestamp y timestamp + ventana.

        Args:
            timestamp: Momento del evento original.
            ventana_min: Duracion de la ventana de medicion en minutos.

        Returns:
            True si hay contaminacion (otro evento de alto impacto en la ventana).
        """
        try:
            ventana_fin = timestamp + timedelta(minutes=ventana_min)

            # Buscar contextos de mercado en la ventana
            contexts = self._repository.get_market_contexts_in_range(
                timestamp, ventana_fin
            )

            for ctx in contexts:
                eventos = ctx.get("eventos")
                if isinstance(eventos, str):
                    try:
                        eventos = json.loads(eventos)
                    except (json.JSONDecodeError, TypeError):
                        continue

                if not isinstance(eventos, list):
                    continue

                for evento in eventos:
                    severidad = evento.get("severidad", "").lower()
                    if severidad in ("alta", "critica"):
                        logger.debug(
                            "contaminacion_detectada",
                            evento_tipo=evento.get("tipo"),
                            severidad=severidad,
                        )
                        return True

            return False

        except Exception:
            logger.exception("error_detectando_contaminacion")
            # Ante la duda, marcar como contaminada (conservador)
            return True

    # ------------------------------------------------------------------
    # Recalculo de pesos
    # ------------------------------------------------------------------

    def recalculate_weights(self) -> None:
        """Recalcula los pesos de todas las fuentes segun precision historica.

        Corre semanalmente (cada lunes). Para cada fuente:
        1. Obtiene registros de accuracy no contaminados de las ultimas 4 semanas.
        2. Calcula win_rate = aciertos de direccion / total.
        3. Calcula nuevo peso basado en win_rate (min 0.1, max 1.0).
        4. Guarda historial en source_weights_history.
        5. Actualiza sources.json con los nuevos pesos.
        """
        try:
            sources_config = self._load_sources_config()
            if not sources_config:
                logger.warning("sources_config_vacio_o_no_encontrado")
                return

            accounts = sources_config.get("accounts", [])
            since = date.today() - timedelta(weeks=4)
            new_weights: dict[str, float] = {}

            for account in accounts:
                username = account.get("username", "")
                if not username:
                    continue

                peso_anterior = float(account.get("peso", 1.0))

                records = self._repository.get_accuracy_for_source(
                    username, since
                )

                # Filtrar contaminados
                clean_records = [r for r in records if not r.get("contaminada")]

                if not clean_records:
                    logger.debug(
                        "sin_datos_para_recalcular_peso",
                        username=username,
                    )
                    new_weights[username] = peso_anterior
                    continue

                total = len(clean_records)
                aciertos = sum(
                    1 for r in clean_records if r.get("acierto_direccion")
                )
                win_rate = aciertos / total if total > 0 else 0.0

                # Mapear win_rate a peso: 0% -> 0.1, 50% -> 0.5, 100% -> 1.0
                # Formula lineal: peso = 0.1 + 0.9 * win_rate
                peso_nuevo = round(
                    max(_MIN_PESO, min(_MAX_PESO, 0.1 + 0.9 * win_rate)), 2
                )

                new_weights[username] = peso_nuevo

                # Guardar historial
                self._repository.insert_weight_history(
                    {
                        "username": username,
                        "peso_anterior": peso_anterior,
                        "peso_nuevo": peso_nuevo,
                        "win_rate": round(win_rate, 2),
                        "n_eventos": total,
                    }
                )

                logger.info(
                    "peso_recalculado",
                    username=username,
                    peso_anterior=peso_anterior,
                    peso_nuevo=peso_nuevo,
                    win_rate=round(win_rate, 2),
                    n_eventos=total,
                )

            # Actualizar archivo de configuracion
            self.update_sources_config(new_weights)

            logger.info(
                "recalculo_pesos_completado",
                fuentes_actualizadas=len(new_weights),
            )

        except Exception:
            logger.exception("error_recalculando_pesos")

    def update_sources_config(self, new_weights: dict[str, float]) -> None:
        """Actualiza research/sources.json con los pesos recalculados.

        Lee el JSON existente, actualiza el campo 'peso' para cada
        username presente en new_weights, y escribe de vuelta el archivo.

        Args:
            new_weights: Mapa de username -> nuevo peso.
        """
        try:
            sources_config = self._load_sources_config()
            if not sources_config:
                logger.warning("no_se_puede_actualizar_sources_sin_config")
                return

            accounts = sources_config.get("accounts", [])
            updated_count = 0

            for account in accounts:
                username = account.get("username", "")
                if username in new_weights:
                    account["peso"] = new_weights[username]
                    updated_count += 1

            _SOURCES_PATH.write_text(
                json.dumps(sources_config, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            logger.info(
                "sources_json_actualizado",
                path=str(_SOURCES_PATH),
                actualizados=updated_count,
            )

        except Exception:
            logger.exception("error_actualizando_sources_json")

    # ------------------------------------------------------------------
    # Procesamiento batch de pendientes
    # ------------------------------------------------------------------

    def process_pending_measurements(self) -> None:
        """Procesa todas las mediciones pendientes cuya ventana ya expiro.

        Busca predicciones con medida=False y medicion_schedulada <= now(),
        y mide cada una. Llamado periodicamente por el scheduler.
        """
        try:
            pending = self._repository.get_pending_predictions()
            if not pending:
                logger.debug("sin_mediciones_pendientes")
                return

            logger.info("procesando_mediciones_pendientes", total=len(pending))

            for prediction in pending:
                prediction_id = prediction["id"]
                try:
                    self.measure_impact(prediction_id)
                except Exception:
                    logger.exception(
                        "error_procesando_medicion_individual",
                        prediction_id=prediction_id,
                    )

        except Exception:
            logger.exception("error_procesando_mediciones_pendientes")

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    def _get_price_change(
        self, ticker: str, timestamp_evento: datetime, ventana_min: int
    ) -> float | None:
        """Calcula el cambio porcentual de precio para un activo.

        Obtiene el precio de cierre el dia del evento y el precio de cierre
        el dia en que la ventana de medicion expira. Calcula el cambio
        porcentual entre ambos.

        Args:
            ticker: Simbolo del instrumento (ej: 'GGAL', 'AL30').
            timestamp_evento: Momento del evento.
            ventana_min: Minutos de la ventana de medicion.

        Returns:
            Cambio porcentual como float (ej: 0.02 para +2%), o None si
            no se pudieron obtener los precios.
        """
        try:
            fecha_evento = timestamp_evento.date()
            fecha_medicion = (
                timestamp_evento + timedelta(minutes=ventana_min)
            ).date()

            # Asegurar que haya al menos un dia de diferencia para comparar cierres
            if fecha_medicion == fecha_evento:
                fecha_medicion = fecha_evento + timedelta(days=1)

            # Inferir tipo e instrumento. Usar defaults razonables para
            # los activos mas comunes del mercado argentino.
            tipo = _infer_tipo(ticker)
            plazo = _infer_plazo(tipo)

            # Obtener datos historicos: pedir un rango que cubra ambas fechas
            df = self._ppi.get_historical(
                ticker, tipo, plazo, fecha_evento, fecha_medicion
            )

            if df.empty:
                logger.debug(
                    "sin_datos_historicos_para_activo",
                    ticker=ticker,
                    fecha_evento=str(fecha_evento),
                    fecha_medicion=str(fecha_medicion),
                )
                return None

            # Buscar cierre del dia del evento
            precio_antes = self._extract_close(df, fecha_evento)
            # Buscar cierre del dia de medicion (o el mas cercano posterior)
            precio_despues = self._extract_close(df, fecha_medicion)

            if precio_antes is None or precio_despues is None:
                logger.debug(
                    "precio_no_encontrado",
                    ticker=ticker,
                    precio_antes=precio_antes,
                    precio_despues=precio_despues,
                )
                return None

            if precio_antes == 0.0:
                return None

            cambio = (precio_despues - precio_antes) / precio_antes
            return cambio

        except Exception:
            logger.exception(
                "error_obteniendo_cambio_precio",
                ticker=ticker,
            )
            return None

    def _extract_close(self, df: Any, target_date: date) -> float | None:
        """Extrae el precio de cierre de un DataFrame para una fecha dada.

        Busca la fecha exacta primero. Si no la encuentra, toma la fila
        mas cercana disponible.

        Args:
            df: DataFrame con datos historicos (columnas: date/fecha, close).
            target_date: Fecha objetivo.

        Returns:
            Precio de cierre como float, o None si no se encuentra.
        """
        if df.empty:
            return None

        # El DataFrame de PPI puede tener la columna de fecha con distintos nombres
        date_col = None
        for col_name in ("date", "fecha", "Date", "Fecha"):
            if col_name in df.columns:
                date_col = col_name
                break

        close_col = None
        for col_name in ("close", "Close", "precio_cierre"):
            if col_name in df.columns:
                close_col = col_name
                break

        if date_col is None or close_col is None:
            return None

        import pandas as pd

        df[date_col] = pd.to_datetime(df[date_col]).dt.date

        # Buscar fecha exacta
        exact = df[df[date_col] == target_date]
        if not exact.empty:
            return float(exact.iloc[0][close_col])

        # Buscar la fecha mas cercana disponible
        if not df.empty:
            # Tomar la ultima fila disponible antes o igual a target_date,
            # o la primera posterior si no hay anterior
            before = df[df[date_col] <= target_date]
            if not before.empty:
                return float(before.iloc[-1][close_col])
            after = df[df[date_col] > target_date]
            if not after.empty:
                return float(after.iloc[0][close_col])

        return None

    def _load_sources_config(self) -> dict[str, Any]:
        """Lee el archivo sources.json y lo parsea.

        Returns:
            Dict con la configuracion de fuentes, o dict vacio si hay error.
        """
        try:
            if not _SOURCES_PATH.exists():
                logger.warning(
                    "sources_json_no_encontrado", path=str(_SOURCES_PATH)
                )
                return {}

            content = _SOURCES_PATH.read_text(encoding="utf-8")
            return json.loads(content)
        except (json.JSONDecodeError, OSError):
            logger.exception("error_leyendo_sources_json")
            return {}


# ------------------------------------------------------------------
# Funciones auxiliares a nivel de modulo
# ------------------------------------------------------------------


def _misma_direccion(predicho: float, real: float) -> bool:
    """Verifica si la prediccion y el resultado real van en la misma direccion.

    Ambos positivos o ambos negativos cuentan como acierto.
    Si alguno es exactamente cero, se considera acierto (neutral).

    Args:
        predicho: Impacto predicho.
        real: Impacto real medido.

    Returns:
        True si la direccion coincide.
    """
    if predicho == 0.0 or real == 0.0:
        return True
    return (predicho > 0 and real > 0) or (predicho < 0 and real < 0)


def _infer_tipo(ticker: str) -> str:
    """Infiere el tipo de instrumento a partir del ticker.

    Heuristica basada en convenciones del mercado argentino:
    - Tickers con '/' suelen ser futuros (DLR/JUN25).
    - Tickers que empiezan con AL, GD, TX, S, T seguidos de numeros son bonos.
    - El resto se asume acciones.

    Args:
        ticker: Simbolo del instrumento.

    Returns:
        Tipo de instrumento para la API de PPI.
    """
    ticker_upper = ticker.upper()

    if "/" in ticker_upper:
        return "Futuros"

    # Bonos: AL30, GD30, TX26, S31E5, T2X5, etc.
    bonos_prefixes = ("AL", "GD", "TX", "TC", "TY", "PR", "TV")
    for prefix in bonos_prefixes:
        if ticker_upper.startswith(prefix) and len(ticker_upper) > len(prefix):
            rest = ticker_upper[len(prefix):]
            if rest[0].isdigit():
                return "Bonos"

    # Letras / Lecaps
    if ticker_upper.startswith("S") and len(ticker_upper) >= 4:
        rest = ticker_upper[1:]
        if rest[:2].isdigit():
            return "Letras"

    return "Acciones"


def _infer_plazo(tipo: str) -> str:
    """Infiere el plazo de liquidacion a partir del tipo de instrumento.

    Args:
        tipo: Tipo de instrumento.

    Returns:
        Plazo de liquidacion para la API de PPI.
    """
    if tipo == "Futuros":
        return "INMEDIATA"
    if tipo in ("Bonos", "Letras"):
        return "A-48HS"
    return "A-48HS"
