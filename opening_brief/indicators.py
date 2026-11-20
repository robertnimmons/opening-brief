"""Deterministic indicator math. No I/O here, so everything is unit-testable."""
from __future__ import annotations

import math
from typing import Sequence

import pandas as pd


def ema(values: Sequence[float], length: int) -> list[float]:
    """EMA seeded with the SMA of the first `length` values (TradingView style).

    Returns a list the same length as `values`; entries before the seed are NaN.
    """
    vals = [float(v) for v in values]
    out = [math.nan] * len(vals)
    if len(vals) < length:
        return out
    k = 2.0 / (length + 1)
    prev = sum(vals[:length]) / length
    out[length - 1] = prev
    for i in range(length, len(vals)):
        prev = vals[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def atr(high: Sequence[float], low: Sequence[float], close: Sequence[float], length: int = 14) -> list[float]:
    """Wilder's ATR (RMA of true range), seeded with the SMA of the first `length` TRs."""
    n = len(close)
    tr = [math.nan] * n
    for i in range(n):
        hl = high[i] - low[i]
        if i == 0:
            tr[i] = hl
        else:
            tr[i] = max(hl, abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    out = [math.nan] * n
    if n < length:
        return out
    prev = sum(tr[:length]) / length
    out[length - 1] = prev
    for i in range(length, n):
        prev = (prev * (length - 1) + tr[i]) / length
        out[i] = prev
    return out


def ema_status(closes: Sequence[float], emas: Sequence[float], lookback: int = 3) -> str:
    """'above' if the last `lookback` closes all closed above their EMA,
    'below' if all closed below, otherwise 'chop'."""
    c = list(closes)[-lookback:]
    e = list(emas)[-lookback:]
    if len(c) < lookback or any(math.isnan(x) for x in e):
        return "unknown"
    if all(ci > ei for ci, ei in zip(c, e)):
        return "above"
    if all(ci < ei for ci, ei in zip(c, e)):
        return "below"
    return "chop"


def consolidation_days(
    high: Sequence[float], low: Sequence[float], close: Sequence[float],
    emas: Sequence[float], atr_value: float,
    min_days: int = 2, max_days: int = 4,
    close_atr: float = 0.5, range_atr: float = 1.5,
) -> int:
    """Length (2..4) of the longest tight consolidation sitting on the EMA that
    ends on the latest bar, or 0 if none.

    Tight means the combined high-low range of the window is at most
    range_atr * ATR. Sitting on the EMA means every close in the window is
    within close_atr * ATR of that day's EMA.
    """
    if atr_value is None or math.isnan(atr_value) or atr_value <= 0:
        return 0
    for n in range(max_days, min_days - 1, -1):
        if len(close) < n:
            continue
        h, l, c, e = high[-n:], low[-n:], close[-n:], emas[-n:]
        if any(math.isnan(x) for x in e):
            continue
        if max(h) - min(l) > range_atr * atr_value:
            continue
        if all(abs(ci - ei) <= close_atr * atr_value for ci, ei in zip(c, e)):
            return n
    return 0


def pct_change(new: float, base: float) -> float:
    return (new / base - 1.0) * 100.0


def premarket_levels(pm: pd.DataFrame | None) -> dict:
    if pm is None or pm.empty:
        return {"pm_high": None, "pm_low": None, "pm_last": None, "pm_volume": None, "pm_last_time": None}
    return {
        "pm_high": float(pm["High"].max()),
        "pm_low": float(pm["Low"].min()),
        "pm_last": float(pm["Close"].iloc[-1]),
        "pm_volume": int(pm["Volume"].fillna(0).sum()) if "Volume" in pm else None,
        "pm_last_time": pm.index[-1],
    }
