"""Recolector central de noticias para el research agent.

Combina todas las fuentes de datos (RSS, Twitter/X via Nitter) en una
lista unificada de noticias para su posterior analisis por el LLM.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from research.rss_reader import RSSReader
    from research.twitter_scraper import TwitterScraper

logger = structlog.get_logger(__name__)


class ResearchCollector:
    """Recolector que combina todas las fuentes de noticias.

    Cada fuente se procesa de forma independiente: si una falla,
    las demas siguen funcionando normalmente.
    """

    def __init__(
        self,
        twitter_scraper: TwitterScraper | None = None,
        rss_reader: RSSReader | None = None,
    ) -> None:
        """Inicializa el collector con las fuentes disponibles.

        Args:
            twitter_scraper: Scraper de X/Twitter via Nitter. Opcional.
            rss_reader: Lector de feeds RSS. Opcional.
        """
        self._twitter_scraper = twitter_scraper
        self._rss_reader = rss_reader

    def collect_all(self) -> list[dict]:
        """Recolecta noticias de todas las fuentes configuradas.

        Cada item retornado tiene la estructura:
            {
                "source": str,      # "rss" | "twitter"
                "title": str,       # Titulo o primer linea del tweet
                "content": str,     # Contenido completo o resumen
                "timestamp": str,   # ISO 8601 timestamp
                "url": str,         # URL de la fuente original
            }

        Returns:
            Lista combinada de noticias de todas las fuentes, ordenada
            por timestamp descendente (mas reciente primero).
        """
        all_items: list[dict] = []

        # --- RSS ---
        if self._rss_reader is not None:
            try:
                rss_items = self._rss_reader.collect()
                all_items.extend(rss_items)
                logger.info("rss_recolectado", items=len(rss_items))
            except Exception:
                logger.exception("error_recolectando_rss")

        # --- Twitter / X ---
        if self._twitter_scraper is not None:
            try:
                twitter_items = self._twitter_scraper.collect()
                all_items.extend(twitter_items)
                logger.info("twitter_recolectado", items=len(twitter_items))
            except Exception:
                logger.exception("error_recolectando_twitter")

        # Ordenar por timestamp descendente
        all_items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

        logger.info("recoleccion_completa", total_items=len(all_items))
        return all_items
