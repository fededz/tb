"""Estrategias de trading algoritmico para mercados argentinos."""

from strategies.base import Signal, Strategy
from strategies.carry_bonos import CarryBonos
from strategies.carry_futuros import CarryFuturos
from strategies.mean_reversion import MeanReversionIntraday
from strategies.momentum_acciones import MomentumAcciones
from strategies.pares import ParesStrategy
from strategies.trend_following import TrendFollowing

__all__ = [
    "Signal",
    "Strategy",
    "CarryBonos",
    "CarryFuturos",
    "MeanReversionIntraday",
    "MomentumAcciones",
    "ParesStrategy",
    "TrendFollowing",
]
