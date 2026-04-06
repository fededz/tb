"""Analizador de noticias con Claude API para producir contexto de mercado.

Envia las noticias recolectadas a Claude (Haiku) y obtiene un JSON
estructurado con el contexto de mercado: nivel de riesgo, sentimiento,
eventos activos, y recomendaciones de sizing.

Utiliza research/sources.json para enriquecer el prompt con informacion
sobre credibilidad y sesgo de cada fuente.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import structlog

import config

logger = structlog.get_logger(__name__)

# Ruta al archivo sources.json (relativa al root del proyecto)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SOURCES_JSON_PATH = _PROJECT_ROOT / "research" / "sources.json"

# Contexto por defecto cuando el analisis falla o no hay noticias
DEFAULT_CONTEXT: dict = {
    "timestamp": "",
    "riesgo_macro": "medio",
    "sentimiento": 0.0,
    "eventos_activos": [],
    "estrategias_pausadas": [],
    "sizing_multiplier": 1.0,
    "resumen": "Sin analisis disponible — usando contexto neutral por defecto.",
}

SYSTEM_PROMPT_TEMPLATE = """Sos un analista financiero especializado en mercados argentinos.

{source_context}

Analizá las siguientes noticias considerando estos sesgos de fuente y producí un JSON con este formato exacto: [schema]
Enfocate en el impacto sobre: bonos, acciones del Merval, tipo de cambio y futuros de dolar.
Sé conservador: ante la duda, subí el nivel de riesgo.
Respondé SOLO con el JSON, sin texto adicional ni markdown."""

USER_PROMPT_TEMPLATE = """Analiza las siguientes noticias recientes del mercado argentino y produci un JSON con este formato exacto:

{{
  "riesgo_macro": "bajo" | "medio" | "alto" | "critico",
  "sentimiento": <float de -1.0 (muy negativo) a 1.0 (muy positivo)>,
  "eventos_activos": [
    {{
      "tipo": "<regulatorio|economico|politico|internacional>",
      "descripcion": "<descripcion breve>",
      "impacto": ["<nombres de estrategias afectadas: carry_futuros, carry_bonos, trend_following, momentum_acciones, pares, mean_reversion>"],
      "severidad": "baja" | "media" | "alta"
    }}
  ],
  "estrategias_pausadas": ["<estrategias que deberian pausarse, lista vacia si ninguna>"],
  "sizing_multiplier": <float 0.0 a 1.0, donde 1.0 es tamano normal>,
  "resumen": "<parrafo breve con el analisis del momento actual>"
}}

Reglas para sizing_multiplier:
- riesgo_macro "critico" -> 0.0 (no operar)
- riesgo_macro "alto" -> 0.5
- riesgo_macro "medio" -> 0.75
- riesgo_macro "bajo" -> 1.0

{datos_mercado}Noticias a analizar:
{noticias}"""


class ResearchAnalyzer:
    """Analizador que usa Claude API para producir contexto de mercado.

    Envia las noticias recolectadas al modelo configurado (por defecto Haiku)
    y parsea la respuesta JSON. Si la API falla o retorna JSON invalido,
    retorna un contexto neutral por defecto.
    """

    def __init__(self) -> None:
        """Inicializa el analyzer con las credenciales de config.

        El cliente de Anthropic se crea lazy en el primer analyze()
        para evitar errores de importacion si la API key no esta configurada.
        El archivo sources.json se carga lazy y se recarga si cambia en disco.
        """
        self._api_key = config.ANTHROPIC_API_KEY
        self._model = config.RESEARCH_MODEL
        self._client = None
        self._sources_cache: dict | None = None
        self._sources_mtime: float = 0.0

    def _get_client(self):
        """Obtiene o crea el cliente de Anthropic de forma lazy.

        Returns:
            Instancia de anthropic.Anthropic.

        Raises:
            ImportError: Si el paquete anthropic no esta instalado.
            ValueError: Si ANTHROPIC_API_KEY no esta configurada.
        """
        if self._client is not None:
            return self._client

        if not self._api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY no esta configurada. "
                "El research analyzer requiere una API key valida."
            )

        import anthropic

        self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def _load_sources(self) -> dict:
        """Lee y cachea research/sources.json, recargando si el archivo cambio.

        Verifica el mtime del archivo en cada llamada. Si cambio desde
        la ultima lectura, recarga el contenido. Si el archivo no existe
        o tiene JSON invalido, retorna un dict vacio con lista de accounts.

        Returns:
            Dict parseado de sources.json con clave 'accounts'.
        """
        try:
            current_mtime = os.path.getmtime(_SOURCES_JSON_PATH)
        except OSError:
            logger.warning("sources_json_no_encontrado", path=str(_SOURCES_JSON_PATH))
            return {"accounts": []}

        if self._sources_cache is not None and current_mtime == self._sources_mtime:
            return self._sources_cache

        try:
            with open(_SOURCES_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._sources_cache = data
            self._sources_mtime = current_mtime
            logger.info(
                "sources_json_cargado",
                accounts=len(data.get("accounts", [])),
                path=str(_SOURCES_JSON_PATH),
            )
            return data
        except (json.JSONDecodeError, OSError):
            logger.exception("sources_json_error_lectura", path=str(_SOURCES_JSON_PATH))
            return {"accounts": []}

    def _build_source_context(self) -> str:
        """Genera un bloque de texto con credibilidad de fuentes para el prompt.

        Lee sources.json y formatea cada cuenta con su peso, afinidad y notas
        para que el LLM considere sesgos al analizar noticias.

        Returns:
            Texto formateado listo para insertar en el system prompt.
            String vacio si no hay fuentes configuradas.
        """
        sources = self._load_sources()
        accounts = sources.get("accounts", [])

        if not accounts:
            return ""

        lines: list[str] = [
            "Contexto sobre credibilidad de fuentes para el mercado argentino:",
            "",
        ]
        for account in accounts:
            username = account.get("username", "desconocido")
            peso = account.get("peso", 1.0)
            afinidad = account.get("afinidad_mercado", "neutral")
            notas = account.get("notas", "")
            lines.append(f"- @{username} (peso {peso}, afinidad {afinidad}): {notas}")

        return "\n".join(lines)

    def get_source_config(self, username: str) -> dict | None:
        """Retorna la configuracion de una fuente especifica de sources.json.

        Busca por username (case-insensitive) en la lista de accounts.
        Utilizado por feedback.py para obtener ventana_medicion_min y otros
        parametros de cada fuente.

        Args:
            username: Nombre de usuario de la fuente (sin @).

        Returns:
            Dict con la configuracion de la fuente, o None si no se encuentra.
        """
        sources = self._load_sources()
        username_lower = username.lower()
        for account in sources.get("accounts", []):
            if account.get("username", "").lower() == username_lower:
                return dict(account)
        return None

    def _format_noticias(self, noticias: list[dict]) -> tuple[str, str]:
        """Formatea las noticias en texto plano para el prompt.

        Separa los items de datos estructurados (source='datos_estructurados')
        de las noticias regulares. Los datos estructurados se devuelven como
        un bloque aparte para insertarlos antes de las noticias en el prompt.

        Si una noticia tiene un campo 'source' que coincide con un username
        en sources.json, se anota con el peso y afinidad de esa fuente.

        Args:
            noticias: Lista de dicts con source, title, content, timestamp.

        Returns:
            Tupla (datos_mercado, noticias_text):
            - datos_mercado: Texto con datos estructurados, o string vacio.
            - noticias_text: Texto formateado con las noticias numeradas.
        """
        if not noticias:
            return "", "(No hay noticias recientes disponibles)"

        # Separar datos estructurados de noticias regulares
        datos_mercado = ""
        noticias_regulares: list[dict] = []

        for item in noticias:
            if item.get("source") == "datos_estructurados":
                datos_mercado = item.get("content", "")
            else:
                noticias_regulares.append(item)

        # Construir lookup de fuentes por username (case-insensitive)
        sources = self._load_sources()
        source_lookup: dict[str, dict] = {}
        for account in sources.get("accounts", []):
            uname = account.get("username", "")
            if uname:
                source_lookup[uname.lower()] = account

        lines: list[str] = []
        for i, noticia in enumerate(noticias_regulares[:30], 1):  # Limitar a 30
            source = noticia.get("source", "desconocido")
            title = noticia.get("title", "Sin titulo")
            content = noticia.get("content", "")[:500]
            timestamp = noticia.get("timestamp", "")

            # Anotar con info de sources.json si hay match
            source_clean = source.lstrip("@")
            source_info = source_lookup.get(source_clean.lower())

            if source_info:
                peso = source_info.get("peso", 1.0)
                afinidad = source_info.get("afinidad_mercado", "neutral")
                source_label = (
                    f"[Fuente: @{source_clean}, peso={peso}, afinidad={afinidad}]"
                )
            else:
                source_label = f"[{source}]"

            lines.append(
                f"{i}. {source_label} ({timestamp})\n"
                f"   Titulo: {title}\n"
                f"   Contenido: {content}\n"
            )

        noticias_text = "\n".join(lines) if lines else "(No hay noticias recientes disponibles)"
        return datos_mercado, noticias_text

    def _parse_response(self, response_text: str) -> dict:
        """Parsea la respuesta del LLM como JSON.

        Intenta extraer JSON del texto de respuesta, manejando posibles
        bloques de codigo markdown.

        Args:
            response_text: Texto de respuesta del LLM.

        Returns:
            Dict con el contexto de mercado parseado.

        Raises:
            json.JSONDecodeError: Si el texto no es JSON valido.
        """
        text = response_text.strip()

        # Remover bloques de codigo markdown si los hay
        if text.startswith("```"):
            lines = text.split("\n")
            # Remover primera y ultima linea (``` markers)
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            text = text.strip()

        return json.loads(text)

    def analyze(self, noticias: list[dict]) -> dict:
        """Analiza las noticias recolectadas y produce un contexto de mercado.

        Envia las noticias al modelo Claude configurado y parsea la
        respuesta JSON. Si la API falla, el JSON es invalido, o no hay
        noticias, retorna un contexto neutral por defecto.

        Args:
            noticias: Lista de noticias recolectadas por el collector.
                Cada item debe tener: source, title, content, timestamp.

        Returns:
            Dict con el contexto de mercado estructurado:
            - riesgo_macro: str
            - sentimiento: float
            - eventos_activos: list
            - estrategias_pausadas: list
            - sizing_multiplier: float
            - resumen: str
        """
        now = datetime.now(timezone.utc).isoformat()

        if not noticias:
            logger.info("analyzer_sin_noticias_usando_default")
            context = dict(DEFAULT_CONTEXT)
            context["timestamp"] = now
            return context

        datos_mercado, noticias_text = self._format_noticias(noticias)
        # Si hay datos estructurados, insertarlos como seccion DATOS DE MERCADO
        datos_mercado_section = ""
        if datos_mercado:
            datos_mercado_section = (
                "DATOS DE MERCADO (numeros duros, usar como referencia):\n"
                f"{datos_mercado}\n\n"
            )
        user_prompt = USER_PROMPT_TEMPLATE.format(
            datos_mercado=datos_mercado_section,
            noticias=noticias_text,
        )
        source_context = self._build_source_context()
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(source_context=source_context)

        try:
            client = self._get_client()
            message = client.messages.create(
                model=self._model,
                max_tokens=2000,
                system=system_prompt,
                messages=[
                    {"role": "user", "content": user_prompt},
                ],
            )

            response_text = message.content[0].text
            context = self._parse_response(response_text)

            # Agregar timestamp
            context["timestamp"] = now

            # Validar campos minimos
            if "riesgo_macro" not in context:
                context["riesgo_macro"] = "medio"
            if "sizing_multiplier" not in context:
                context["sizing_multiplier"] = 1.0
            if "estrategias_pausadas" not in context:
                context["estrategias_pausadas"] = []
            if "sentimiento" not in context:
                context["sentimiento"] = 0.0

            logger.info(
                "analisis_completado",
                riesgo=context["riesgo_macro"],
                sentimiento=context.get("sentimiento"),
                sizing=context["sizing_multiplier"],
                eventos=len(context.get("eventos_activos", [])),
            )
            return context

        except json.JSONDecodeError:
            logger.exception("analyzer_json_invalido")
        except ValueError:
            logger.exception("analyzer_config_error")
        except Exception:
            logger.exception("analyzer_error_inesperado")

        # Fallback a contexto neutral
        logger.warning("analyzer_usando_contexto_default")
        context = dict(DEFAULT_CONTEXT)
        context["timestamp"] = now
        return context
