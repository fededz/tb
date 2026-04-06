"""Shared fixtures for all tests.

Provides pre-configured MagicMock objects for the main dependencies
so that tests run without a database, PPI connection, or Telegram bot.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_repository() -> MagicMock:
    """Repository mock with commonly needed return values."""
    repo = MagicMock()
    repo.get_active_orders.return_value = []
    repo.get_posiciones_abiertas.return_value = []
    repo.get_pnl_diario.return_value = {"pnl_ars": 0.0, "pnl_usd": 0.0}
    repo.get_latest_market_context.return_value = None
    repo.get_active_risk_profile.return_value = {
        "nombre": "moderado",
        "max_drawdown_diario_pct": 0.03,
    }
    repo.insert_order.return_value = {"id": 1}
    repo.get_order_by_id.return_value = None
    return repo


@pytest.fixture
def mock_ppi() -> MagicMock:
    """PPIWrapper mock with default price and balance responses."""
    ppi = MagicMock()
    ppi.get_current_price.return_value = 1000.0
    ppi.get_historical.return_value = MagicMock()  # empty DataFrame-like
    ppi.get_balance.return_value = {"total": 1_000_000.0, "disponible": 500_000.0}
    ppi.get_book.return_value = {"bids": [], "asks": []}
    return ppi


@pytest.fixture
def mock_alertas() -> MagicMock:
    """Alertas mock with async methods."""
    alertas = MagicMock()
    alertas.send = MagicMock()
    alertas.orden_ejecutada = MagicMock()
    alertas.orden_rechazada = MagicMock()
    alertas.signal_generada = MagicMock()
    return alertas


@pytest.fixture
def mock_portfolio() -> MagicMock:
    """Portfolio mock with reasonable defaults.

    Simulates a portfolio with 1,000,000 ARS total capital,
    3 open positions, and 0 P&L for the day.
    """
    portfolio = MagicMock()
    portfolio.get_capital_total.return_value = 1_000_000.0
    portfolio.get_capital_disponible.return_value = 500_000.0
    portfolio.get_posiciones_count.return_value = 3
    portfolio.get_posiciones.return_value = {}
    portfolio.get_pnl_diario.return_value = 0.0
    portfolio.get_pnl_total.return_value = 0.0
    return portfolio


@pytest.fixture
def mock_historical_data() -> MagicMock:
    """HistoricalData mock."""
    return MagicMock()
