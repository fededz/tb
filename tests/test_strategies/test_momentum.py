"""Tests for strategies.momentum_acciones.MomentumAcciones.generate_signals().

Historical data is fully mocked via ppi.get_historical().
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

from strategies.momentum_acciones import (
    LOOKBACK_SEMANAS,
    NOMINALES_BASE,
    PLAZO,
    TOP_N,
    UNIVERSO,
    MomentumAcciones,
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
) -> MomentumAcciones:
    return MomentumAcciones(
        ppi=mock_ppi,
        portfolio=mock_portfolio,
        risk_manager=MagicMock(),
        order_manager=MagicMock(),
        repository=mock_repository,
        alertas=mock_alertas,
        historical_data=mock_historical_data,
    )


def _make_historical_df(precio_inicio: float, precio_fin: float, ruedas: int = 20) -> pd.DataFrame:
    """Create a simple DataFrame with a 'close' column going from precio_inicio to precio_fin."""
    precios = [
        precio_inicio + (precio_fin - precio_inicio) * i / (ruedas - 1)
        for i in range(ruedas)
    ]
    return pd.DataFrame({"close": precios})


def _setup_returns(mock_ppi: MagicMock, returns_by_ticker: dict[str, float]):
    """Configure mock_ppi.get_historical to return DataFrames that produce specific returns.

    Args:
        returns_by_ticker: Mapping of ticker -> desired return (e.g. 0.20 = 20%).
    """
    def historical_side_effect(ticker, tipo, plazo, desde, hasta):
        ret = returns_by_ticker.get(ticker)
        if ret is None:
            return pd.DataFrame()  # No data
        # Start at 100, end at 100 * (1 + ret)
        return _make_historical_df(100.0, 100.0 * (1.0 + ret))

    mock_ppi.get_historical.side_effect = historical_side_effect


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMomentumAccionesGenerateSignals:

    def test_generates_buy_for_top_5_sell_for_holdings_not_in_top(
        self, mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
    ):
        # Assign unique returns to each ticker in UNIVERSO
        # The first 5 get the highest returns, the rest get lower ones
        returns = {}
        for i, ticker in enumerate(UNIVERSO):
            # Reverse: first tickers get highest return
            returns[ticker] = 0.30 - i * 0.01

        _setup_returns(mock_ppi, returns)

        # Current holdings: we hold "CEPU" (the last ticker, which is in the bottom)
        # and it is NOT in top 5, so it should be sold
        mock_posicion = MagicMock()
        mock_posicion.strategy = "momentum_acciones"
        mock_posicion.cantidad = 5000.0

        mock_portfolio.get_posiciones.return_value = {
            "CEPU": mock_posicion,
        }

        strategy = _make_strategy(
            mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
        )
        signals = strategy.generate_signals()

        buy_signals = [s for s in signals if s.operacion == "COMPRA"]
        sell_signals = [s for s in signals if s.operacion == "VENTA"]

        # Should buy top 5 tickers (those not already held)
        expected_top = sorted(
            UNIVERSO, key=lambda t: returns[t], reverse=True
        )[:TOP_N]

        buy_tickers = {s.ticker for s in buy_signals}
        assert buy_tickers == set(expected_top)

        # Should sell CEPU since it is not in top 5
        assert len(sell_signals) == 1
        assert sell_signals[0].ticker == "CEPU"
        assert sell_signals[0].cantidad == 5000.0

        # All buy signals use the correct amount and plazo
        for s in buy_signals:
            assert s.cantidad == NOMINALES_BASE
            assert s.plazo == PLAZO

    def test_no_signals_with_insufficient_data(
        self, mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
    ):
        # All tickers return empty DataFrames -> insufficient rankings
        mock_ppi.get_historical.return_value = pd.DataFrame()
        mock_portfolio.get_posiciones.return_value = {}

        strategy = _make_strategy(
            mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
        )
        signals = strategy.generate_signals()

        assert len(signals) == 0

    def test_correct_ranking_logic(
        self, mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
    ):
        # Give specific returns to verify ranking order
        returns = {}
        for ticker in UNIVERSO:
            returns[ticker] = 0.05  # 5% baseline

        # Make VALO the clear winner and GGAL second
        returns["VALO"] = 0.50
        returns["GGAL"] = 0.40
        returns["BMA"] = 0.35
        returns["YPF"] = 0.30
        returns["PAMP"] = 0.25

        _setup_returns(mock_ppi, returns)
        mock_portfolio.get_posiciones.return_value = {}

        strategy = _make_strategy(
            mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
        )
        signals = strategy.generate_signals()

        buy_tickers = [s.ticker for s in signals if s.operacion == "COMPRA"]

        # Top 5 by return should be VALO, GGAL, BMA, YPF, PAMP
        assert "VALO" in buy_tickers
        assert "GGAL" in buy_tickers
        assert "BMA" in buy_tickers
        assert "YPF" in buy_tickers
        assert "PAMP" in buy_tickers

    def test_does_not_buy_already_held_top_tickers(
        self, mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
    ):
        returns = {}
        for i, ticker in enumerate(UNIVERSO):
            returns[ticker] = 0.30 - i * 0.01

        _setup_returns(mock_ppi, returns)

        # Already holding the top ticker
        top_ticker = sorted(UNIVERSO, key=lambda t: returns[t], reverse=True)[0]
        mock_posicion = MagicMock()
        mock_posicion.strategy = "momentum_acciones"
        mock_posicion.cantidad = 5000.0

        mock_portfolio.get_posiciones.return_value = {
            top_ticker: mock_posicion,
        }

        strategy = _make_strategy(
            mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
        )
        signals = strategy.generate_signals()

        buy_tickers = [s.ticker for s in signals if s.operacion == "COMPRA"]

        # Should NOT try to buy the already-held ticker
        assert top_ticker not in buy_tickers
        # Should buy the remaining 4 of top 5
        assert len(buy_tickers) == TOP_N - 1

    def test_partial_data_still_works(
        self, mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
    ):
        # Only provide data for 6 tickers (just above TOP_N)
        available_tickers = UNIVERSO[:6]
        returns = {t: 0.10 + i * 0.02 for i, t in enumerate(available_tickers)}

        _setup_returns(mock_ppi, returns)
        mock_portfolio.get_posiciones.return_value = {}

        strategy = _make_strategy(
            mock_ppi, mock_portfolio, mock_repository, mock_alertas, mock_historical_data
        )
        signals = strategy.generate_signals()

        buy_signals = [s for s in signals if s.operacion == "COMPRA"]
        assert len(buy_signals) == TOP_N
