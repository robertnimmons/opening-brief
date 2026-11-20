"""Fakes that mimic the shapes yfinance, Finnhub and Anthropic return."""
from __future__ import annotations

import json
import random
from datetime import date, datetime, time, timedelta

import pandas as pd

from opening_brief.market_calendar import ET, is_trading_day

BASE_PRICES = {
    "NVDA": 182.0, "TSLA": 410.0, "AMZN": 228.0, "META": 610.0, "AAPL": 268.0, "MSFT": 505.0,
    "GOOGL": 285.0, "AMD": 240.0, "NFLX": 112.0, "AVGO": 345.0, "MU": 205.0, "COIN": 290.0,
    "HOOD": 128.0, "INTC": 38.0, "SPY": 672.0, "QQQ": 605.0, "IWM": 243.0,
}


def make_daily(ticker: str, end: date, n: int = 260, drift: float = 0.0008,
               include_partial_today: bool = True, seed: int | None = None) -> pd.DataFrame:
    """Daily OHLCV ending on the last trading day before `end`.

    Like Yahoo during premarket, it can also include a partial row dated `end`
    with junk values, which the pipeline must ignore.
    """
    rng = random.Random(seed if seed is not None else sum(map(ord, ticker)))
    days = []
    d = end - timedelta(days=1)
    while len(days) < n:
        if is_trading_day(d):
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    price = BASE_PRICES.get(ticker, 100.0) * 0.8
    rows = []
    for _ in days:
        o = price * (1 + rng.gauss(0, 0.004))
        c = o * (1 + rng.gauss(drift, 0.015))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.006)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.006)))
        rows.append((o, h, l, c, c, int(rng.uniform(2e7, 9e7))))
        price = c
    idx = pd.DatetimeIndex(pd.to_datetime(days))
    df = pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close", "Adj Close", "Volume"])
    if include_partial_today:
        df.loc[pd.Timestamp(end)] = [price * 1.5, price * 1.6, price * 1.4, price * 1.55, price * 1.55, 0]
    return df


def make_premarket(ticker: str, d: date, prior_close: float, gap_pct: float,
                   end: time = time(8, 45), seed: int | None = None) -> pd.DataFrame:
    """1m bars from 04:00 ET to `end`, drifting from prior close to the target gap.
    Index is tz-aware America/New_York, as yfinance returns for US equities."""
    rng = random.Random(seed if seed is not None else sum(map(ord, ticker)) * 7)
    start = datetime.combine(d, time(4, 0), tzinfo=ET)
    stop = datetime.combine(d, end, tzinfo=ET)
    n = int((stop - start).total_seconds() // 60)
    target = prior_close * (1 + gap_pct / 100)
    rows, idx = [], []
    for i in range(n):
        mid = prior_close + (target - prior_close) * (i + 1) / n
        c = mid * (1 + rng.gauss(0, 0.0007))
        o = c * (1 + rng.gauss(0, 0.0004))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.0004)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.0004)))
        rows.append((o, h, l, c, c, int(rng.uniform(1e3, 5e4))))
        idx.append(start + timedelta(minutes=i))
    # last bar lands exactly on target so the expected gap is known
    o, h, l, _, _, v = rows[-1]
    rows[-1] = (o, max(h, target), min(l, target), target, target, v)
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx),
                        columns=["Open", "High", "Low", "Close", "Adj Close", "Volume"])


DEFAULT_GAPS = {
    "NVDA": 2.4, "TSLA": -1.1, "AMZN": 0.6, "META": 0.9, "AAPL": 0.3, "MSFT": 0.2, "GOOGL": 1.4,
    "AMD": 3.1, "NFLX": -0.4, "AVGO": 1.8, "MU": 2.9, "COIN": -2.6, "HOOD": -1.9, "INTC": 0.1,
    "SPY": 0.45, "QQQ": 0.7, "IWM": -0.2,
}


class FakeProvider:
    """Stands in for YFinanceProvider."""

    def __init__(self, gaps: dict | None = None, fail: set | None = None, no_pm: set | None = None,
                 daily_overrides: dict | None = None, pm_end: time = time(8, 45)):
        self.gaps = gaps or DEFAULT_GAPS
        self.fail = fail or set()
        self.no_pm = no_pm or set()
        self.overrides = daily_overrides or {}
        self.pm_end = pm_end
        self._daily: dict = {}
        self.calls = {"daily": 0, "premarket": 0}

    def daily(self, tickers, brief_date):
        self.calls["daily"] += 1
        out = {}
        for t in tickers:
            if t in self.fail:
                continue
            df = self.overrides.get(t)
            if df is None:
                df = make_daily(t, brief_date)
            self._daily[t] = df
            out[t] = df
        return out

    def premarket(self, tickers, brief_date):
        self.calls["premarket"] += 1
        out = {}
        for t in tickers:
            if t in self.fail or t in self.no_pm or t not in self._daily:
                continue
            d = self._daily[t]
            hist = d[d.index.date < brief_date]
            out[t] = make_premarket(t, brief_date, float(hist["Close"].iloc[-1]),
                                    self.gaps.get(t, 0.0), self.pm_end)
        return out


class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status
        self.text = json.dumps(payload)[:200]

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")


class FakeFinnhubSession:
    """Answers /company-news, /calendar/earnings and /news like Finnhub."""

    def __init__(self, as_of: datetime, heavy: dict | None = None, earnings: list | None = None,
                 fail_tickers: set | None = None):
        self.as_of = as_of
        self.heavy = heavy or {"TSLA": 14}
        self.earnings = earnings or []
        self.fail = fail_tickers or set()
        self.requests: list = []

    def get(self, url, params=None, timeout=None):
        self.requests.append((url, dict(params or {})))
        ts = int((self.as_of - timedelta(hours=3)).timestamp())
        old = int((self.as_of - timedelta(hours=40)).timestamp())
        if url.endswith("/company-news"):
            sym = params["symbol"]
            if sym in self.fail:
                return FakeResp({"error": "boom"}, 500)
            n = self.heavy.get(sym, 2 if sym not in ("SPY", "QQQ", "IWM") else 0)
            items = [{"datetime": ts - i * 600, "headline": f"{sym} headline number {i}",
                      "source": "Reuters", "url": f"https://example.com/{sym}/{i}"} for i in range(n)]
            # duplicate cross-listed story and an old story outside 24h
            items.append({"datetime": ts, "headline": "Chip stocks rally as AI demand stays strong",
                          "source": "Bloomberg", "url": "https://example.com/chips"})
            items.append({"datetime": old, "headline": f"{sym} old story", "source": "X", "url": ""})
            return FakeResp(items)
        if url.endswith("/calendar/earnings"):
            return FakeResp({"earningsCalendar": self.earnings})
        if url.endswith("/news"):
            return FakeResp([{"datetime": ts, "headline": "Fed minutes signal patience on rates",
                              "source": "CNBC", "url": "https://example.com/fed"}])
        return FakeResp({}, 404)


class ScriptedClaude:
    """Returns a fixed sequence of Messages API responses."""

    def __init__(self, responses: list, model: str = "fake-model"):
        self.responses = list(responses)
        self.model = model
        self.calls: list = []

    def create(self, **kw):
        self.calls.append(kw)
        if not self.responses:
            raise RuntimeError("no more scripted responses")
        r = self.responses.pop(0)
        return r(kw) if callable(r) else r


def tool_use(name, inp, uid="t1", text="Checking."):
    return {"stop_reason": "tool_use", "content": [
        {"type": "text", "text": text},
        {"type": "tool_use", "id": uid, "name": name, "input": inp}]}


def final(obj):
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(obj)}]}
