"""Tests for core.order_manager.OrderManager.

PPI SDK, Repository, and Alertas are fully mocked. No real API calls are made.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.order_manager import OrderManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_om(
    mock_ppi: MagicMock,
    mock_repository: MagicMock,
    mock_alertas: MagicMock,
) -> OrderManager:
    with patch("core.order_manager.PPI_ACCOUNT_NUMBER", "TEST-123"):
        return OrderManager(
            ppi=mock_ppi,
            repository=mock_repository,
            alertas=mock_alertas,
        )


# ---------------------------------------------------------------------------
# Tests: send_order
# ---------------------------------------------------------------------------

class TestSendOrder:

    def test_dry_run_saves_to_db_does_not_call_ppi(
        self, mock_ppi, mock_repository, mock_alertas
    ):
        mock_repository.insert_order.return_value = {"id": 42}

        om = _make_om(mock_ppi, mock_repository, mock_alertas)

        result = om.send_order(
            ticker="GGAL",
            tipo="Acciones",
            operacion="COMPRA",
            cantidad=100,
            precio=1000.0,
            plazo="A-48HS",
            strategy="momentum_acciones",
            dry_run=True,
        )

        assert result["status"] == "DRY_RUN"
        assert result["order_id"] == 42

        # DB insert was called
        mock_repository.insert_order.assert_called_once()
        # Status updated to DRY_RUN
        mock_repository.update_order_status.assert_called_once_with(42, "DRY_RUN")

    @patch("core.order_manager.PPI")
    def test_send_order_success(
        self, mock_ppi_sdk, mock_ppi, mock_repository, mock_alertas
    ):
        mock_repository.insert_order.return_value = {"id": 7}
        mock_ppi_sdk.orders.budget.return_value = {"budget_id": "B1", "disclaimers": []}
        mock_ppi_sdk.orders.confirm.return_value = {"id": "EXT-99"}

        om = _make_om(mock_ppi, mock_repository, mock_alertas)

        result = om.send_order(
            ticker="GGAL",
            tipo="Acciones",
            operacion="COMPRA",
            cantidad=100,
            precio=1000.0,
            plazo="A-48HS",
            strategy="momentum_acciones",
        )

        assert result["status"] == "EXECUTED"
        assert result["external_id"] == "EXT-99"

        # Budget then confirm were called
        mock_ppi_sdk.orders.budget.assert_called_once()
        mock_ppi_sdk.orders.confirm.assert_called_once()

        # DB was updated to EXECUTED
        calls = mock_repository.update_order_status.call_args_list
        assert any(c.args[1] == "EXECUTED" for c in calls)

    @patch("core.order_manager.PPI")
    def test_send_order_ppi_error(
        self, mock_ppi_sdk, mock_ppi, mock_repository, mock_alertas
    ):
        mock_repository.insert_order.return_value = {"id": 8}
        mock_ppi_sdk.orders.budget.side_effect = ConnectionError("PPI unavailable")

        om = _make_om(mock_ppi, mock_repository, mock_alertas)

        result = om.send_order(
            ticker="GGAL",
            tipo="Acciones",
            operacion="COMPRA",
            cantidad=100,
            precio=1000.0,
            plazo="A-48HS",
            strategy="momentum_acciones",
        )

        assert result["status"] == "REJECTED"
        assert "PPI unavailable" in result["error"]

        # DB updated to REJECTED with error message
        calls = mock_repository.update_order_status.call_args_list
        rejected_call = [c for c in calls if c.args[1] == "REJECTED"]
        assert len(rejected_call) == 1
        assert "PPI unavailable" in rejected_call[0].kwargs.get("error_msg", "")

    def test_idempotency_existing_pending_order(
        self, mock_ppi, mock_repository, mock_alertas
    ):
        # Simulate an existing PENDING order for this ticker/strategy
        mock_repository.get_active_orders.return_value = [
            {"id": 5, "ticker": "GGAL", "strategy": "momentum_acciones", "status": "PENDING"},
        ]

        om = _make_om(mock_ppi, mock_repository, mock_alertas)

        result = om.send_order(
            ticker="GGAL",
            tipo="Acciones",
            operacion="COMPRA",
            cantidad=100,
            precio=1000.0,
            plazo="A-48HS",
            strategy="momentum_acciones",
        )

        assert result["status"] == "ALREADY_PENDING"
        assert result["order_id"] == 5

        # insert_order should NOT have been called
        mock_repository.insert_order.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: cancel_order
# ---------------------------------------------------------------------------

class TestCancelOrder:

    @patch("core.order_manager.PPI")
    def test_cancel_order_success(
        self, mock_ppi_sdk, mock_ppi, mock_repository, mock_alertas
    ):
        mock_repository.get_order_by_id.return_value = {
            "id": 10,
            "status": "EXECUTED",
            "external_id": "EXT-10",
        }

        om = _make_om(mock_ppi, mock_repository, mock_alertas)
        result = om.cancel_order(10)

        assert result["status"] == "CANCELLED"
        mock_ppi_sdk.orders.cancel.assert_called_once()
        mock_repository.update_order_status.assert_called_with(10, "CANCELLED")

    def test_cancel_order_not_found(
        self, mock_ppi, mock_repository, mock_alertas
    ):
        mock_repository.get_order_by_id.return_value = None

        om = _make_om(mock_ppi, mock_repository, mock_alertas)
        result = om.cancel_order(999)

        assert result["status"] == "NOT_FOUND"

    def test_cancel_order_invalid_state(
        self, mock_ppi, mock_repository, mock_alertas
    ):
        mock_repository.get_order_by_id.return_value = {
            "id": 10,
            "status": "CANCELLED",
        }

        om = _make_om(mock_ppi, mock_repository, mock_alertas)
        result = om.cancel_order(10)

        assert result["status"] == "INVALID_STATE"


# ---------------------------------------------------------------------------
# Tests: get_active_orders
# ---------------------------------------------------------------------------

class TestGetActiveOrders:

    def test_returns_from_repository(
        self, mock_ppi, mock_repository, mock_alertas
    ):
        expected = [
            {"id": 1, "ticker": "GGAL", "status": "PENDING"},
            {"id": 2, "ticker": "YPF", "status": "PENDING"},
        ]
        mock_repository.get_active_orders.return_value = expected

        om = _make_om(mock_ppi, mock_repository, mock_alertas)
        result = om.get_active_orders()

        assert result == expected
        mock_repository.get_active_orders.assert_called()
