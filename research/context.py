"""Lectura y escritura del contexto de mercado en la base de datos.

Provee ContextReader para que las estrategias consulten el estado actual
del mercado, y ContextWriter para que el research agent persista los
resultados del analisis.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog

from db.repository import Repository

logger = structlog.get_logger(__name__)


class ContextReader:
    """Lee el contexto de mercado mas reciente desde la base de datos.

    Las estrategias de trading consultan este reader antes de generar
    senales para ajustar sizing, pausar operaciones, o detener
    completamente ante riesgo critico.
    """

    def __init__(self, repository: Repository) -> None:
        """Inicializa el reader con acceso al repositorio.

        Args:
            repository: Instancia del repositorio de datos.
        """
        self._repository = repository

    def get_current_context(self) -> dict[str, Any] | None:
        """Lee el contexto de mercado mas reciente de la DB.

        Returns:
            Dict con el contexto completo (riesgo_macro, sentimiento,
            sizing_mult, eventos, estrategias_pausadas, resumen),
            o None si no hay contexto almacenado.
        """
        try:
            context = self._repository.get_latest_market_context()
            if context is None:
                logger.debug("sin_contexto_mercado_en_db")
                return None

            # Parsear campos JSONB si vienen como string
            if isinstance(context.get("eventos"), str):
                context["eventos"] = json.loads(context["eventos"])
            if isinstance(context.get("estrategias_pausadas"), str):
                context["estrategias_pausadas"] = json.loads(
                    context["estrategias_pausadas"]
                )

            return context
        except Exception:
            logger.exception("error_leyendo_contexto_mercado")
            return None

    def is_strategy_paused(self, strategy_name: str) -> bool:
        """Verifica si una estrategia esta pausada por el research agent.

        Args:
            strategy_name: Nombre de la estrategia a verificar.

        Returns:
            True si la estrategia esta en la lista de pausadas, False
            si no esta pausada o si no hay contexto disponible.
        """
        context = self.get_current_context()
        if context is None:
            return False

        pausadas = context.get("estrategias_pausadas")
        if pausadas is None:
            return False

        if isinstance(pausadas, str):
            try:
                pausadas = json.loads(pausadas)
            except (json.JSONDecodeError, TypeError):
                return False

        return strategy_name in pausadas

    def get_sizing_multiplier(self) -> float:
        """Obtiene el multiplicador de sizing actual.

        El sizing_multiplier se aplica a todas las ordenes:
        - 1.0 = tamano normal
        - 0.5 = mitad del tamano
        - 0.0 = no operar

        Returns:
            Float entre 0.0 y 1.0. Retorna 1.0 por defecto si no hay
            contexto o si hay error.
        """
        context = self.get_current_context()
        if context is None:
            return 1.0

        sizing = context.get("sizing_mult", 1.0)
        try:
            sizing = float(sizing)
            return max(0.0, min(1.0, sizing))
        except (ValueError, TypeError):
            logger.warning("sizing_multiplier_invalido", valor=sizing)
            return 1.0

    def get_riesgo_macro(self) -> str:
        """Obtiene el nivel de riesgo macro actual.

        Returns:
            Uno de: "bajo", "medio", "alto", "critico".
            Retorna "medio" por defecto si no hay contexto.
        """
        context = self.get_current_context()
        if context is None:
            return "medio"

        riesgo = context.get("riesgo_macro", "medio")
        valid_values = {"bajo", "medio", "alto", "critico"}
        if riesgo not in valid_values:
            logger.warning("riesgo_macro_invalido", valor=riesgo)
            return "medio"

        return riesgo


class ContextWriter:
    """Persiste el contexto de mercado producido por el research agent.

    Escribe los resultados del analisis en la tabla market_context
    para que las estrategias los consulten via ContextReader.
    """

    def __init__(self, repository: Repository) -> None:
        """Inicializa el writer con acceso al repositorio.

        Args:
            repository: Instancia del repositorio de datos.
        """
        self._repository = repository

    def save_context(self, context: dict) -> None:
        """Guarda un nuevo contexto de mercado en la base de datos.

        Args:
            context: Dict con el contexto producido por el analyzer.
                Campos esperados:
                - riesgo_macro (str): "bajo" | "medio" | "alto" | "critico"
                - sentimiento (float): -1.0 a 1.0
                - sizing_multiplier (float): 0.0 a 1.0
                - eventos_activos (list): Lista de eventos detectados
                - estrategias_pausadas (list[str]): Estrategias a pausar
                - resumen (str): Texto libre del analisis
                - timestamp (str): ISO 8601 timestamp

        Raises:
            Exception: Si hay un error escribiendo en la DB (se loguea y re-lanza).
        """
        try:
            timestamp_str = context.get("timestamp", "")
            if timestamp_str:
                try:
                    timestamp = datetime.fromisoformat(timestamp_str)
                except (ValueError, TypeError):
                    timestamp = datetime.now(timezone.utc)
            else:
                timestamp = datetime.now(timezone.utc)

            eventos = context.get("eventos_activos", [])
            estrategias_pausadas = context.get("estrategias_pausadas", [])
            sizing = context.get("sizing_multiplier", 1.0)

            self._repository.insert_market_context(
                timestamp=timestamp,
                riesgo_macro=context.get("riesgo_macro", "medio"),
                sentimiento=context.get("sentimiento"),
                sizing_mult=float(sizing),
                eventos=eventos,
                estrategias_pausadas=estrategias_pausadas,
                resumen=context.get("resumen"),
                fuentes_count=context.get("fuentes_count"),
            )

            logger.info(
                "contexto_guardado",
                riesgo=context.get("riesgo_macro"),
                sizing=sizing,
                eventos=len(eventos),
            )
        except Exception:
            logger.exception("error_guardando_contexto_mercado")
            raise
