"""Descarga y cache de datos historicos de mercado.

Consulta primero la tabla market_data_cache en PostgreSQL. Para las fechas
faltantes, descarga desde la API de PPI y persiste en DB para futuras consultas.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import pandas as pd
import structlog

if TYPE_CHECKING:
    from core.ppi_wrapper import PPIWrapper
    from db.repository import Repository

logger = structlog.get_logger(__name__)

# Columnas estandar del DataFrame retornado
COLUMNS = ["fecha", "open", "high", "low", "close", "volume"]


class HistoricalData:
    """Fetcher de datos historicos con cache en base de datos.

    Combina datos cacheados en PostgreSQL con descargas frescas de la API
    de PPI para minimizar llamadas al servicio externo.
    """

    def __init__(self, ppi: PPIWrapper, repository: Repository) -> None:
        """Inicializa el fetcher de datos historicos.

        Args:
            ppi: Wrapper de la API de PPI para descargar datos.
            repository: Repositorio de base de datos para leer/escribir cache.
        """
        self._ppi = ppi
        self._repo = repository
        logger.info("historical_data.inicializado")

    def get(
        self,
        ticker: str,
        tipo: str,
        plazo: str,
        desde: date,
        hasta: date,
    ) -> pd.DataFrame:
        """Obtiene datos historicos OHLCV para un instrumento y rango de fechas.

        Primero consulta el cache en DB. Para las fechas faltantes, descarga
        desde la API de PPI y las persiste en el cache.

        Args:
            ticker: Simbolo del instrumento (ej: "GGAL", "AL30").
            tipo: Tipo de instrumento (ej: "Acciones", "Bonos").
            plazo: Plazo de liquidacion (ej: "A-48HS", "INMEDIATA").
            desde: Fecha de inicio (inclusive).
            hasta: Fecha de fin (inclusive).

        Returns:
            DataFrame con columnas [fecha, open, high, low, close, volume],
            ordenado por fecha ascendente. Puede estar vacio si no hay datos.
        """
        if desde > hasta:
            logger.warning(
                "historical_data.rango_invalido",
                ticker=ticker,
                desde=str(desde),
                hasta=str(hasta),
            )
            return pd.DataFrame(columns=COLUMNS)

        # Paso 1: obtener datos cacheados en DB
        cached_df = self._load_from_cache(ticker, tipo, plazo, desde, hasta)
        cached_dates = set(cached_df["fecha"].tolist()) if not cached_df.empty else set()

        # Paso 2: determinar fechas faltantes
        all_dates = self._date_range(desde, hasta)
        missing_dates = sorted(all_dates - cached_dates)

        if not missing_dates:
            logger.debug(
                "historical_data.cache_completo",
                ticker=ticker,
                desde=str(desde),
                hasta=str(hasta),
            )
            return cached_df.sort_values("fecha").reset_index(drop=True)

        # Paso 3: descargar fechas faltantes desde PPI
        logger.info(
            "historical_data.descargando_faltantes",
            ticker=ticker,
            missing_count=len(missing_dates),
            desde=str(missing_dates[0]),
            hasta=str(missing_dates[-1]),
        )
        fetched_df = self._fetch_from_api(
            ticker, tipo, plazo, missing_dates[0], missing_dates[-1]
        )

        # Paso 4: persistir en cache
        if not fetched_df.empty:
            self._save_to_cache(ticker, tipo, plazo, fetched_df)

        # Paso 5: combinar y retornar
        if cached_df.empty and fetched_df.empty:
            return pd.DataFrame(columns=COLUMNS)

        frames = [df for df in [cached_df, fetched_df] if not df.empty]
        result = pd.concat(frames, ignore_index=True)
        result = result.drop_duplicates(subset=["fecha"]).sort_values("fecha")
        return result.reset_index(drop=True)

    def invalidate_cache(
        self,
        ticker: str,
        tipo: str,
        plazo: str,
        fecha: date,
    ) -> None:
        """Invalida una entrada especifica del cache de datos historicos.

        Util cuando se sabe que el dato almacenado es incorrecto o incompleto
        (ej: dato parcial del dia actual que necesita actualizarse).

        Args:
            ticker: Simbolo del instrumento.
            tipo: Tipo de instrumento.
            plazo: Plazo de liquidacion.
            fecha: Fecha a invalidar.
        """
        try:
            self._repo.delete_market_data_cache(ticker, tipo, plazo, fecha)
            logger.info(
                "historical_data.cache_invalidado",
                ticker=ticker,
                tipo=tipo,
                plazo=plazo,
                fecha=str(fecha),
            )
        except Exception:
            logger.exception(
                "historical_data.error_invalidando_cache",
                ticker=ticker,
                fecha=str(fecha),
            )

    def _load_from_cache(
        self,
        ticker: str,
        tipo: str,
        plazo: str,
        desde: date,
        hasta: date,
    ) -> pd.DataFrame:
        """Carga datos desde la tabla market_data_cache en DB.

        Returns:
            DataFrame con las columnas estandar, o DataFrame vacio.
        """
        try:
            rows = self._repo.get_cached_market_data(ticker, tipo, plazo, desde, hasta)
            if not rows:
                return pd.DataFrame(columns=COLUMNS)
            df = pd.DataFrame(rows)
            # Rename DB columns to standard format
            if "fecha" in df.columns:
                df["fecha"] = pd.to_datetime(df["fecha"]).dt.date
            # Keep only standard columns that exist
            available = [c for c in COLUMNS if c in df.columns]
            df = df[available]
            return df
        except Exception:
            logger.exception(
                "historical_data.error_leyendo_cache",
                ticker=ticker,
            )
            return pd.DataFrame(columns=COLUMNS)

    def _fetch_from_api(
        self,
        ticker: str,
        tipo: str,
        plazo: str,
        desde: date,
        hasta: date,
    ) -> pd.DataFrame:
        """Descarga datos historicos desde la API de PPI.

        Returns:
            DataFrame con las columnas estandar, o DataFrame vacio si falla.
        """
        try:
            df = self._ppi.get_historical(ticker, tipo, plazo, desde, hasta)
            if df is None or df.empty:
                logger.warning(
                    "historical_data.api_sin_datos",
                    ticker=ticker,
                    desde=str(desde),
                    hasta=str(hasta),
                )
                return pd.DataFrame(columns=COLUMNS)

            # Normalizar columnas al formato estandar
            df = self._normalize_columns(df)
            logger.info(
                "historical_data.descargado",
                ticker=ticker,
                registros=len(df),
            )
            return df
        except Exception:
            logger.exception(
                "historical_data.error_descargando",
                ticker=ticker,
                desde=str(desde),
                hasta=str(hasta),
            )
            return pd.DataFrame(columns=COLUMNS)

    def _save_to_cache(
        self,
        ticker: str,
        tipo: str,
        plazo: str,
        df: pd.DataFrame,
    ) -> None:
        """Persiste datos descargados en la tabla market_data_cache.

        Args:
            ticker: Simbolo del instrumento.
            tipo: Tipo de instrumento.
            plazo: Plazo de liquidacion.
            df: DataFrame con datos a persistir.
        """
        try:
            rows_saved = 0
            for _, row in df.iterrows():
                self._repo.cache_market_data(
                    ticker=ticker,
                    tipo=tipo,
                    plazo=plazo,
                    fecha=row["fecha"],
                    open=row.get("open"),
                    high=row.get("high"),
                    low=row.get("low"),
                    close=row.get("close"),
                    volume=row.get("volume"),
                )
                rows_saved += 1
            logger.info(
                "historical_data.cache_guardado",
                ticker=ticker,
                registros=rows_saved,
            )
        except Exception:
            logger.exception(
                "historical_data.error_guardando_cache",
                ticker=ticker,
            )

    @staticmethod
    def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
        """Normaliza los nombres de columnas al formato estandar.

        La API de PPI puede devolver columnas con distintos nombres
        (ej: 'Date', 'Close', 'Volume'). Este metodo los mapea a
        los nombres estandar en minuscula.

        Returns:
            DataFrame con columnas renombradas.
        """
        column_map = {
            "Date": "fecha",
            "date": "fecha",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
        df = df.rename(columns=column_map)

        # Asegurar que 'fecha' sea date, no datetime
        if "fecha" in df.columns:
            df["fecha"] = pd.to_datetime(df["fecha"]).dt.date

        # Filtrar solo columnas estandar
        available = [c for c in COLUMNS if c in df.columns]
        return df[available]

    @staticmethod
    def _date_range(desde: date, hasta: date) -> set[date]:
        """Genera el conjunto de fechas habiles (lunes a viernes) en el rango.

        No filtra feriados — eso se maneja al verificar si hay datos
        reales disponibles. Solo excluye sabados y domingos.

        Returns:
            Set de dates entre desde y hasta (inclusive), sin fines de semana.
        """
        dates: set[date] = set()
        current = desde
        while current <= hasta:
            # 0=lunes, 5=sabado, 6=domingo
            if current.weekday() < 5:
                dates.add(current)
            current += timedelta(days=1)
        return dates
