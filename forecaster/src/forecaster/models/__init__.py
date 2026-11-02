"""Forecasting models behind one interface (base.Model)."""

from .base import Forecast, Model, ModelError
from .holtwinters import HoltWinters
from .naive import SeasonalNaive

MODELS = {m.name: m for m in (SeasonalNaive, HoltWinters)}


def get(name):
    try:
        return MODELS[name]()
    except KeyError:
        raise ValueError(f"unknown model {name!r}; have {sorted(MODELS)}") from None


__all__ = ["Forecast", "Model", "ModelError", "MODELS", "get"]
