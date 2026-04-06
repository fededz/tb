"""Recolector de datos financieros estructurados de APIs publicas argentinas.

Obtiene tipos de cambio (oficial, blue, MEP, CCL), brecha cambiaria,
y forecasts del FMI desde endpoints verificados. Cada fuente se consulta
de forma independiente: si una falla, las demas siguen funcionando.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Timeouts para requests HTTP
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def _parse_ar_number(value: str) -> float | None:
    """Parsea un numero en formato argentino ('1.380,00') a float.

    Maneja formatos como '1.380,00', '1380,00', '1380.00', y numeros
    sin separador de miles.

    Args:
        value: String con el numero en formato argentino o estandar.

    Returns:
        Float parseado, o None si el formato es invalido.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        # Remover espacios
        cleaned = value.strip()
        # Formato argentino: punto como separador de miles, coma como decimal
        # Detectar si tiene coma como separador decimal
        if "," in cleaned:
            # Remover puntos de miles y reemplazar coma decimal por punto
            cleaned = cleaned.replace(".", "").replace(",", ".")
        return float(cleaned)
    except (ValueError, TypeError):
        logger.warning("parse_ar_number_fallido", valor_original=value)
        return None


class StructuredDataCollector:
    """Recolecta datos financieros estructurados de APIs publicas argentinas.

    Consulta endpoints verificados del BCRA, Ambito.com y FMI para obtener
    tipos de cambio, cotizaciones del dolar y proyecciones de crecimiento.
    Todos los datos se recolectan con tolerancia a fallos individuales.
    """

    def __init__(self) -> None:
        """Inicializa el collector con un cliente HTTP compartido."""
        self._client: httpx.Client | None = None
        self._last_data: dict | None = None

    def _get_client(self) -> httpx.Client:
        """Obtiene o crea el cliente HTTP de forma lazy.

        Returns:
            Instancia de httpx.Client configurada con timeout.
        """
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=_TIMEOUT,
                headers={"User-Agent": "TradingBot/1.0"},
                follow_redirects=True,
            )
        return self._client

    def collect_all(self) -> dict:
        """Recolecta todos los datos disponibles de las APIs configuradas.

        Cada fuente se consulta de forma independiente. Si una falla,
        su clave tendra valor None en el resultado.

        Returns:
            Dict estructurado con los datos recolectados:
            - bcra_fx: Tipo de cambio oficial del BCRA
            - dolar_blue: Cotizacion del dolar blue
            - dolar_mep: Cotizacion del dolar MEP
            - dolar_ccl: Cotizacion del dolar CCL
            - brecha: Brecha cambiaria oficial/blue (porcentaje)
            - imf_growth: Proyecciones de crecimiento del FMI
            - timestamp: Momento de la recoleccion
        """
        logger.info("structured_data_recoleccion_inicio")

        bcra_fx = self._fetch_bcra_fx()
        dolar_blue = self._fetch_dolar_blue()
        dolar_mep = self._fetch_dolar_mep()
        dolar_ccl = self._fetch_dolar_ccl()
        imf_growth = self._fetch_imf_growth()

        # Calcular brecha si tenemos ambos datos
        brecha: float | None = None
        oficial_venta = None
        blue_venta = None

        if bcra_fx and bcra_fx.get("usd_venta"):
            oficial_venta = bcra_fx["usd_venta"]
        if dolar_blue and dolar_blue.get("venta"):
            blue_venta = dolar_blue["venta"]

        if oficial_venta and blue_venta and oficial_venta > 0:
            brecha = self._calc_brecha_cambiaria(oficial_venta, blue_venta)

        data = {
            "bcra_fx": bcra_fx,
            "dolar_blue": dolar_blue,
            "dolar_mep": dolar_mep,
            "dolar_ccl": dolar_ccl,
            "brecha": brecha,
            "imf_growth": imf_growth,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        self._last_data = data

        fuentes_ok = sum(
            1 for k in ("bcra_fx", "dolar_blue", "dolar_mep", "dolar_ccl", "imf_growth")
            if data.get(k) is not None
        )
        logger.info(
            "structured_data_recoleccion_completa",
            fuentes_ok=fuentes_ok,
            fuentes_total=5,
        )

        return data

    def _fetch_bcra_fx(self) -> dict | None:
        """Tipo de cambio oficial del BCRA.

        Consulta el endpoint de cotizaciones del BCRA y extrae
        la cotizacion del USD.

        Returns:
            Dict con usd_compra, usd_venta, fecha, o None si falla.
        """
        url = "https://api.bcra.gob.ar/estadisticascambiarias/v1.0/Cotizaciones"
        try:
            client = self._get_client()
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results", {})
            fecha = results.get("fecha", "")
            detalle = results.get("detalle", [])

            for item in detalle:
                if item.get("codigoMoneda") == "USD":
                    tipo_cotizacion = item.get("tipoCotizacion")
                    # El endpoint puede devolver compra y venta por separado
                    # o un solo valor. Manejar ambos casos.
                    result = {
                        "fecha": fecha,
                        "usd_venta": float(tipo_cotizacion) if tipo_cotizacion else None,
                    }
                    # Buscar si hay campo de compra separado
                    tipo_compra = item.get("tipoCotizacionCompra")
                    if tipo_compra:
                        result["usd_compra"] = float(tipo_compra)

                    logger.info("bcra_fx_ok", venta=result.get("usd_venta"), fecha=fecha)
                    return result

            logger.warning("bcra_fx_usd_no_encontrado", detalle_count=len(detalle))
            return None

        except httpx.HTTPStatusError as exc:
            logger.warning("bcra_fx_http_error", status=exc.response.status_code)
        except httpx.RequestError as exc:
            logger.warning("bcra_fx_request_error", error=str(exc))
        except Exception:
            logger.exception("bcra_fx_error_inesperado")
        return None

    def _fetch_dolar_blue(self) -> dict | None:
        """Dolar blue (informal) de Ambito.com.

        Returns:
            Dict con compra, venta, variacion, fecha, o None si falla.
        """
        url = "https://mercados.ambito.com/dolar/informal/variacion"
        try:
            client = self._get_client()
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()

            compra = _parse_ar_number(data.get("compra", ""))
            venta = _parse_ar_number(data.get("venta", ""))
            variacion = data.get("variacion", "")
            fecha = data.get("fecha", "")

            if venta is None:
                logger.warning("dolar_blue_venta_parse_error", raw=data.get("venta"))
                return None

            result = {
                "compra": compra,
                "venta": venta,
                "variacion": variacion,
                "fecha": fecha,
            }
            logger.info("dolar_blue_ok", venta=venta, variacion=variacion)
            return result

        except httpx.HTTPStatusError as exc:
            logger.warning("dolar_blue_http_error", status=exc.response.status_code)
        except httpx.RequestError as exc:
            logger.warning("dolar_blue_request_error", error=str(exc))
        except Exception:
            logger.exception("dolar_blue_error_inesperado")
        return None

    def _fetch_dolar_mep(self) -> dict | None:
        """Dolar MEP de Ambito.com.

        Returns:
            Dict con valor, variacion, o None si falla.
        """
        url = "https://mercados.ambito.com/dolarrava/mep/variacion"
        try:
            client = self._get_client()
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()

            valor = _parse_ar_number(data.get("valor", ""))
            variacion = data.get("variacion", "")

            if valor is None:
                logger.warning("dolar_mep_valor_parse_error", raw=data.get("valor"))
                return None

            result = {
                "valor": valor,
                "variacion": variacion,
            }
            logger.info("dolar_mep_ok", valor=valor, variacion=variacion)
            return result

        except httpx.HTTPStatusError as exc:
            logger.warning("dolar_mep_http_error", status=exc.response.status_code)
        except httpx.RequestError as exc:
            logger.warning("dolar_mep_request_error", error=str(exc))
        except Exception:
            logger.exception("dolar_mep_error_inesperado")
        return None

    def _fetch_dolar_ccl(self) -> dict | None:
        """Dolar CCL (contado con liquidacion) de Ambito.com.

        Returns:
            Dict con valor, variacion, o None si falla.
        """
        url = "https://mercados.ambito.com/dolarrava/cl/variacion"
        try:
            client = self._get_client()
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()

            valor = _parse_ar_number(data.get("valor", ""))
            variacion = data.get("variacion", "")

            if valor is None:
                logger.warning("dolar_ccl_valor_parse_error", raw=data.get("valor"))
                return None

            result = {
                "valor": valor,
                "variacion": variacion,
            }
            logger.info("dolar_ccl_ok", valor=valor, variacion=variacion)
            return result

        except httpx.HTTPStatusError as exc:
            logger.warning("dolar_ccl_http_error", status=exc.response.status_code)
        except httpx.RequestError as exc:
            logger.warning("dolar_ccl_request_error", error=str(exc))
        except Exception:
            logger.exception("dolar_ccl_error_inesperado")
        return None

    def _calc_brecha_cambiaria(self, oficial: float, blue: float) -> float:
        """Calcula la brecha porcentual entre el dolar oficial y el blue.

        Args:
            oficial: Tipo de cambio oficial (venta) en ARS/USD.
            blue: Tipo de cambio blue (venta) en ARS/USD.

        Returns:
            Brecha como porcentaje (ej: 5.0 para 5%).
        """
        if oficial <= 0:
            return 0.0
        brecha = ((blue - oficial) / oficial) * 100.0
        logger.info("brecha_cambiaria_calculada", brecha_pct=round(brecha, 2))
        return round(brecha, 2)

    def _fetch_imf_growth(self) -> dict | None:
        """GDP growth forecast del FMI para Argentina.

        Consulta el DataMapper del FMI para obtener las proyecciones
        de crecimiento del PBI real.

        Returns:
            Dict con anio como clave y crecimiento como valor, o None si falla.
            Ejemplo: {"2024": -1.3, "2025": 4.5, "2026": 4.0}
        """
        url = (
            "https://www.imf.org/external/datamapper/api/v1/"
            "NGDP_RPCH/ARG?periods=2024,2025,2026"
        )
        try:
            client = self._get_client()
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()

            values = data.get("values", {}).get("NGDP_RPCH", {}).get("ARG", {})
            if not values:
                logger.warning("imf_growth_sin_datos")
                return None

            # Convertir claves a string y valores a float
            result = {str(k): float(v) for k, v in values.items()}
            logger.info("imf_growth_ok", periodos=list(result.keys()))
            return result

        except httpx.HTTPStatusError as exc:
            logger.warning("imf_growth_http_error", status=exc.response.status_code)
        except httpx.RequestError as exc:
            logger.warning("imf_growth_request_error", error=str(exc))
        except Exception:
            logger.exception("imf_growth_error_inesperado")
        return None

    def format_for_analyzer(self) -> str:
        """Formatea los datos recolectados como texto para el prompt del analyzer.

        Usa los datos de la ultima llamada a collect_all(). Si no se ha
        llamado a collect_all() previamente, retorna un string vacio.

        Returns:
            Bloque de texto formateado con los datos de mercado, listo
            para insertar en el prompt de Claude. String vacio si no hay datos.
        """
        data = self._last_data
        if not data:
            return ""

        timestamp = data.get("timestamp", "")
        # Formatear fecha legible
        try:
            dt = datetime.fromisoformat(timestamp)
            fecha_display = dt.strftime("%Y-%m-%d %H:%M UTC")
        except (ValueError, TypeError):
            fecha_display = timestamp

        lines: list[str] = [
            f"DATOS ESTRUCTURADOS DE MERCADO ({fecha_display}):",
            "",
        ]

        # --- Tipos de cambio ---
        has_fx_data = False
        bcra = data.get("bcra_fx")
        blue = data.get("dolar_blue")
        mep = data.get("dolar_mep")
        ccl = data.get("dolar_ccl")
        brecha = data.get("brecha")

        if any([bcra, blue, mep, ccl]):
            has_fx_data = True
            lines.append("Tipo de cambio:")

            if bcra and bcra.get("usd_venta"):
                lines.append(f"- Oficial: ${bcra['usd_venta']:,.2f} ARS/USD")

            if blue:
                venta_str = f"${blue['venta']:,.2f}" if blue.get("venta") else "N/D"
                compra_str = (
                    f" (compra ${blue['compra']:,.2f})" if blue.get("compra") else ""
                )
                lines.append(f"- Blue: {venta_str}{compra_str}")

            if mep and mep.get("valor"):
                var_str = f" ({mep['variacion']})" if mep.get("variacion") else ""
                lines.append(f"- MEP: ${mep['valor']:,.2f}{var_str}")

            if ccl and ccl.get("valor"):
                var_str = f" ({ccl['variacion']})" if ccl.get("variacion") else ""
                lines.append(f"- CCL: ${ccl['valor']:,.2f}{var_str}")

            if brecha is not None:
                lines.append(f"- Brecha oficial/blue: {brecha:.1f}%")

            lines.append("")

        # --- Variaciones del dia ---
        variaciones: list[str] = []
        if blue and blue.get("variacion"):
            variaciones.append(f"- Blue: {blue['variacion']}")
        if mep and mep.get("variacion"):
            variaciones.append(f"- MEP: {mep['variacion']}")
        if ccl and ccl.get("variacion"):
            variaciones.append(f"- CCL: {ccl['variacion']}")

        if variaciones:
            lines.append("Variaciones del dia:")
            lines.extend(variaciones)
            lines.append("")

        # --- IMF GDP Growth ---
        imf = data.get("imf_growth")
        if imf:
            lines.append("IMF GDP Growth Argentina:")
            for year in sorted(imf.keys()):
                value = imf[year]
                sign = "+" if value > 0 else ""
                lines.append(f"- {year}: {sign}{value}%")
            lines.append("")

        # Si no hay ningun dato disponible
        if not has_fx_data and not imf:
            return ""

        return "\n".join(lines)

    def close(self) -> None:
        """Cierra el cliente HTTP si esta abierto."""
        if self._client and not self._client.is_closed:
            self._client.close()
            logger.info("structured_data_client_cerrado")

    def __del__(self) -> None:
        """Cierra el cliente HTTP al destruir la instancia."""
        try:
            self.close()
        except Exception:
            pass
