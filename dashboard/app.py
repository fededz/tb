"""Dashboard web FastAPI para el sistema de trading algoritmico.

Provee una interfaz web para monitorear posiciones, P&L, estrategias,
perfil de riesgo y contexto de mercado del research agent.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
from pydantic import BaseModel

import config
from db.repository import Repository

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Trading Dashboard", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

_start_time = time.time()

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

security = HTTPBasic(auto_error=False)


def verify_auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    """Verifica HTTP Basic Auth. Si DASHBOARD_PASSWORD esta vacio, omite auth."""
    if not config.DASHBOARD_PASSWORD:
        return
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales requeridas",
            headers={"WWW-Authenticate": "Basic"},
        )
    correct_user = secrets.compare_digest(credentials.username, config.DASHBOARD_USER)
    correct_pass = secrets.compare_digest(credentials.password, config.DASHBOARD_PASSWORD)
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales invalidas",
            headers={"WWW-Authenticate": "Basic"},
        )


# ---------------------------------------------------------------------------
# Repository dependency
# ---------------------------------------------------------------------------

_repo: Repository | None = None


def get_repo() -> Repository:
    """Obtiene la instancia singleton del repositorio."""
    global _repo
    if _repo is None:
        _repo = Repository()
    return _repo


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class PerfilCambioRequest(BaseModel):
    """Request para cambiar perfil de riesgo."""
    nombre: str
    benchmark_pct: float | None = None
    carry_pct: float | None = None
    tendencia_pct: float | None = None
    relative_value_pct: float | None = None
    spread_minimo_benchmark: float | None = None
    max_drawdown_diario_pct: float | None = None


class EstrategiaPausaRequest(BaseModel):
    """Request body opcional para pausar/reactivar."""
    motivo: str | None = None


# ---------------------------------------------------------------------------
# Strategy definitions (static metadata)
# ---------------------------------------------------------------------------

ESTRATEGIAS_META: dict[str, dict[str, Any]] = {
    "carry_futuros": {
        "nombre_display": "Carry Futuros Dolar",
        "bloque": "carry",
        "frecuencia": "diaria",
    },
    "carry_bonos": {
        "nombre_display": "Carry Bonos CER",
        "bloque": "carry",
        "frecuencia": "diaria",
    },
    "trend_following": {
        "nombre_display": "Trend Following Futuros",
        "bloque": "tendencia",
        "frecuencia": "diaria",
    },
    "momentum_acciones": {
        "nombre_display": "Momentum Acciones Merval",
        "bloque": "tendencia",
        "frecuencia": "semanal",
    },
    "pares": {
        "nombre_display": "Spread / Pares",
        "bloque": "relative_value",
        "frecuencia": "diaria",
    },
    "mean_reversion": {
        "nombre_display": "Mean Reversion Intraday",
        "bloque": "relative_value",
        "frecuencia": "intraday",
    },
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convierte campos date/datetime a string para JSON."""
    result = {}
    for k, v in row.items():
        if isinstance(v, (date, datetime)):
            result[k] = v.isoformat()
        else:
            result[k] = v
    return result


def _get_pnl_period_dates(periodo: str) -> tuple[date, date]:
    """Retorna (desde, hasta) para un periodo dado."""
    hoy = date.today()
    if periodo == "dia":
        return hoy, hoy
    elif periodo == "semana":
        lunes = hoy - timedelta(days=hoy.weekday())
        return lunes, hoy
    elif periodo == "mes":
        return hoy.replace(day=1), hoy
    elif periodo == "anio":
        return hoy.replace(month=1, day=1), hoy
    else:
        return hoy, hoy


# ===================================================================
# API Endpoints
# ===================================================================

# ---------------------------------------------------------------------------
# Perfil de riesgo
# ---------------------------------------------------------------------------

@app.get("/api/perfil/actual")
def api_perfil_actual(
    _auth: None = Depends(verify_auth),
    repo: Repository = Depends(get_repo),
) -> JSONResponse:
    """Retorna el perfil de riesgo activo."""
    perfil = repo.get_active_risk_profile()
    if perfil is None:
        return JSONResponse(
            content={"perfil": None, "presets": config.PERFILES},
            status_code=200,
        )
    return JSONResponse(content={
        "perfil": _serialize_row(perfil),
        "presets": config.PERFILES,
    })


@app.post("/api/perfil/cambiar")
def api_perfil_cambiar(
    body: PerfilCambioRequest,
    _auth: None = Depends(verify_auth),
    repo: Repository = Depends(get_repo),
) -> JSONResponse:
    """Cambia el perfil de riesgo activo."""
    result = repo.set_active_risk_profile(body.nombre, updated_by="dashboard")
    if result is None:
        raise HTTPException(status_code=404, detail=f"Perfil '{body.nombre}' no encontrado")
    logger.info("perfil_cambiado_desde_dashboard", nombre=body.nombre)
    return JSONResponse(content={"ok": True, "perfil": _serialize_row(result)})


@app.get("/api/perfil/historial")
def api_perfil_historial(
    _auth: None = Depends(verify_auth),
    repo: Repository = Depends(get_repo),
) -> JSONResponse:
    """Retorna todos los perfiles de riesgo."""
    perfiles = repo.get_all_risk_profiles()
    return JSONResponse(content=[_serialize_row(p) for p in perfiles])


# ---------------------------------------------------------------------------
# Estado del sistema
# ---------------------------------------------------------------------------

@app.get("/api/estado")
def api_estado(
    _auth: None = Depends(verify_auth),
    repo: Repository = Depends(get_repo),
) -> JSONResponse:
    """Retorna estado general del sistema."""
    try:
        posiciones = repo.get_posiciones_abiertas()
        ordenes_activas = repo.get_active_orders()
        contexto = repo.get_latest_market_context()
    except Exception as exc:
        logger.error("error_obteniendo_estado", error=str(exc))
        posiciones = []
        ordenes_activas = []
        contexto = None

    uptime_seconds = int(time.time() - _start_time)
    estrategias_pausadas: list[str] = []
    if contexto and contexto.get("estrategias_pausadas"):
        ep = contexto["estrategias_pausadas"]
        if isinstance(ep, str):
            try:
                estrategias_pausadas = json.loads(ep)
            except (json.JSONDecodeError, TypeError):
                estrategias_pausadas = []
        elif isinstance(ep, list):
            estrategias_pausadas = ep

    estrategias_activas = len(ESTRATEGIAS_META) - len(estrategias_pausadas)

    return JSONResponse(content={
        "sandbox": config.PPI_SANDBOX,
        "uptime_seconds": uptime_seconds,
        "estrategias_activas": estrategias_activas,
        "estrategias_total": len(ESTRATEGIAS_META),
        "posiciones_abiertas": len(posiciones),
        "ordenes_pendientes": len(ordenes_activas),
        "estrategias_pausadas": estrategias_pausadas,
    })


# ---------------------------------------------------------------------------
# P&L
# ---------------------------------------------------------------------------

@app.get("/api/pnl")
def api_pnl(
    periodo: str = Query("dia", pattern="^(dia|semana|mes|anio)$"),
    _auth: None = Depends(verify_auth),
    repo: Repository = Depends(get_repo),
) -> JSONResponse:
    """Retorna datos de P&L para el periodo solicitado."""
    desde, hasta = _get_pnl_period_dates(periodo)
    registros = repo.get_pnl_range(desde, hasta)

    pnl_ars_total = sum(r.get("pnl_ars", 0) or 0 for r in registros)
    pnl_usd_total = sum(r.get("pnl_usd", 0) or 0 for r in registros)
    trades_total = sum(r.get("trades", 0) or 0 for r in registros)

    capital_inicio = registros[0].get("capital_inicio", 0) if registros else 0
    capital_fin = registros[-1].get("capital_fin", 0) if registros else 0
    capital_inicio = capital_inicio or 0
    capital_fin = capital_fin or 0

    pnl_pct = (pnl_ars_total / capital_inicio * 100) if capital_inicio else 0
    pnl_real = pnl_ars_total - (capital_inicio * config.INFLACION_ANUAL_ESTIMADA / 365 * len(registros))

    return JSONResponse(content={
        "periodo": periodo,
        "desde": desde.isoformat(),
        "hasta": hasta.isoformat(),
        "pnl_ars": round(pnl_ars_total, 2),
        "pnl_usd": round(pnl_usd_total, 2),
        "pnl_pct": round(pnl_pct, 4),
        "pnl_real": round(pnl_real, 2),
        "capital_inicio": round(capital_inicio, 2),
        "capital_fin": round(capital_fin, 2),
        "trades": trades_total,
        "registros": [_serialize_row(r) for r in registros],
    })


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------

@app.get("/api/benchmark/actual")
def api_benchmark_actual(
    _auth: None = Depends(verify_auth),
) -> JSONResponse:
    """Retorna la tasa benchmark configurada."""
    return JSONResponse(content={
        "instrumento": config.BENCHMARK_INSTRUMENTO,
        "inflacion_anual_estimada": config.INFLACION_ANUAL_ESTIMADA,
    })


# ---------------------------------------------------------------------------
# Estrategias
# ---------------------------------------------------------------------------

@app.get("/api/estrategias")
def api_estrategias(
    _auth: None = Depends(verify_auth),
    repo: Repository = Depends(get_repo),
) -> JSONResponse:
    """Lista todas las estrategias con su estado y P&L."""
    contexto = repo.get_latest_market_context()
    pausadas: list[str] = []
    if contexto and contexto.get("estrategias_pausadas"):
        ep = contexto["estrategias_pausadas"]
        if isinstance(ep, str):
            try:
                pausadas = json.loads(ep)
            except (json.JSONDecodeError, TypeError):
                pausadas = []
        elif isinstance(ep, list):
            pausadas = ep

    hoy = date.today()
    resultado: list[dict[str, Any]] = []

    for nombre, meta in ESTRATEGIAS_META.items():
        ordenes_hoy = repo.get_ordenes_filtradas(strategy=nombre, desde=hoy, hasta=hoy)
        pnl_dia = sum(
            (o.get("precio", 0) or 0) * (o.get("cantidad", 0) or 0)
            * (1 if o.get("operacion") == "VENTA" else -1)
            for o in ordenes_hoy
            if o.get("status") == "EXECUTED"
        )

        if nombre in pausadas:
            estado = "pausada"
        else:
            estado = "activa"

        resultado.append({
            "nombre": nombre,
            "nombre_display": meta["nombre_display"],
            "bloque": meta["bloque"],
            "frecuencia": meta["frecuencia"],
            "estado": estado,
            "pnl_dia": round(pnl_dia, 2),
            "ordenes_hoy": len(ordenes_hoy),
        })

    return JSONResponse(content=resultado)


@app.post("/api/estrategias/{nombre}/pausar")
def api_estrategia_pausar(
    nombre: str,
    _auth: None = Depends(verify_auth),
    repo: Repository = Depends(get_repo),
) -> JSONResponse:
    """Pausa una estrategia agregandola a estrategias_pausadas en market_context."""
    if nombre not in ESTRATEGIAS_META:
        raise HTTPException(status_code=404, detail=f"Estrategia '{nombre}' no existe")

    contexto = repo.get_latest_market_context()
    pausadas: list[str] = []
    riesgo_macro = "medio"
    sentimiento = 0.0
    sizing_mult = 1.0
    resumen = None

    if contexto:
        ep = contexto.get("estrategias_pausadas")
        if isinstance(ep, str):
            try:
                pausadas = json.loads(ep)
            except (json.JSONDecodeError, TypeError):
                pausadas = []
        elif isinstance(ep, list):
            pausadas = list(ep)
        riesgo_macro = contexto.get("riesgo_macro", "medio")
        sentimiento = contexto.get("sentimiento", 0.0)
        sizing_mult = contexto.get("sizing_mult", 1.0)
        resumen = contexto.get("resumen")

    if nombre not in pausadas:
        pausadas.append(nombre)

    repo.insert_market_context(
        timestamp=datetime.now(),
        riesgo_macro=riesgo_macro,
        sentimiento=sentimiento,
        sizing_mult=sizing_mult,
        eventos=contexto.get("eventos") if contexto else None,
        estrategias_pausadas=pausadas,
        resumen=resumen,
        fuentes_count=contexto.get("fuentes_count") if contexto else None,
    )
    logger.info("estrategia_pausada_dashboard", nombre=nombre)
    return JSONResponse(content={"ok": True, "pausadas": pausadas})


@app.post("/api/estrategias/{nombre}/reactivar")
def api_estrategia_reactivar(
    nombre: str,
    _auth: None = Depends(verify_auth),
    repo: Repository = Depends(get_repo),
) -> JSONResponse:
    """Reactiva una estrategia removiendola de estrategias_pausadas."""
    if nombre not in ESTRATEGIAS_META:
        raise HTTPException(status_code=404, detail=f"Estrategia '{nombre}' no existe")

    contexto = repo.get_latest_market_context()
    pausadas: list[str] = []
    riesgo_macro = "medio"
    sentimiento = 0.0
    sizing_mult = 1.0
    resumen = None

    if contexto:
        ep = contexto.get("estrategias_pausadas")
        if isinstance(ep, str):
            try:
                pausadas = json.loads(ep)
            except (json.JSONDecodeError, TypeError):
                pausadas = []
        elif isinstance(ep, list):
            pausadas = list(ep)
        riesgo_macro = contexto.get("riesgo_macro", "medio")
        sentimiento = contexto.get("sentimiento", 0.0)
        sizing_mult = contexto.get("sizing_mult", 1.0)
        resumen = contexto.get("resumen")

    if nombre in pausadas:
        pausadas.remove(nombre)

    repo.insert_market_context(
        timestamp=datetime.now(),
        riesgo_macro=riesgo_macro,
        sentimiento=sentimiento,
        sizing_mult=sizing_mult,
        eventos=contexto.get("eventos") if contexto else None,
        estrategias_pausadas=pausadas,
        resumen=resumen,
        fuentes_count=contexto.get("fuentes_count") if contexto else None,
    )
    logger.info("estrategia_reactivada_dashboard", nombre=nombre)
    return JSONResponse(content={"ok": True, "pausadas": pausadas})


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

@app.get("/api/research/contexto")
def api_research_contexto(
    _auth: None = Depends(verify_auth),
    repo: Repository = Depends(get_repo),
) -> JSONResponse:
    """Retorna el ultimo contexto de mercado."""
    contexto = repo.get_latest_market_context()
    if contexto is None:
        return JSONResponse(content={"contexto": None})
    return JSONResponse(content={"contexto": _serialize_row(contexto)})


@app.get("/api/research/noticias")
def api_research_noticias(
    limit: int = Query(20, ge=1, le=100),
    _auth: None = Depends(verify_auth),
    repo: Repository = Depends(get_repo),
) -> JSONResponse:
    """Retorna las ultimas N entradas de contexto de mercado."""
    query = """
        SELECT * FROM market_context
        ORDER BY timestamp DESC
        LIMIT %(limit)s
    """
    try:
        registros = repo._execute(query, {"limit": limit}, fetch_all=True) or []
    except Exception:
        registros = []
    return JSONResponse(content=[_serialize_row(r) for r in registros])


# ---------------------------------------------------------------------------
# Ordenes
# ---------------------------------------------------------------------------

@app.get("/api/ordenes")
def api_ordenes(
    estrategia: str | None = Query(None),
    ticker: str | None = Query(None),
    desde: str | None = Query(None),
    hasta: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    _auth: None = Depends(verify_auth),
    repo: Repository = Depends(get_repo),
) -> JSONResponse:
    """Retorna ordenes filtradas."""
    desde_date = date.fromisoformat(desde) if desde else None
    hasta_date = date.fromisoformat(hasta) if hasta else None

    ordenes = repo.get_ordenes_filtradas(
        strategy=estrategia,
        ticker=ticker,
        desde=desde_date,
        hasta=hasta_date,
        status=status_filter,
    )
    return JSONResponse(content=[_serialize_row(o) for o in ordenes])


# ===================================================================
# HTML Pages
# ===================================================================

@app.get("/", response_class=HTMLResponse)
def page_index(request: Request, _auth: None = Depends(verify_auth)) -> HTMLResponse:
    """Pagina principal del dashboard."""
    return templates.TemplateResponse(
        request, "index.html",
        context={"sandbox": config.PPI_SANDBOX, "page": "index"},
    )


@app.get("/cartera", response_class=HTMLResponse)
def page_cartera(request: Request, _auth: None = Depends(verify_auth)) -> HTMLResponse:
    """Pagina de cartera / posiciones."""
    return templates.TemplateResponse(
        request, "cartera.html",
        context={"sandbox": config.PPI_SANDBOX, "page": "cartera"},
    )


@app.get("/trades", response_class=HTMLResponse)
def page_trades(request: Request, _auth: None = Depends(verify_auth)) -> HTMLResponse:
    """Pagina de historial de trades."""
    return templates.TemplateResponse(
        request, "trades.html",
        context={"sandbox": config.PPI_SANDBOX, "page": "trades"},
    )


@app.get("/estrategias", response_class=HTMLResponse)
def page_estrategias(request: Request, _auth: None = Depends(verify_auth)) -> HTMLResponse:
    """Pagina de gestion de estrategias."""
    return templates.TemplateResponse(
        request, "estrategias.html",
        context={"sandbox": config.PPI_SANDBOX, "page": "estrategias"},
    )


@app.get("/perfil", response_class=HTMLResponse)
def page_perfil(request: Request, _auth: None = Depends(verify_auth)) -> HTMLResponse:
    """Pagina de gestion de perfil de riesgo."""
    return templates.TemplateResponse(
        request, "perfil.html",
        context={"sandbox": config.PPI_SANDBOX, "page": "perfil"},
    )


@app.get("/research", response_class=HTMLResponse)
def page_research(request: Request, _auth: None = Depends(verify_auth)) -> HTMLResponse:
    """Pagina del research agent."""
    return templates.TemplateResponse(
        request, "research.html",
        context={"sandbox": config.PPI_SANDBOX, "page": "research"},
    )
