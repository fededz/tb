"""Lector de feeds RSS para el research agent.

Descarga y parsea feeds RSS configurados, retornando los items
recientes en un formato unificado para el analyzer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import mktime

import feedparser
import structlog

logger = structlog.get_logger(__name__)

# Horas hacia atras para considerar una noticia como reciente
DEFAULT_LOOKBACK_HOURS: int = 6


class RSSReader:
    """Lector de feeds RSS con manejo de errores por feed individual.

    Si un feed falla (timeout, formato invalido, etc.), los demas feeds
    se procesan normalmente.
    """

    def __init__(
        self,
        feeds: list[str],
        lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    ) -> None:
        """Inicializa el lector con la lista de feeds.

        Args:
            feeds: Lista de URLs de feeds RSS a monitorear.
            lookback_hours: Cantidad de horas hacia atras para filtrar noticias recientes.
        """
        self._feeds = feeds
        self._lookback_hours = lookback_hours

    def _parse_entry_timestamp(self, entry: feedparser.FeedParserDict) -> str:
        """Extrae y normaliza el timestamp de un entry RSS.

        Intenta usar published_parsed, luego updated_parsed. Si ninguno
        esta disponible, usa la hora actual.

        Args:
            entry: Entry de feedparser.

        Returns:
            Timestamp en formato ISO 8601.
        """
        time_struct = getattr(entry, "published_parsed", None) or getattr(
            entry, "updated_parsed", None
        )
        if time_struct is not None:
            dt = datetime.fromtimestamp(mktime(time_struct), tz=timezone.utc)
            return dt.isoformat()
        return datetime.now(timezone.utc).isoformat()

    def _is_recent(self, timestamp_iso: str) -> bool:
        """Verifica si un timestamp es reciente segun el lookback configurado.

        Args:
            timestamp_iso: Timestamp en formato ISO 8601.

        Returns:
            True si el timestamp es mas reciente que now - lookback_hours.
        """
        try:
            dt = datetime.fromisoformat(timestamp_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=self._lookback_hours)
            return dt >= cutoff
        except (ValueError, TypeError):
            # Si no podemos parsear el timestamp, lo incluimos por las dudas
            return True

    def _parse_feed(self, feed_url: str) -> list[dict]:
        """Parsea un feed RSS individual y retorna los items recientes.

        Args:
            feed_url: URL del feed RSS.

        Returns:
            Lista de dicts con source, title, content, timestamp, url.
        """
        items: list[dict] = []

        try:
            feed = feedparser.parse(feed_url)
        except Exception:
            logger.exception("error_parseando_feed", feed_url=feed_url)
            return items

        if feed.bozo and not feed.entries:
            logger.warning(
                "feed_con_errores_sin_entries",
                feed_url=feed_url,
                bozo_exception=str(getattr(feed, "bozo_exception", "unknown")),
            )
            return items

        feed_title = getattr(feed.feed, "title", feed_url)

        for entry in feed.entries:
            timestamp = self._parse_entry_timestamp(entry)

            if not self._is_recent(timestamp):
                continue

            title = getattr(entry, "title", "Sin titulo")
            # Usar summary si esta disponible, sino description
            content = getattr(entry, "summary", "") or getattr(
                entry, "description", ""
            )
            url = getattr(entry, "link", feed_url)

            items.append({
                "source": f"rss:{feed_title}",
                "title": title,
                "content": content[:2000],  # Limitar contenido para el LLM
                "timestamp": timestamp,
                "url": url,
            })

        return items

    def collect(self) -> list[dict]:
        """Recolecta noticias recientes de todos los feeds configurados.

        Procesa cada feed de forma independiente. Si un feed falla,
        los demas se procesan normalmente.

        Returns:
            Lista combinada de items de todos los feeds, cada uno con:
            source, title, content, timestamp, url.
        """
        all_items: list[dict] = []

        for feed_url in self._feeds:
            try:
                items = self._parse_feed(feed_url)
                all_items.extend(items)
                logger.debug(
                    "feed_procesado",
                    feed_url=feed_url,
                    items=len(items),
                )
            except Exception:
                logger.exception("error_procesando_feed", feed_url=feed_url)

        logger.info("rss_recoleccion_completa", total_items=len(all_items))
        return all_items
