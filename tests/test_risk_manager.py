"""Tests for core.risk_manager.RiskManager.

All dependencies (Portfolio, Repository, Alertas) are mocked.
Datetime/timezone mocking is used for market-hours checks.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from core.risk_manager import ARGENTINA_TZ, OrderIntent, RiskManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_order(**overrides) -> OrderIntent:
    """Create a default BUY order, with optional field overrides."""
    defaults = {
        "ticker": "GGAL",
        "tipo": "Acciones",
        "operacion": "COMPRA",
        "cantidad": 100.0,
        "precio": 1000.0,
        "plazo": "A-48HS",
        "strategy": "momentum_acciones",
    }
    defaults.update(overrides)
    return OrderIntent(**defaults)


def _make_rm(
    mock_portfolio: MagicMock,
    mock_repository: MagicMock,
    mock_alertas: MagicMock,
) -> RiskManager:
    return RiskManager(
        portfolio=mock_portfolio,
        repository=mock_repository,
        alertas=mock_alertas,
    )


def _mock_weekday_market_hours():
    """Return a datetime that is a Wednesday at 14:00 Argentina time."""
    return datetime(2026, 4, 1, 14, 0, 0, tzinfo=ARGENTINA_TZ)  # Wednesday


def _mock_saturday():
    """Return a datetime that is a Saturday."""
    return datetime(2026, 4, 4, 14, 0, 0, tzinfo=ARGENTINA_TZ)  # Saturday


def _mock_late_night():
    """Return a datetime that is a weekday at 23:00 (outside market hours)."""
    return datetime(2026, 4, 1, 23, 0, 0, tzinfo=ARGENTINA_TZ)  # Wednesday 23:00


# ---------------------------------------------------------------------------
# Tests: validate()
# ---------------------------------------------------------------------------

class TestRiskManagerValidate:
    """Tests for the validate() entry point."""

    @patch("core.risk_manager.datetime")
    def test_order_passes_all_validations(
        self, mock_dt, mock_portfolio, mock_repository, mock_alertas
    ):
        mock_dt.now.return_value = _mock_weekday_market_hours()
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        rm = _make_rm(mock_portfolio, mock_repository, mock_alertas)
        order = _make_order()

        approved, reason = rm.validate(order)

        assert approved is True
        assert reason == "ok"

    @patch("core.risk_manager.datetime")
    def test_insufficient_capital(
        self, mock_dt, mock_portfolio, mock_repository, mock_alertas
    ):
        mock_dt.now.return_value = _mock_weekday_market_hours()
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        # Order costs 100 * 1000 = 100,000 but only 10,000 available
        mock_portfolio.get_capital_disponible.return_value = 10_000.0
        rm = _make_rm(mock_portfolio, mock_repository, mock_alertas)
        order = _make_order(cantidad=100, precio=1000.0)

        approved, reason = rm.validate(order)

        assert approved is False
        assert "Capital insuficiente" in reason

    @patch("core.risk_manager.datetime")
    def test_exceeds_max_capital_per_operation(
        self, mock_dt, mock_portfolio, mock_repository, mock_alertas
    ):
        mock_dt.now.return_value = _mock_weekday_market_hours()
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        # Capital total = 1,000,000. Max per op = 5% = 50,000
        # Order = 100 * 600 = 60,000 > 50,000
        mock_portfolio.get_capital_total.return_value = 1_000_000.0
        mock_portfolio.get_capital_disponible.return_value = 1_000_000.0
        rm = _make_rm(mock_portfolio, mock_repository, mock_alertas)
        order = _make_order(cantidad=100, precio=600.0)

        approved, reason = rm.validate(order)

        assert approved is False
        assert "limite por operacion" in reason

    @patch("core.risk_manager.datetime")
    def test_too_many_open_positions(
        self, mock_dt, mock_portfolio, mock_repository, mock_alertas
    ):
        mock_dt.now.return_value = _mock_weekday_market_hours()
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        # 10 positions open (the max), and the order is for a new ticker
        mock_portfolio.get_posiciones_count.return_value = 10
        mock_portfolio.get_posiciones.return_value = {}  # ticker not in existing
        mock_portfolio.get_capital_disponible.return_value = 500_000.0
        mock_portfolio.get_capital_total.return_value = 1_000_000.0

        rm = _make_rm(mock_portfolio, mock_repository, mock_alertas)
        order = _make_order(cantidad=1, precio=100.0)

        approved, reason = rm.validate(order)

        assert approved is False
        assert "Maximo de posiciones" in reason

    @patch("core.risk_manager.datetime")
    def test_drawdown_exceeded(
        self, mock_dt, mock_portfolio, mock_repository, mock_alertas
    ):
        mock_dt.now.return_value = _mock_weekday_market_hours()
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        # P&L diario = -40,000 on capital of 1,000,000 -> 4% DD > 3% max (moderado)
        mock_portfolio.get_pnl_diario.return_value = -40_000.0
        mock_portfolio.get_capital_total.return_value = 1_000_000.0
        mock_portfolio.get_capital_disponible.return_value = 500_000.0

        rm = _make_rm(mock_portfolio, mock_repository, mock_alertas)
        order = _make_order(cantidad=1, precio=100.0)

        approved, reason = rm.validate(order)

        assert approved is False
        assert "Drawdown diario" in reason

    @patch("core.risk_manager.datetime")
    def test_outside_market_hours_saturday(
        self, mock_dt, mock_portfolio, mock_repository, mock_alertas
    ):
        mock_dt.now.return_value = _mock_saturday()
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        rm = _make_rm(mock_portfolio, mock_repository, mock_alertas)
        order = _make_order()

        approved, reason = rm.validate(order)

        assert approved is False
        assert "fin de semana" in reason

    @patch("core.risk_manager.datetime")
    def test_outside_market_hours_late_night(
        self, mock_dt, mock_portfolio, mock_repository, mock_alertas
    ):
        mock_dt.now.return_value = _mock_late_night()
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        rm = _make_rm(mock_portfolio, mock_repository, mock_alertas)
        order = _make_order()

        approved, reason = rm.validate(order)

        assert approved is False
        assert "Fuera de horario" in reason

    @patch("core.risk_manager.datetime")
    def test_during_market_hours_passes(
        self, mock_dt, mock_portfolio, mock_repository, mock_alertas
    ):
        mock_dt.now.return_value = _mock_weekday_market_hours()
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        rm = _make_rm(mock_portfolio, mock_repository, mock_alertas)
        order = _make_order(cantidad=1, precio=100.0)

        approved, reason = rm.validate(order)

        assert approved is True
        assert reason == "ok"

    @patch("core.risk_manager.datetime")
    def test_strategy_paused_by_research(
        self, mock_dt, mock_portfolio, mock_repository, mock_alertas
    ):
        mock_dt.now.return_value = _mock_weekday_market_hours()
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        mock_repository.get_latest_market_context.return_value = {
            "riesgo_macro": "medio",
            "estrategias_pausadas": ["momentum_acciones"],
            "sizing_mult": 1.0,
        }
        mock_portfolio.get_capital_disponible.return_value = 500_000.0
        mock_portfolio.get_capital_total.return_value = 1_000_000.0

        rm = _make_rm(mock_portfolio, mock_repository, mock_alertas)
        order = _make_order(cantidad=1, precio=100.0, strategy="momentum_acciones")

        approved, reason = rm.validate(order)

        assert approved is False
        assert "pausada por research" in reason

    @patch("core.risk_manager.datetime")
    def test_riesgo_macro_critico_blocks_buys(
        self, mock_dt, mock_portfolio, mock_repository, mock_alertas
    ):
        mock_dt.now.return_value = _mock_weekday_market_hours()
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        mock_repository.get_latest_market_context.return_value = {
            "riesgo_macro": "critico",
            "estrategias_pausadas": [],
            "sizing_mult": 0.0,
        }
        mock_portfolio.get_capital_disponible.return_value = 500_000.0
        mock_portfolio.get_capital_total.return_value = 1_000_000.0

        rm = _make_rm(mock_portfolio, mock_repository, mock_alertas)
        order = _make_order(cantidad=1, precio=100.0)

        approved, reason = rm.validate(order)

        assert approved is False
        assert "CRITICO" in reason


# ---------------------------------------------------------------------------
# Tests: adjust_size_for_context()
# ---------------------------------------------------------------------------

class TestAdjustSizeForContext:
    """Tests for the sizing multiplier logic."""

    def test_sizing_multiplier_halves_size(
        self, mock_portfolio, mock_repository, mock_alertas
    ):
        mock_repository.get_latest_market_context.return_value = {
            "riesgo_macro": "alto",
            "sizing_mult": 0.5,
        }
        rm = _make_rm(mock_portfolio, mock_repository, mock_alertas)

        result = rm.adjust_size_for_context(10.0)

        assert result == 5.0

    def test_riesgo_critico_returns_zero(
        self, mock_portfolio, mock_repository, mock_alertas
    ):
        mock_repository.get_latest_market_context.return_value = {
            "riesgo_macro": "critico",
            "sizing_mult": 0.0,
        }
        rm = _make_rm(mock_portfolio, mock_repository, mock_alertas)

        result = rm.adjust_size_for_context(10.0)

        assert result == 0.0

    def test_no_context_returns_original_size(
        self, mock_portfolio, mock_repository, mock_alertas
    ):
        mock_repository.get_latest_market_context.return_value = None
        rm = _make_rm(mock_portfolio, mock_repository, mock_alertas)

        result = rm.adjust_size_for_context(10.0)

        assert result == 10.0

    def test_sizing_multiplier_one_returns_original(
        self, mock_portfolio, mock_repository, mock_alertas
    ):
        mock_repository.get_latest_market_context.return_value = {
            "riesgo_macro": "bajo",
            "sizing_mult": 1.0,
        }
        rm = _make_rm(mock_portfolio, mock_repository, mock_alertas)

        result = rm.adjust_size_for_context(10.0)

        assert result == 10.0
