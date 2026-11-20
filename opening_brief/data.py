"""Price data from yfinance.

Daily history is downloaded once for the whole watchlist in a single batch
request. Tickers that come back empty are retried individually once, after a
short backoff. A ticker that still fails is returned with an error string and
the rest of the brief carries on.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pandas as pd

from .market_calendar import ET
from .util import log

PREMARKET_OPEN = time(4, 0)
REGULAR_OPEN = time(9, 30)


@dataclass
class TickerData:
    ticker: str
    daily: pd.DataFrame | None = None       # completed sessions strictly before the brief date
    premarket: pd.DataFrame | None = None   # 1m bars 04:00 up to the as-of time
    session_open: float | None = None       # official open of the brief date (replay fallback only)
    error: str | None = None


def _split_download(raw: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out
    if isinstance(raw.columns, pd.MultiIndex):
        level0 = set(raw.columns.get_level_values(0))
        level1 = set(raw.columns.get_level_values(1))
        for t in tickers:
            if t in level0:
                df = raw[t]
            elif t in level1:  # (Price, Ticker) ordering
                df = raw.xs(t, axis=1, level=1)
            else:
                continue
            df = df.dropna(how="all")
            if not df.empty:
                out[t] = df
    elif len(tickers) == 1:
        df = raw.dropna(how="all")
        if not df.empty:
            out[tickers[0]] = df
    return out


class YFinanceProvider:
    """Thin wrapper around yfinance so tests can swap in a fake provider."""

    def __init__(self, backoff: float = 2.0):  # seconds before the single retry
        self.backoff = backoff

    def _download(self, tickers: list[str], **kw) -> dict[str, pd.DataFrame]:
        import yfinance as yf

        try:
            raw = yf.download(
                tickers if len(tickers) > 1 else tickers[0],
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=True,
                **kw,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("yfinance download failed for %s: %s", tickers, exc)
            return {}
        return _split_download(raw, tickers)

    def _download_with_retry(self, tickers: list[str], **kw) -> dict[str, pd.DataFrame]:
        got = self._download(tickers, **kw)
        missing = [t for t in tickers if t not in got]
        if missing:
            log.warning("No data for %s; retrying once in %.0fs", missing, self.backoff)
            _time.sleep(self.backoff)
            for t in missing:
                got.update(self._download([t], **kw))
        return got

    def daily(self, tickers: list[str], brief_date: date) -> dict[str, pd.DataFrame]:
        # ~400 calendar days gives 250+ sessions: plenty for an 8 EMA and ATR14
        start = brief_date - timedelta(days=400)
        end = brief_date + timedelta(days=1)  # yfinance end is exclusive
        return self._download_with_retry(
            tickers, start=start.isoformat(), end=end.isoformat(), interval="1d"
        )

    def premarket(self, tickers: list[str], brief_date: date) -> dict[str, pd.DataFrame]:
        start = brief_date
        end = brief_date + timedelta(days=1)
        return self._download_with_retry(
            tickers, start=start.isoformat(), end=end.isoformat(),
            interval="1m", prepost=True,
        )


def _to_et_index(df: pd.DataFrame) -> pd.DataFrame:
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    df = df.copy()
    df.index = idx.tz_convert(ET)
    return df


def _daily_dates(df: pd.DataFrame) -> pd.Series:
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert(ET).tz_localize(None)
    return pd.Series(idx.normalize().date, index=df.index)


def slice_premarket(df: pd.DataFrame, brief_date: date, as_of: datetime) -> pd.DataFrame:
    """1m bars on brief_date from 04:00 ET up to (not including) as_of and 09:30."""
    df = _to_et_index(df)
    start = datetime.combine(brief_date, PREMARKET_OPEN, tzinfo=ET)
    cutoff = min(as_of.astimezone(ET), datetime.combine(brief_date, REGULAR_OPEN, tzinfo=ET))
    mask = (df.index >= start) & (df.index < cutoff)
    out = df.loc[mask]
    return out.dropna(subset=["High", "Low", "Close"])


def load_market_data(
    provider,
    tickers: list[str],
    brief_date: date,
    as_of: datetime,
    replay: bool = False,
) -> dict[str, TickerData]:
    daily = provider.daily(tickers, brief_date)
    pm = provider.premarket(tickers, brief_date)

    result: dict[str, TickerData] = {}
    for t in tickers:
        td = TickerData(ticker=t)
        d = daily.get(t)
        if d is None or d.empty:
            td.error = "no daily data"
            result[t] = td
            continue
        d = d.dropna(subset=["Close"])
        dates = _daily_dates(d)
        hist = d.loc[dates < brief_date]
        today = d.loc[dates == brief_date]
        if len(hist) < 30:
            td.error = f"only {len(hist)} daily bars"
            result[t] = td
            continue
        hist = hist.copy()
        hist.index = pd.DatetimeIndex(dates[dates < brief_date].values)
        td.daily = hist
        # The official open only exists once the session has started, so it is
        # used only when replaying a past day.
        if replay and not today.empty and pd.notna(today["Open"].iloc[0]):
            td.session_open = float(today["Open"].iloc[0])

        p = pm.get(t)
        if p is not None and not p.empty:
            sliced = slice_premarket(p, brief_date, as_of)
            td.premarket = sliced if not sliced.empty else None
        result[t] = td
    return result
