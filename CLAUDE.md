# CLAUDE.md — Sistema de Trading Algorítmico con PPI

## Contexto del proyecto

Sistema de trading algorítmico para mercados argentinos (BYMA y ROFEX) usando la API de PPI Inversiones.
Desarrollado por un ingeniero en sistemas con conocimiento del mercado local.
El sistema corre en un VPS 24/7, sin intervención manual en la ejecución de órdenes.

---

## Stack técnico

- **Python 3.11+**
- **ppi_client** — SDK oficial de PPI para market data y órdenes
- **PostgreSQL** — estado del sistema, posiciones, historial de órdenes, P&L
- **APScheduler** — scheduling de estrategias (cron-like)
- **asyncio** — manejo de WebSocket y concurrencia
- **pandas / numpy** — cálculo de señales e indicadores
- **python-telegram-bot** — alertas en tiempo real
- **Docker + docker-compose** — deployment en VPS
- **pytest** — tests unitarios e integración

---

## Estructura del proyecto

```
/
├── CLAUDE.md                  # Este archivo
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env                       # Credenciales (nunca commitear)
├── .env.example
│
├── core/
│   ├── __init__.py
│   ├── ppi_wrapper.py         # Wrapper sobre ppi_client con reconexión automática
│   ├── order_manager.py       # Manejo del flujo budget → confirm + disclaimers
│   ├── portfolio.py           # Estado de posiciones y capital disponible
│   ├── risk_manager.py        # Validaciones de riesgo antes de cada orden
│   └── alertas.py             # Telegram bot para notificaciones
│
├── market_data/
│   ├── __init__.py
│   ├── realtime.py            # WebSocket handler con reconexión
│   ├── historical.py          # Descarga y cache de datos históricos
│   └── cache.py               # Cache en memoria de precios actuales
│
├── strategies/
│   ├── __init__.py
│   ├── base.py                # Clase base Strategy
│   ├── carry_bonos.py         # Carry trade en bonos
│   ├── carry_futuros.py       # Base dólar spot vs futuro ROFEX
│   ├── trend_following.py     # Trend following en futuros
│   ├── momentum_acciones.py   # Momentum semanal en acciones Merval
│   ├── pares.py               # Spread trading entre activos correlacionados
│   └── mean_reversion.py      # Mean reversion intraday en futuros
│
├── scheduler/
│   ├── __init__.py
│   └── jobs.py                # Definición de jobs por frecuencia
│
├── db/
│   ├── __init__.py
│   ├── models.py              # Modelos de tablas
│   ├── migrations/            # SQL de migraciones
│   └── repository.py         # Queries de lectura/escritura
│
├── monitoring/
│   ├── __init__.py
│   └── heartbeat.py           # Heartbeat cada N minutos a Telegram
│
└── tests/
    ├── test_order_manager.py
    ├── test_risk_manager.py
    └── test_strategies/
```

---

## Variables de entorno (.env)

```env
# PPI API
PPI_PUBLIC_KEY=
PPI_PRIVATE_KEY=
PPI_ACCOUNT_NUMBER=
PPI_SANDBOX=true           # SIEMPRE true hasta autorización explícita para live

# PostgreSQL
DB_HOST=localhost
DB_PORT=5432
DB_NAME=trading
DB_USER=trading
DB_PASSWORD=

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Risk
MAX_CAPITAL_POR_OPERACION_PCT=0.05   # 5% del capital total por operación
MAX_POSICIONES_ABIERTAS=10

# Perfil de riesgo inicial (conservador | moderado | agresivo)
# Cambiable desde el dashboard sin reiniciar
PERFIL_INICIAL=moderado

# Benchmark e inflación
BENCHMARK_INSTRUMENTO=LECAP          # "LECAP" | "UVA" — contra qué comparás
INFLACION_ANUAL_ESTIMADA=0.35        # Fallback si INDEC no responde, se actualiza automáticamente
```

---

## Regla crítica de seguridad

**PPI_SANDBOX debe ser `true` en todo momento durante desarrollo y paper trading.**
Antes de cambiar a `false` para live trading, se requiere:
1. Mínimo 30 días de paper trading con resultados positivos
2. Revisión manual del risk_manager
3. Cambio explícito y consciente de la variable

Si PPI_SANDBOX es `false`, el sistema debe logear una advertencia prominente al arrancar:
```
⚠️  MODO LIVE ACTIVO — ÓRDENES REALES EN CURSO
```

---

## Core: ppi_wrapper.py

Wrapper sobre `ppi_client.ppi.PPI` que maneja:

- Login automático con las credenciales del .env
- Reconexión automática del WebSocket si se cae (`ondisconnect` → reconnect con backoff exponencial)
- Re-suscripción automática a todos los instrumentos activos tras reconexión
- Logging de cada llamada a la API con timestamp y resultado
- Rate limiting para no saturar la API

```python
# Interfaz esperada
class PPIWrapper:
    def connect(self) -> None
    def subscribe_instrument(self, ticker: str, tipo: str, plazo: str) -> None
    def get_current_price(self, ticker: str, tipo: str, plazo: str) -> float
    def get_historical(self, ticker: str, tipo: str, plazo: str, desde: date, hasta: date) -> pd.DataFrame
    def get_book(self, ticker: str, tipo: str, plazo: str) -> dict
    def get_balance(self) -> dict
```

---

## Core: order_manager.py

Maneja el flujo de dos pasos de PPI:

1. `budget()` → obtiene costo estimado y lista de disclaimers
2. Acepta automáticamente todos los disclaimers
3. `confirm()` → envía la orden real

Debe ser **idempotente**: si el sistema se reinicia entre budget y confirm, no debe reenviar la orden. Usar la tabla `orders` en DB para trackear estado.

```python
# Interfaz esperada
class OrderManager:
    def send_order(
        self,
        ticker: str,
        tipo: str,
        operacion: str,          # "COMPRA" | "VENTA"
        cantidad: float,
        precio: float | None,    # None = precio de mercado
        plazo: str,
        dry_run: bool = False    # Si True, solo loguea sin enviar
    ) -> dict

    def cancel_order(self, order_id: int) -> dict
    def cancel_all(self) -> None
    def get_active_orders(self) -> list
```

---

## Core: risk_manager.py

**Cada orden pasa por el risk_manager antes de ejecutarse.** Si falla cualquier validación, la orden se rechaza y se alerta por Telegram.

Validaciones:
- Capital disponible suficiente
- No superar `MAX_CAPITAL_POR_OPERACION_PCT`
- No superar `MAX_POSICIONES_ABIERTAS`
- Drawdown diario no superó `MAX_DRAWDOWN_DIARIO_PCT`
- Horario de mercado válido (no enviar órdenes fuera de horario)
- El instrumento está dentro del universo permitido por la estrategia

```python
class RiskManager:
    def validate(self, order: OrderIntent) -> tuple[bool, str]
    def check_drawdown_diario(self) -> bool
    def get_capital_disponible(self) -> float
    def get_posiciones_abiertas(self) -> int
```

---

## Core: portfolio.py

Estado en tiempo real del portafolio. Se actualiza con cada notificación de orden ejecutada vía WebSocket.

```python
class Portfolio:
    def get_posiciones(self) -> dict[str, Posicion]
    def get_pnl_diario(self) -> float
    def get_pnl_total(self) -> float
    def update_from_execution(self, order_notification: dict) -> None
    def get_capital_total(self) -> float
```

---

## Estrategias

### Clase base (strategies/base.py)

```python
class Strategy:
    name: str
    frecuencia: str          # "intraday" | "diaria" | "semanal" | "mensual"
    instrumentos: list[str]

    def should_run(self) -> bool          # Verifica horario y condiciones
    def generate_signals(self) -> list[Signal]
    def run(self) -> None                 # Genera señales → valida riesgo → ejecuta
```

---

### 1. Carry en Futuros de Dólar (carry_futuros.py)

**Lógica:**
- Obtener precio del dólar MEP (AL30 en pesos / AL30D en dólares)
- Obtener precio de futuros de dólar en ROFEX (DLR próximo vencimiento)
- Calcular tasa implícita anualizada = `(precio_futuro / precio_spot - 1) * (365 / días_al_vencimiento)`
- Si tasa_implícita > umbral_minimo (configurable, default 40% anual): señal de COMPRA futuro
- Si tasa_implícita < umbral_salida: señal de VENTA / cierre de posición

**Frecuencia:** Diaria, al cierre del mercado  
**Instrumentos:** AL30, AL30D, DLR/MMAA (futuros ROFEX)  
**Plazo:** INMEDIATA para futuros  
**Parámetros configurables:**
```python
UMBRAL_ENTRADA_TASA = 0.40    # 40% anual
UMBRAL_SALIDA_TASA = 0.25     # 25% anual
MAX_DIAS_VENCIMIENTO = 90     # No tomar futuros a más de 90 días
```

---

### 2. Carry en Bonos (carry_bonos.py)

**Lógica:**
- Calcular TIR de bonos CER cortos (usando `estimate_bonds` de PPI)
- Calcular costo de fondeo implícito (tasa del mercado de cauciones)
- Si TIR_bono > tasa_fondeo + spread_minimo: señal de COMPRA
- Monitorear variación de tipo de cambio para stop loss

**Frecuencia:** Diaria  
**Instrumentos:** Lecaps, bonos CER cortos (TX24, TX26, etc.)  
**Parámetros configurables:**
```python
SPREAD_MINIMO = 0.05          # 5% sobre tasa de fondeo
MAX_DURATION = 2.0            # Duration modificada máxima en años
STOP_LOSS_FX_PCT = 0.05       # Salir si dólar sube más de 5% en el día
```

---

### 3. Trend Following en Futuros (trend_following.py)

**Lógica:**
- Calcular media móvil de 20 y 50 ruedas sobre precio de cierre
- Si MA20 cruza por encima de MA50: señal LONG
- Si MA20 cruza por debajo de MA50: señal SHORT o cierre
- Confirmar con ATR para filtrar señales en mercados laterales

**Frecuencia:** Diaria, al cierre  
**Instrumentos:** DLR (dólar futuro), RFX20 (índice Merval futuro), SOJ (soja)  
**Parámetros configurables:**
```python
MA_RAPIDA = 20
MA_LENTA = 50
ATR_PERIODO = 14
ATR_MULTIPLICADOR = 1.5       # Stop loss dinámico = precio ± 1.5 * ATR
```

---

### 4. Momentum en Acciones (momentum_acciones.py)

**Lógica:**
- Universo: las 20 acciones más líquidas del Merval (lista configurable)
- Cada lunes calcular retorno de las últimas 4 semanas para cada acción
- Rankear de mayor a menor retorno
- Comprar las top N acciones (default 5), evitar / vender las bottom N
- Rebalancear semanalmente

**Frecuencia:** Semanal (lunes al abrir)  
**Instrumentos:** GGAL, BBAR, BMA, YPF, PAMP, TXAR, ALUA, CRES, SUPV, TECO2, COME, BYMA, CVH, EDN, GGAL, HARG, LOMA, MIRG, TRAN, VALO  
**Parámetros configurables:**
```python
LOOKBACK_SEMANAS = 4
TOP_N = 5
BOTTOM_N = 5
PLAZO = "A-48HS"
```

---

### 5. Pares / Spread Trading (pares.py)

**Lógica:**
- Pares definidos con correlación histórica alta (>0.85): GGAL/BMA, PAMP/TRAN, etc.
- Calcular spread z-score = `(spread_actual - media_spread) / std_spread` usando ventana de 60 ruedas
- Si z-score > 2: el activo A está caro vs B → vender A, comprar B
- Si z-score < -2: el activo A está barato vs B → comprar A, vender B
- Cerrar cuando z-score vuelve a 0

**Frecuencia:** Diaria  
**Pares iniciales:**
```python
PARES = [
    ("GGAL", "BMA"),      # Bancos
    ("PAMP", "TRAN"),     # Utilities
    ("GGAL", "SUPV"),     # Bancos
]
ZSCORE_ENTRADA = 2.0
ZSCORE_SALIDA = 0.5
LOOKBACK_RUEDAS = 60
```

---

### 6. Mean Reversion Intraday (mean_reversion.py)

**Lógica:**
- Calcular VWAP del día para futuros de dólar
- Si precio actual se desvía más de X% del VWAP: señal de retorno
- Señal LONG si precio < VWAP * (1 - umbral)
- Señal SHORT si precio > VWAP * (1 + umbral)
- Cierre obligatorio 15 minutos antes del cierre del mercado

**Frecuencia:** Intraday (cada 5 minutos durante horario de mercado)  
**Instrumentos:** DLR próximo vencimiento  
**Parámetros configurables:**
```python
UMBRAL_DESVIACION = 0.003    # 0.3% del VWAP
CIERRE_ANTICIPADO_MIN = 15   # Cerrar posiciones 15 min antes del cierre
MAX_POSICION_INTRADAY = 3    # Máximo 3 contratos simultáneos
```

---

## Scheduler (scheduler/jobs.py)

```python
# Jobs definidos con APScheduler
JOBS = [
    # Intraday — solo en horario de mercado (10:00 - 17:00 hora Argentina)
    {"strategy": "mean_reversion",    "cron": "*/5 10-16 * * 1-5"},

    # Diaria — al cierre del mercado
    {"strategy": "carry_futuros",     "cron": "30 17 * * 1-5"},
    {"strategy": "carry_bonos",       "cron": "35 17 * * 1-5"},
    {"strategy": "trend_following",   "cron": "40 17 * * 1-5"},
    {"strategy": "pares",             "cron": "45 17 * * 1-5"},

    # Semanal — lunes al abrir
    {"strategy": "momentum_acciones", "cron": "10 10 * * 1"},

    # Heartbeat — cada 30 minutos
    {"job": "heartbeat",              "cron": "*/30 * * * *"},
]
```

---

## Base de datos (PostgreSQL)

### Tabla: orders
```sql
CREATE TABLE orders (
    id              SERIAL PRIMARY KEY,
    external_id     VARCHAR(100),
    strategy        VARCHAR(50) NOT NULL,
    ticker          VARCHAR(20) NOT NULL,
    tipo            VARCHAR(30) NOT NULL,
    operacion       VARCHAR(10) NOT NULL,    -- COMPRA | VENTA
    cantidad        NUMERIC(18,4) NOT NULL,
    precio          NUMERIC(18,4),
    plazo           VARCHAR(20) NOT NULL,
    status          VARCHAR(20) NOT NULL,    -- PENDING | EXECUTED | CANCELLED | REJECTED
    dry_run         BOOLEAN DEFAULT false,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    executed_at     TIMESTAMPTZ,
    error_msg       TEXT
);
```

### Tabla: positions
```sql
CREATE TABLE positions (
    id              SERIAL PRIMARY KEY,
    ticker          VARCHAR(20) NOT NULL,
    tipo            VARCHAR(30) NOT NULL,
    cantidad        NUMERIC(18,4) NOT NULL,
    precio_entrada  NUMERIC(18,4) NOT NULL,
    strategy        VARCHAR(50) NOT NULL,
    opened_at       TIMESTAMPTZ DEFAULT NOW(),
    closed_at       TIMESTAMPTZ,
    pnl             NUMERIC(18,4)
);
```

### Tabla: pnl_diario
```sql
CREATE TABLE pnl_diario (
    fecha           DATE PRIMARY KEY,
    pnl_ars         NUMERIC(18,4),
    pnl_usd         NUMERIC(18,4),
    capital_inicio  NUMERIC(18,4),
    capital_fin     NUMERIC(18,4),
    trades          INTEGER
);
```

### Tabla: market_data_cache
```sql
CREATE TABLE market_data_cache (
    ticker          VARCHAR(20) NOT NULL,
    tipo            VARCHAR(30) NOT NULL,
    plazo           VARCHAR(20) NOT NULL,
    fecha           DATE NOT NULL,
    open            NUMERIC(18,4),
    high            NUMERIC(18,4),
    low             NUMERIC(18,4),
    close           NUMERIC(18,4),
    volume          NUMERIC(18,4),
    PRIMARY KEY (ticker, tipo, plazo, fecha)
);
```

---

## Alertas Telegram

El bot de Telegram debe enviar mensajes para:

| Evento | Prioridad |
|--------|-----------|
| Orden ejecutada | Alta — inmediata |
| Orden rechazada por risk manager | Alta — inmediata |
| Error de conexión con PPI | Alta — inmediata |
| Drawdown diario superado | Crítica — inmediata + bloqueo |
| Heartbeat (cada 30 min) | Baja — solo si falla |
| Resumen diario al cierre | Media — 18:00 |
| Nueva señal generada | Media — inmediata |

Formato de mensajes:
```
🟢 ORDEN EJECUTADA
Estrategia: carry_futuros
Ticker: DLR/JUN25
Op: COMPRA 10 contratos @ $1,245.50
P&L día: +$12,340 ARS
```

---

## Orden de implementación

### Fase 1 — Infraestructura base (Semana 1-2)
1. Setup del proyecto: estructura de carpetas, Docker, .env
2. `ppi_wrapper.py` con login, REST calls y WebSocket con reconexión
3. Base de datos: crear tablas, `repository.py`
4. `order_manager.py` con flujo budget → confirm → log en DB
5. `alertas.py` con Telegram bot básico
6. Tests de integración contra sandbox de PPI

### Fase 2 — Risk y Portfolio (Semana 2-3)
7. `portfolio.py` sincronizado con WebSocket de account data
8. `risk_manager.py` con todas las validaciones
9. Tests unitarios de risk manager con casos edge

### Fase 3 — Primera estrategia (Semana 3-4)
10. `carry_futuros.py` — la más simple y directa
11. `scheduler/jobs.py` con APScheduler
12. Paper trading en sandbox durante 2 semanas mínimo
13. Validar P&L calculado vs posiciones reales

### Fase 4 — Estrategias adicionales (Mes 2)
14. `momentum_acciones.py`
15. `carry_bonos.py`
16. `trend_following.py`
17. `pares.py`

### Fase 5 — Estrategia intraday (Mes 3)
18. `mean_reversion.py` — la más compleja, requiere WebSocket estable probado

### Fase 6 — Live trading
19. Revisión completa del risk manager
20. Cambiar PPI_SANDBOX=false
21. Empezar con capital mínimo (10-20% del capital real)
22. Escalar gradualmente según resultados

---

## Convenciones de código

- **Type hints** en todas las funciones
- **Logging** con `structlog` o `logging` estándar — cada acción importante debe loguearse
- **Nunca** usar `print()` en producción — solo logging
- **Nunca** hardcodear precios, umbrales o parámetros — siempre desde config o .env
- **Manejo de excepciones** explícito en toda llamada a la API — nunca dejar que un error de red mate el proceso
- **Docstrings** en cada clase y método público
- Cada estrategia debe ser **testeable de forma aislada** con datos mockeados

---

## Contexto de mercado argentino

- Horario BYMA: 11:00 - 17:00 hora Argentina (GMT-3)
- Horario ROFEX: 10:00 - 17:00 hora Argentina
- Plazos de liquidación: INMEDIATA (mismo día), A-24HS, A-48HS, A-72HS
- La mayoría de acciones operan en A-48HS
- Los futuros de ROFEX operan en INMEDIATA
- Los bonos dolarizados (AL30D) operan en INMEDIATA en dólares
- El tipo de cambio implícito (CCL) = precio_ARS / precio_USD del mismo bono
- Feriados: consultar `ppi.configuration.get_holidays()` al inicio de cada jornada

---

## Gestión de capital y perfiles de riesgo

### Filosofía general

El capital nunca está 100% en estrategias algorítmicas. Siempre hay una porción en el **benchmark** (Lecap o UVA), que actúa como piso de retorno y garantiza que el sistema nunca pierda contra la inflación. Solo el excedente va a las estrategias.

El sistema reporta P&L en tres versiones siempre:
- **Nominal en pesos** — el número bruto
- **Real ajustado por inflación** — descontando el CER del período
- **En dólares al CCL del día** — el más honesto para Argentina

### Perfiles predefinidos

```python
PERFILES = {
    "conservador": {
        "benchmark_pct": 0.60,       # 60% en Lecap/UVA
        "carry_pct": 0.25,           # 25% en carry (futuros + bonos)
        "tendencia_pct": 0.10,       # 10% en trend following + momentum
        "relative_value_pct": 0.05,  # 5% en pares + mean reversion
        "spread_minimo_benchmark": 0.15,  # Estrategias deben superar benchmark + 15%
        "max_drawdown_diario_pct": 0.01,  # 1% drawdown diario → parar
    },
    "moderado": {
        "benchmark_pct": 0.40,
        "carry_pct": 0.25,
        "tendencia_pct": 0.25,
        "relative_value_pct": 0.10,
        "spread_minimo_benchmark": 0.10,
        "max_drawdown_diario_pct": 0.03,
    },
    "agresivo": {
        "benchmark_pct": 0.20,
        "carry_pct": 0.25,
        "tendencia_pct": 0.30,
        "relative_value_pct": 0.25,
        "spread_minimo_benchmark": 0.05,
        "max_drawdown_diario_pct": 0.05,
    },
    "custom": {
        # Valores editables desde el dashboard
        # Se persisten en DB, no en .env
    }
}
```

### Distribución interna de cada bloque

Dentro de cada bloque, el capital se distribuye por **risk parity**: cada estrategia recibe capital inversamente proporcional a su volatilidad histórica de los últimos 60 días. Se recalcula cada lunes.

```
Bloque carry (ej: 25% del capital total)
├── carry_futuros → según volatilidad histórica
└── carry_bonos   → según volatilidad histórica

Bloque tendencia
├── trend_following → según volatilidad histórica
└── momentum_acciones → según volatilidad histórica

Bloque relative value
├── pares → según volatilidad histórica
└── mean_reversion → según volatilidad histórica
```

### Ajuste dinámico por contexto de mercado

El research agent puede modificar el sizing via `sizing_multiplier`:
- `riesgo_macro = "critico"` → solo benchmark, cero en estrategias
- `riesgo_macro = "alto"` → `sizing_multiplier = 0.5` (mitad del tamaño normal)
- `riesgo_macro = "medio"` → `sizing_multiplier = 0.75`
- `riesgo_macro = "bajo"` → `sizing_multiplier = 1.0`

### Benchmark como estrategia

Si una estrategia no supera `benchmark + spread_minimo_benchmark` en los últimos 30 días, su capital se redirige automáticamente al benchmark hasta que mejore. El dashboard muestra qué estrategias están activas vs derivadas al benchmark.

### Tabla en DB para perfil activo

```sql
CREATE TABLE risk_profile (
    id              SERIAL PRIMARY KEY,
    nombre          VARCHAR(20) NOT NULL,    -- conservador | moderado | agresivo | custom
    benchmark_pct   NUMERIC(5,4) NOT NULL,
    carry_pct       NUMERIC(5,4) NOT NULL,
    tendencia_pct   NUMERIC(5,4) NOT NULL,
    relative_value_pct NUMERIC(5,4) NOT NULL,
    spread_minimo_benchmark NUMERIC(5,4) NOT NULL,
    max_drawdown_diario_pct NUMERIC(5,4) NOT NULL,
    activo          BOOLEAN DEFAULT false,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_by      VARCHAR(50)              -- "dashboard" | "sistema"
);

-- Solo un perfil puede estar activo a la vez
CREATE UNIQUE INDEX idx_risk_profile_activo ON risk_profile(activo) WHERE activo = true;
```

---

## Dashboard web

Interface web simple para monitorear y controlar el sistema sin tocar código ni terminal.

### Stack del dashboard

- **FastAPI** — backend REST, misma base de código que el trading system
- **HTML + Alpine.js + TailwindCSS** — frontend minimalista, sin frameworks pesados
- **Chart.js** — gráficos de P&L y distribución de capital
- Un solo archivo `dashboard/app.py` + templates HTML
- Corre en el mismo VPS, puerto 8080, acceso via IP:8080 o dominio propio

### Estructura

```
dashboard/
├── app.py              # FastAPI app con todos los endpoints
├── templates/
│   ├── base.html
│   ├── index.html      # Vista principal
│   ├── perfil.html     # Gestión de perfil de riesgo
│   └── estrategias.html
└── static/
    └── main.js
```

### Pantallas del dashboard

**1. Resumen — Pantalla principal**

Fila 1 — métricas de capital (4 cards):
- Capital total en ARS
- P&L hoy nominal en ARS con % del día
- P&L hoy real ajustado por inflación
- P&L hoy en USD al CCL del día

Fila 2 — métricas de performance (4 cards):
- P&L del mes nominal con %
- P&L del año nominal con %
- Benchmark actual (tasa Lecap o UVA vigente, anualizada)
- Semáforo benchmark: verde (supera benchmark +5%), amarillo (supera menos de 5%), rojo (no supera)

Sección central izquierda — Distribución de capital:
- Barras horizontales por bloque (Benchmark, Carry, Tendencia, Relative Value)
- Cada barra muestra % y monto en ARS
- Debajo: nivel de riesgo macro del research agent con barra de progreso y sizing multiplier activo

Sección central derecha — Estrategias:
- Una fila por estrategia con nombre, P&L del mes y badge de estado
- Badge: `Activa` (verde) | `→ Benchmark` (amarillo) | `Pausada` (rojo)

Tabla inferior — Últimos trades:
- Columnas: Hora, Estrategia, Ticker, Operación, Cantidad, Precio, P&L, Estado
- Estado: `Ejecutada` | `Rechazada · Risk` con motivo
- Últimos 10 trades del día, expandible a historial completo

**2. Cartera**
- Posiciones abiertas agrupadas por estrategia
- Para cada posición: ticker, tipo, cantidad, precio entrada, precio actual, P&L latente en ARS y USD
- Valor total de la cartera en ARS y USD
- Gráfico de torta por tipo de instrumento (acciones, futuros, bonos, benchmark)

**3. Trades**
- Historial completo de órdenes con filtros: estrategia, ticker, fecha, estado
- Export a CSV
- Métricas de la selección filtrada: win rate, P&L total, trade promedio

**4. Estrategias**
- Estado detallado de cada estrategia
- Capital asignado actual y % del total
- P&L por estrategia (día, semana, mes) con sparkline
- Última señal generada con timestamp
- Botón pausar/reactivar manualmente
- Si está derivada a benchmark: motivo y fecha desde cuándo

**5. Perfil de riesgo**
- Selector de perfil: `Conservador | Moderado | Agresivo | Custom`
- Al seleccionar preset: muestra los % que aplica con descripción
- Modo custom: sliders para benchmark %, carry %, tendencia %, relative value %
- Validación en tiempo real: los % deben sumar exactamente 100%
- Preview del impacto en ARS antes de confirmar
- Botón "Aplicar" → persiste en DB, el sistema adopta el perfil en el próximo ciclo
- Historial de cambios con timestamp

**6. Research**
- Nivel de riesgo macro actual con justificación en texto libre
- Sizing multiplier activo con explicación
- Estrategias pausadas por el research agent
- Feed de últimas noticias procesadas con score de impacto (-1 a +1) y fuente
- Cuentas de X monitoreadas: lista editable sin reiniciar el sistema
- Feeds RSS activos: lista editable
- Botón "Forzar actualización" → análisis inmediato fuera del schedule

### Endpoints FastAPI

```python
# Perfil de riesgo
GET  /api/perfil/actual
POST /api/perfil/cambiar          # {"nombre": "agresivo"} o valores custom
GET  /api/perfil/historial

# Estado del sistema
GET  /api/estado
GET  /api/pnl?periodo=dia|semana|mes|anio
GET  /api/benchmark/actual        # Tasa Lecap + inflación estimada

# Estrategias
GET  /api/estrategias
POST /api/estrategias/{nombre}/pausar
POST /api/estrategias/{nombre}/reactivar

# Research
GET  /api/research/contexto
GET  /api/research/noticias?limit=20
POST /api/research/twitter-accounts   # Editar cuentas monitoreadas

# Órdenes
GET  /api/ordenes?estrategia=&ticker=&desde=&hasta=
```

### Seguridad del dashboard

- Autenticación básica con usuario/contraseña (configurable en .env)
- HTTPS si se configura dominio propio (Let's Encrypt)
- El dashboard nunca ejecuta órdenes directamente — solo cambia configuración
- Toda acción queda logueada en DB con timestamp

```env
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=        # Password fuerte, nunca el default
DASHBOARD_PORT=8080
```

### Orden de implementación del dashboard

Se implementa al final, después de que el sistema de trading funcione en paper trading:

1. FastAPI básico con endpoint de estado y P&L
2. HTML estático con Chart.js para ver el P&L
3. Pantalla de perfil de riesgo con los presets y sliders
4. Pantalla de estrategias con pause/reactivar
5. Pantalla de research y log de órdenes

---

## Research Agent

Agente paralelo al de trading que monitorea fuentes de noticias y produce un contexto de mercado estructurado. Las estrategias de trading consultan este contexto antes de generar señales.

### Arquitectura

```
[Fuentes] → [Collector] → [Claude API] → [contexto_mercado] → [Trading Agent]
```

- Corre de forma independiente al trading agent
- Actualiza el contexto cada 30 minutos en horario de mercado
- Ante eventos críticos puede triggerear una actualización inmediata
- El trading agent lee el contexto antes de cada señal — si el riesgo es ALTO, reduce sizing o no opera

### Estructura de archivos

```
research/
├── __init__.py
├── collector.py        # Recolecta noticias de todas las fuentes
├── twitter_scraper.py  # Scraping de X via Nitter
├── rss_reader.py       # Lectura de feeds RSS
├── analyzer.py         # Llama a Claude API con prompt enriquecido por sources.json
├── context.py          # Lee/escribe contexto_mercado en DB
├── feedback.py         # Mide aciertos automáticamente con market data de PPI
└── sources.json        # Configuración de cuentas con credibilidad, peso y notas
```

### Variables de entorno para research (.env)

```env
# Cuentas de X a monitorear (separadas por coma, configurables sin tocar código)
TWITTER_ACCOUNTS=BancoCentral_AR,MinEconomiaAR,LuisCaputoAR,IMFNews

# Instancia de Nitter para scraping (hay varias públicas, configurar la más estable)
NITTER_BASE_URL=https://nitter.net

# Feeds RSS a monitorear (separados por coma)
RSS_FEEDS=https://www.ambito.com/rss,https://www.cronista.com/rss,https://www.infobae.com/economia/rss

# Claude API para análisis de noticias
ANTHROPIC_API_KEY=
RESEARCH_MODEL=claude-haiku-4-5-20251001   # Haiku es suficiente y mucho más barato

# Frecuencia de actualización en minutos
RESEARCH_INTERVAL_MIN=30
RESEARCH_INTERVAL_MIN_MARKET_HOURS=15      # Más frecuente durante horario de mercado
```

### Formato del contexto de mercado (JSON en DB)

```json
{
  "timestamp": "2025-04-05T14:30:00-03:00",
  "riesgo_macro": "medio",          // "bajo" | "medio" | "alto" | "critico"
  "sentimiento": 0.2,               // -1.0 (muy negativo) a 1.0 (muy positivo)
  "eventos_activos": [
    {
      "tipo": "regulatorio",
      "descripcion": "BCRA modificó encajes bancarios",
      "impacto": ["carry_bonos", "pares"],
      "severidad": "alta",
      "fuente": "twitter/@BancoCentral_AR",
      "timestamp": "2025-04-05T13:15:00-03:00"
    }
  ],
  "estrategias_pausadas": ["carry_bonos"],   // El research agent puede pausar estrategias
  "sizing_multiplier": 0.5,                  // 1.0 = normal, <1.0 = reducir tamaño
  "resumen": "Texto libre con el análisis del momento actual del mercado"
}
```

### Lógica del collector (collector.py)

```python
class ResearchCollector:
    def collect_twitter(self) -> list[dict]    # Scraping de cuentas configuradas
    def collect_rss(self) -> list[dict]        # Lectura de feeds RSS
    def collect_all(self) -> list[dict]        # Combina todas las fuentes
```

### Lógica del analyzer (analyzer.py)

Llama a Claude API (Haiku) con todas las noticias recolectadas y produce el JSON de contexto.

Prompt base al modelo:
```
Sos un analista financiero especializado en mercados argentinos.
Analizá las siguientes noticias y producí un JSON con este formato exacto: [schema]
Enfocate en el impacto sobre: bonos, acciones del Merval, tipo de cambio y futuros de dólar.
Sé conservador: ante la duda, subí el nivel de riesgo.
Respondé SOLO con el JSON, sin texto adicional.
```

### Cómo consume el contexto el trading agent

Antes de ejecutar cualquier señal, cada estrategia llama a:

```python
class ContextReader:
    def get_current_context(self) -> MarketContext
    def is_strategy_paused(self, strategy_name: str) -> bool
    def get_sizing_multiplier(self) -> float      # Multiplica el tamaño de la orden
    def get_riesgo_macro(self) -> str
```

Si `riesgo_macro == "critico"`: ninguna estrategia opera, solo se mantienen posiciones abiertas con stop loss ajustado.
Si `estrategias_pausadas` contiene la estrategia actual: no genera señales.
El `sizing_multiplier` se aplica a todas las órdenes — si es 0.5, todas las posiciones se abren a la mitad del tamaño normal.

### Configuración de fuentes con credibilidad (sources.json)

Las cuentas de X no son una lista plana — cada una tiene atributos que el research agent usa al ponderar el impacto de sus declaraciones. Este archivo vive en el proyecto y es editable desde el dashboard.

```json
{
  "accounts": [
    {
      "username": "LuisCaputoAR",
      "nombre": "Luis Caputo — Ministro de Economía",
      "afinidad_mercado": "pro",
      "peso": 1.0,
      "ventana_medicion_min": 30,
      "notas": "Impacto directo y en la dirección esperada. Alta correlación con movimiento de bonos y tipo de cambio."
    },
    {
      "username": "BancoCentral_AR",
      "nombre": "BCRA",
      "afinidad_mercado": "neutral",
      "peso": 1.0,
      "ventana_medicion_min": 15,
      "notas": "Comunicados oficiales de tasa y regulación. Impacto inmediato y directo."
    },
    {
      "username": "Kicillof",
      "nombre": "Axel Kicillof — Gobernador PBA",
      "afinidad_mercado": "contra",
      "peso": 0.3,
      "ventana_medicion_min": 60,
      "notas": "Oposición al gobierno nacional. Efecto frecuentemente inverso al esperado por el emisor. Declaraciones críticas sobre dólar o inflación suelen generar rally por expectativa de que el gobierno nacional reafirme su postura. Medir en ventana de 60 min para capturar el efecto real."
    },
    {
      "username": "JMilei",
      "nombre": "Javier Milei — Presidente",
      "afinidad_mercado": "pro",
      "peso": 0.8,
      "ventana_medicion_min": 30,
      "notas": "Distinguir entre tweets de política económica vs ideológicos/personales. Los económicos mueven mercado, los ideológicos tienen impacto menor. Peso menor a Caputo porque sus declaraciones son más impredecibles."
    },
    {
      "username": "IMFNews",
      "nombre": "FMI",
      "afinidad_mercado": "pro",
      "peso": 1.0,
      "ventana_medicion_min": 120,
      "notas": "Alto impacto en bonos soberanos. Medir en ventana larga — el mercado tarda en pricear completamente. Si el mercado ya tenía priceado el acuerdo, el efecto puede revertirse en la segunda hora."
    }
  ]
}
```

El prompt del analyzer incluye estos metadatos antes de pedir el análisis:

```
Contexto sobre credibilidad de fuentes para el mercado argentino:

- @LuisCaputoAR (peso 1.0, afinidad pro): impacto directo y en dirección esperada.
- @Kicillof (peso 0.3, afinidad contra): sus declaraciones críticas frecuentemente 
  generan el efecto INVERSO — una crítica sobre el dólar puede interpretarse como señal 
  de que el gobierno va a reafirmar su postura, generando rally. Ponderá con escepticismo.
- @IMFNews (peso 1.0): alto impacto en bonos, pero si el mercado ya lo tenía priceado 
  el efecto puede revertirse en 2 horas.

[notas completas de cada cuenta según sources.json]

Analizá las siguientes noticias considerando estos sesgos de fuente...
```

---

### Feedback loop — medición automática de aciertos

El sistema mide empíricamente si las predicciones del research agent fueron correctas, usando el market data real de PPI. No requiere intervención manual.

**Flujo completo:**

```
Evento detectado
      ↓
Predicción guardada en DB con timestamp y activos afectados
      ↓
Job scheduler espera N minutos (ventana_medicion_min por fuente)
      ↓
Lee precio antes (en el timestamp del evento) y precio después
      ↓
Calcula movimiento real y compara con predicción
      ↓
Registra acierto/error en source_accuracy
      ↓
Cada lunes: recalcula pesos de cada fuente basado en track record de 4 semanas
      ↓
Actualiza sources.json automáticamente con nuevos pesos
```

**Manejo de eventos simultáneos:**

Si en la ventana de medición ocurrió otro evento de alto impacto (dato INDEC, comunicado BCRA), la medición se marca como `contaminada = true`. Se registra pero no se usa para ajustar pesos — el movimiento del mercado no puede atribuirse con certeza a la fuente original.

```python
class FeedbackEngine:
    def schedule_measurement(self, prediccion_id: int, ventana_min: int) -> None
    def measure_impact(self, prediccion_id: int) -> dict
    def detect_contamination(self, timestamp: datetime, ventana_min: int) -> bool
    def recalculate_weights(self) -> None    # Corre cada lunes
    def update_sources_config(self, nuevos_pesos: dict) -> None
```

**Lógica de acierto:**

```python
# Dirección correcta = acierto
# Magnitud también se registra por separado

prediccion = -0.6    # predijo impacto negativo
real = -0.15         # el mercado bajó

acierto_direccion = True     # predijo baja, hubo baja
error_magnitud = 0.45        # sobreestimó el impacto

# Después de N eventos, si el agente sistemáticamente sobreestima
# el impacto de Kicillof, el sistema aprende a reducir su peso
```

---

### Tablas en DB para feedback

```sql
CREATE TABLE source_predictions (
    id                  SERIAL PRIMARY KEY,
    timestamp_evento    TIMESTAMPTZ NOT NULL,
    fuente              VARCHAR(100) NOT NULL,
    username            VARCHAR(50),
    contenido_resumen   TEXT,
    activos_afectados   JSONB,               -- ["AL30", "DLR/JUN25"]
    impacto_predicho    NUMERIC(4,2),        -- -1.0 a 1.0
    ventana_min         INTEGER NOT NULL,
    medicion_schedulada TIMESTAMPTZ,
    medida              BOOLEAN DEFAULT false,
    contaminada         BOOLEAN DEFAULT false,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE source_accuracy (
    id                  SERIAL PRIMARY KEY,
    prediction_id       INTEGER REFERENCES source_predictions(id),
    username            VARCHAR(50) NOT NULL,
    impacto_predicho    NUMERIC(4,2),
    impacto_real        NUMERIC(4,2),
    acierto_direccion   BOOLEAN,
    error_magnitud      NUMERIC(4,2),
    contaminada         BOOLEAN DEFAULT false,
    medido_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE source_weights_history (
    id          SERIAL PRIMARY KEY,
    username    VARCHAR(50) NOT NULL,
    peso_anterior NUMERIC(4,2),
    peso_nuevo  NUMERIC(4,2),
    win_rate    NUMERIC(4,2),    -- % de aciertos en las últimas 4 semanas
    n_eventos   INTEGER,         -- cantidad de eventos medidos
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
```

---

### Orden de implementación del research agent

Se implementa en paralelo a la Fase 4 del trading agent:

1. `rss_reader.py` — lo más simple y confiable, arrancar por acá
2. `sources.json` — configuración inicial de cuentas con credibilidad
3. `twitter_scraper.py` — Nitter scraping de las cuentas configuradas
4. `analyzer.py` — integración con Claude API (Haiku) con prompt enriquecido por sources.json
5. `context.py` — persistencia en DB y lectura por el trading agent
6. `feedback.py` — medición automática de aciertos con market data de PPI
7. Integrar `ContextReader` en la clase base `Strategy`
8. Job semanal de recálculo de pesos en scheduler
9. Tests con noticias históricas para validar que el analyzer produce contexto correcto

### Tabla en DB para el contexto

```sql
CREATE TABLE market_context (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL,
    riesgo_macro    VARCHAR(10) NOT NULL,
    sentimiento     NUMERIC(4,2),
    sizing_mult     NUMERIC(4,2) DEFAULT 1.0,
    eventos         JSONB,
    estrategias_pausadas JSONB,
    resumen         TEXT,
    fuentes_count   INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- El trading agent siempre lee el registro más reciente
CREATE INDEX idx_market_context_timestamp ON market_context(timestamp DESC);
```

---



```bash
# Levantar el sistema completo
docker-compose up -d

# Ver logs en tiempo real
docker-compose logs -f trading

# Correr en modo paper trading (sandbox)
PPI_SANDBOX=true python main.py

# Correr tests
pytest tests/ -v

# Conectar a la DB
docker-compose exec postgres psql -U trading -d trading

# Ver posiciones actuales
python -c "from db.repository import Repository; r = Repository(); print(r.get_posiciones_abiertas())"
```