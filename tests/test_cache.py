"""Tests for market_data.cache.MarketDataCache."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from market_data.cache import MarketDataCache


class TestMarketDataCache:

    def test_update_and_get_price(self):
        cache = MarketDataCache()
        cache.update("GGAL", "Acciones", "A-48HS", 1500.0)

        price = cache.get_price("GGAL", "Acciones", "A-48HS")

        assert price == 1500.0

    def test_get_price_missing_key_returns_none(self):
        cache = MarketDataCache()

        price = cache.get_price("NONEXISTENT", "Acciones", "A-48HS")

        assert price is None

    def test_update_overwrites_previous_value(self):
        cache = MarketDataCache()
        cache.update("GGAL", "Acciones", "A-48HS", 1500.0)
        cache.update("GGAL", "Acciones", "A-48HS", 1600.0)

        price = cache.get_price("GGAL", "Acciones", "A-48HS")

        assert price == 1600.0

    def test_get_age_seconds(self):
        cache = MarketDataCache()
        fixed_time = 1000000.0
        cache.update("GGAL", "Acciones", "A-48HS", 1500.0, timestamp=fixed_time)

        with patch("market_data.cache.time") as mock_time:
            mock_time.time.return_value = fixed_time + 30.0
            age = cache.get_age_seconds("GGAL", "Acciones", "A-48HS")

        assert age == pytest.approx(30.0)

    def test_get_age_seconds_missing_key_returns_none(self):
        cache = MarketDataCache()

        age = cache.get_age_seconds("NONEXISTENT", "Acciones", "A-48HS")

        assert age is None

    def test_get_all(self):
        cache = MarketDataCache()
        cache.update("GGAL", "Acciones", "A-48HS", 1500.0, volume=100_000.0, timestamp=1000.0)
        cache.update("YPF", "Acciones", "A-48HS", 25000.0, volume=50_000.0, timestamp=1001.0)

        all_data = cache.get_all()

        assert len(all_data) == 2
        assert "GGAL:Acciones:A-48HS" in all_data
        assert "YPF:Acciones:A-48HS" in all_data

        ggal = all_data["GGAL:Acciones:A-48HS"]
        assert ggal["price"] == 1500.0
        assert ggal["volume"] == 100_000.0
        assert ggal["timestamp"] == 1000.0

    def test_clear(self):
        cache = MarketDataCache()
        cache.update("GGAL", "Acciones", "A-48HS", 1500.0)
        cache.update("YPF", "Acciones", "A-48HS", 25000.0)

        cache.clear()

        assert cache.get_price("GGAL", "Acciones", "A-48HS") is None
        assert cache.get_price("YPF", "Acciones", "A-48HS") is None
        assert cache.get_all() == {}

    def test_different_plazos_are_separate_entries(self):
        cache = MarketDataCache()
        cache.update("GGAL", "Acciones", "A-48HS", 1500.0)
        cache.update("GGAL", "Acciones", "INMEDIATA", 1510.0)

        assert cache.get_price("GGAL", "Acciones", "A-48HS") == 1500.0
        assert cache.get_price("GGAL", "Acciones", "INMEDIATA") == 1510.0

    def test_update_with_volume(self):
        cache = MarketDataCache()
        cache.update("GGAL", "Acciones", "A-48HS", 1500.0, volume=250_000.0)

        all_data = cache.get_all()
        entry = all_data["GGAL:Acciones:A-48HS"]

        assert entry["volume"] == 250_000.0

    def test_update_without_volume_defaults_to_none(self):
        cache = MarketDataCache()
        cache.update("GGAL", "Acciones", "A-48HS", 1500.0)

        all_data = cache.get_all()
        entry = all_data["GGAL:Acciones:A-48HS"]

        assert entry["volume"] is None
