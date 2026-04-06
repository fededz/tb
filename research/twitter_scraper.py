"""Scraper de X/Twitter via instancias de Nitter.

Obtiene tweets recientes de las cuentas configuradas parseando el HTML
de Nitter. Si Nitter esta caido o no responde, retorna lista vacia
sin afectar al resto del sistema.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Timeout para requests HTTP a Nitter
REQUEST_TIMEOUT_SECONDS: float = 15.0

# Maximo de tweets a extraer por cuenta
MAX_TWEETS_PER_ACCOUNT: int = 10


class TwitterScraper:
    """Scraper de tweets recientes via Nitter.

    Parsea el HTML de las paginas de perfil de Nitter para extraer
    texto y timestamps de los tweets. Es un scraping basico que puede
    romperse si Nitter cambia su HTML — en ese caso, loguea el error
    y retorna lista vacia.
    """

    def __init__(
        self,
        accounts: list[str],
        nitter_url: str,
    ) -> None:
        """Inicializa el scraper con las cuentas y URL de Nitter.

        Args:
            accounts: Lista de handles de X/Twitter (sin @).
            nitter_url: URL base de la instancia de Nitter a usar.
        """
        self._accounts = accounts
        self._nitter_url = nitter_url.rstrip("/")

    def _parse_tweets_from_html(self, html: str, account: str) -> list[dict]:
        """Extrae tweets del HTML de una pagina de perfil de Nitter.

        Busca los bloques de contenido de tweets usando patrones regex
        sobre las clases CSS de Nitter.

        Args:
            html: HTML completo de la pagina del perfil.
            account: Handle de la cuenta (para el campo source).

        Returns:
            Lista de dicts con source, title, content, timestamp, url.
        """
        tweets: list[dict] = []

        # Patron para extraer el texto de cada tweet
        # Nitter usa <div class="tweet-content media-body"> para el contenido
        content_pattern = re.compile(
            r'<div[^>]*class="[^"]*tweet-content[^"]*"[^>]*>(.*?)</div>',
            re.DOTALL,
        )

        # Patron para extraer timestamps
        # Nitter usa <span class="tweet-date"><a ... title="TIMESTAMP">
        timestamp_pattern = re.compile(
            r'<span[^>]*class="[^"]*tweet-date[^"]*"[^>]*>'
            r'<a[^>]*title="([^"]*)"',
            re.DOTALL,
        )

        # Patron para extraer links de tweets individuales
        link_pattern = re.compile(
            r'<a[^>]*class="[^"]*tweet-link[^"]*"[^>]*href="([^"]*)"',
            re.DOTALL,
        )

        contents = content_pattern.findall(html)
        timestamps = timestamp_pattern.findall(html)
        links = link_pattern.findall(html)

        for i, content_html in enumerate(contents[:MAX_TWEETS_PER_ACCOUNT]):
            # Limpiar HTML tags del contenido
            clean_text = re.sub(r"<[^>]+>", "", content_html).strip()
            if not clean_text:
                continue

            # Timestamp
            timestamp_str = ""
            if i < len(timestamps):
                timestamp_str = timestamps[i]
            if not timestamp_str:
                timestamp_str = datetime.now(timezone.utc).isoformat()

            # URL del tweet
            tweet_url = ""
            if i < len(links):
                tweet_url = f"{self._nitter_url}{links[i]}"
            else:
                tweet_url = f"{self._nitter_url}/{account}"

            # Titulo: primera linea o primeros 100 caracteres
            title = clean_text[:100]
            if "\n" in clean_text:
                title = clean_text.split("\n")[0][:100]

            tweets.append({
                "source": f"twitter:@{account}",
                "title": title,
                "content": clean_text[:2000],
                "timestamp": timestamp_str,
                "url": tweet_url,
            })

        return tweets

    def _scrape_account(self, account: str) -> list[dict]:
        """Hace scraping de los tweets recientes de una cuenta.

        Args:
            account: Handle de X/Twitter (sin @).

        Returns:
            Lista de tweets parseados, o lista vacia si hay error.
        """
        url = f"{self._nitter_url}/{account}"

        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                response = client.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0)",
                        "Accept": "text/html",
                    },
                    follow_redirects=True,
                )
                response.raise_for_status()
        except httpx.TimeoutException:
            logger.warning("nitter_timeout", account=account, url=url)
            return []
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "nitter_http_error",
                account=account,
                status=exc.response.status_code,
            )
            return []
        except httpx.HTTPError:
            logger.warning("nitter_request_error", account=account)
            return []

        return self._parse_tweets_from_html(response.text, account)

    def collect(self) -> list[dict]:
        """Recolecta tweets recientes de todas las cuentas configuradas.

        Procesa cada cuenta de forma independiente. Si Nitter esta caido
        o una cuenta no existe, loguea un warning y continua con las demas.

        Returns:
            Lista combinada de tweets de todas las cuentas, cada uno con:
            source, title, content, timestamp, url.
        """
        if not self._accounts:
            logger.debug("twitter_scraper_sin_cuentas_configuradas")
            return []

        all_tweets: list[dict] = []

        for account in self._accounts:
            account = account.strip().lstrip("@")
            if not account:
                continue

            try:
                tweets = self._scrape_account(account)
                all_tweets.extend(tweets)
                logger.debug(
                    "cuenta_scrapeada",
                    account=account,
                    tweets=len(tweets),
                )
            except Exception:
                logger.exception("error_scrapeando_cuenta", account=account)

        logger.info("twitter_recoleccion_completa", total_tweets=len(all_tweets))
        return all_tweets
