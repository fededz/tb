"""Tests for strategies.carry_futuros.CarryFuturos.generate_signals().

All PPI and portfolio dependencies are mocked.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from strategies.carry_futuros import (
    CONTRATOS_BASE,
    MAX_DIAS_VENCIMIENTO,
    UMBRAL_ENTRADA_TASA,
    UMBRAL_SALIDA_TASA,
    CarryFuturos,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_strategy(
    mock_ppi: MagicMock,
    mock_portfolio: MagicMock,
    mock_repository: MagicMock,
    mock_alertas: MagicMock,
    mock_historical_data: MagicMock,
) -> CarryFuturos:
    """Instantiate CarryFuturos with all mocked dependencies."""
    return CarryFuturos(
        ppi=mock_ppi,
        portfolio=mock_portfolio,
        risk_manager=MagicMock(),
        order_manager=MagicMock(),
        repository=mock_repository,
        alertas=mock_alertas,
        historical_data=mock_historical_data,
    )


def _setup_prices(mock_ppi: MagicMock, al30_ars: float, al30d_usd: float, futuro_precio: float):
    """Configure mock_ppi to return specific spot and future prices.

    The get_current_price mock uses side_effect to differentiate tickers.
    """
    def price_side_effect(ticker, tipo, plazo):
        if ticker == "AL30":
            return al30_ars
        if ticker == "AL30D":
            return al30d_usd
        if "DLR/" in ticker:
            return futuro_precio
        return 0.0

    mock_ppi.get_current_price.side_effect = price_side_effect


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCarryFuturosGenerateSignals:

    def test_buy_signal_when_rate_above_entry_threshold(
        self, mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
    ):
        # Spot MEP = 60000 / 50 = 1200. Futuro = 1500.
        # With ~60 days to expiry: rate ~ (1500/1200 - 1) * (365/60) ~ 1.52 > 0.40
        _setup_prices(mock_ppi, al30_ars=60_000.0, al30d_usd=50.0, futuro_precio=1500.0)
        mock_portfolio.get_posiciones.return_value = {}  # no position

        strategy = _make_strategy(
            mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
        )
        signals = strategy.generate_signals()

        assert len(signals) == 1
        assert signals[0].operacion == "COMPRA"
        assert signals[0].tipo == "Futuros"
        assert signals[0].cantidad == CONTRATOS_BASE

    def test_sell_signal_when_rate_below_exit_threshold_with_position(
        self, mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
    ):
        # Spot MEP = 60000 / 50 = 1200. Futuro = 1220.
        # Rate ~ (1220/1200 - 1) * (365/60) ~ 0.10 < 0.25 exit threshold
        _setup_prices(mock_ppi, al30_ars=60_000.0, al30d_usd=50.0, futuro_precio=1220.0)

        # Has an existing position -- the key must match a DLR/XXXXX ticker
        # We need the portfolio to report a position for the future ticker
        # that _get_proximo_futuro will find
        mock_portfolio.get_posiciones.return_value = MagicMock(
            __contains__=lambda self, key: "DLR/" in key
        )

        strategy = _make_strategy(
            mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
        )
        signals = strategy.generate_signals()

        assert len(signals) == 1
        assert signals[0].operacion == "VENTA"

    def test_no_signal_when_rate_between_thresholds(
        self, mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
    ):
        # Spot MEP = 60000 / 50 = 1200. Futuro = 1260.
        # Rate ~ (1260/1200 - 1) * (365/60) ~ 0.30 -- between 0.25 and 0.40
        _setup_prices(mock_ppi, al30_ars=60_000.0, al30d_usd=50.0, futuro_precio=1260.0)
        mock_portfolio.get_posiciones.return_value = {}

        strategy = _make_strategy(
            mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
        )
        signals = strategy.generate_signals()

        assert len(signals) == 0

    def test_no_signal_when_future_beyond_max_days(
        self, mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
    ):
        # Set up prices so that the rate would trigger a buy, but make all
        # futures return 0 price (simulating no available future within range)
        def price_side_effect(ticker, tipo, plazo):
            if ticker == "AL30":
                return 60_000.0
            if ticker == "AL30D":
                return 50.0
            # All DLR futures return 0 -> _get_proximo_futuro returns None
            return 0.0

        mock_ppi.get_current_price.side_effect = price_side_effect
        mock_portfolio.get_posiciones.return_value = {}

        strategy = _make_strategy(
            mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
        )
        signals = strategy.generate_signals()

        assert len(signals) == 0

    def test_no_signal_when_prices_unavailable(
        self, mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
    ):
        # All prices return 0 -> graceful handling, no crash
        mock_ppi.get_current_price.return_value = 0.0

        strategy = _make_strategy(
            mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
        )
        signals = strategy.generate_signals()

        assert len(signals) == 0

    def test_no_crash_when_al30d_is_zero(
        self, mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
    ):
        # AL30D = 0 would cause division by zero without guard
        _setup_prices(mock_ppi, al30_ars=60_000.0, al30d_usd=0.0, futuro_precio=1500.0)

        strategy = _make_strategy(
            mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
        )
        signals = strategy.generate_signals()

        assert len(signals) == 0
