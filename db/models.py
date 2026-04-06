"""Modelos ORM SQLAlchemy para el sistema de trading.

Define todas las tablas del sistema usando SQLAlchemy 2.0 declarative style.
"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base declarativa compartida por todos los modelos."""

    pass


class Order(Base):
    """Registro de cada orden enviada al mercado."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    strategy: Mapped[str] = mapped_column(String(50), nullable=False)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    tipo: Mapped[str] = mapped_column(String(30), nullable=False)
    operacion: Mapped[str] = mapped_column(String(10), nullable=False)
    cantidad: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    precio: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    plazo: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()"
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)


class Position(Base):
    """Posicion abierta o cerrada en el portafolio."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    tipo: Mapped[str] = mapped_column(String(30), nullable=False)
    cantidad: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    precio_entrada: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    strategy: Mapped[str] = mapped_column(String(50), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()"
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    pnl: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)


class PnlDiario(Base):
    """P&L consolidado por dia."""

    __tablename__ = "pnl_diario"

    fecha: Mapped[date] = mapped_column(Date, primary_key=True)
    pnl_ars: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    pnl_usd: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    capital_inicio: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    capital_fin: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    trades: Mapped[int | None] = mapped_column(Integer, nullable=True)


class MarketDataCache(Base):
    """Cache de datos historicos de mercado (OHLCV por dia)."""

    __tablename__ = "market_data_cache"

    ticker: Mapped[str] = mapped_column(String(20), primary_key=True)
    tipo: Mapped[str] = mapped_column(String(30), primary_key=True)
    plazo: Mapped[str] = mapped_column(String(20), primary_key=True)
    fecha: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    high: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    low: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    close: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    volume: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)


class RiskProfile(Base):
    """Perfil de riesgo activo del sistema.

    Solo un perfil puede estar activo a la vez, garantizado por el indice parcial
    idx_risk_profile_activo.
    """

    __tablename__ = "risk_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[str] = mapped_column(String(20), nullable=False)
    benchmark_pct: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    carry_pct: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    tendencia_pct: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    relative_value_pct: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    spread_minimo_benchmark: Mapped[float] = mapped_column(
        Numeric(5, 4), nullable=False
    )
    max_drawdown_diario_pct: Mapped[float] = mapped_column(
        Numeric(5, 4), nullable=False
    )
    activo: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()"
    )
    updated_by: Mapped[str | None] = mapped_column(String(50), nullable=True)

    __table_args__ = (
        Index(
            "idx_risk_profile_activo",
            activo,
            unique=True,
            postgresql_where=(activo.is_(True)),
        ),
    )


class MarketContext(Base):
    """Contexto de mercado producido por el research agent."""

    __tablename__ = "market_context"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    riesgo_macro: Mapped[str] = mapped_column(String(10), nullable=False)
    sentimiento: Mapped[float | None] = mapped_column(Numeric(4, 2), nullable=True)
    sizing_mult: Mapped[float] = mapped_column(Numeric(4, 2), default=1.0)
    eventos: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    estrategias_pausadas: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    resumen: Mapped[str | None] = mapped_column(Text, nullable=True)
    fuentes_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()"
    )

    __table_args__ = (
        Index("idx_market_context_timestamp", timestamp.desc()),
    )
