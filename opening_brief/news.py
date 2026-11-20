"""Finnhub news and earnings calendar (free tier).

One company-news request per ticker, one earnings-calendar request for the
week, and at most one general market-news request (only if the agent asks).
Each request is retried once with backoff; failures degrade to "unavailable"
rather than stopping the brief.
"""
from __future__ import annotations

import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import requests

from .analysis import Analysis
from .market_calendar import ET, week_bounds
from .util import log

BASE = "https://finnhub.io/api/v1"


@dataclass
class Headline:
    id: str
    tickers: list[str]
    headline: str
    source: str
    url: str
    published: datetime   # ET


@dataclass
class EarningsEvent:
    ticker: str
    date: date
    hour: str  # "bmo" | "amc" | "dmh" | ""


@dataclass
class NewsBundle:
    available: bool = True
    error: str | None = None
    by_ticker: dict[str, list[Headline]] = field(default_factory=dict)
    all: list[Headline] = field(default_factory=list)
    earnings: dict[str, EarningsEvent] = field(default_factory=dict)
    earnings_available: bool = True
    market: list[Headline] | None = None  # filled lazily


def normalize_headline(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


class FinnhubClient:
    DEFAULT_PAUSE = 0.25   # seconds between requests; free tier allows 60/min
    DEFAULT_BACKOFF = 2.0

    def __init__(self, api_key: str | None, session: requests.Session | None = None,
                 pause: float | None = None, backoff: float | None = None, timeout: float = 15.0):
        self.key = api_key
        self.s = session or requests.Session()
        self.pause = self.DEFAULT_PAUSE if pause is None else pause
        self.backoff = self.DEFAULT_BACKOFF if backoff is None else backoff
        self.timeout = timeout

    def get(self, path: str, **params):
        if not self.key:
            raise RuntimeError("FINNHUB_API_KEY not set")
        params["token"] = self.key
        for attempt in (1, 2):
            try:
                r = self.s.get(f"{BASE}{path}", params=params, timeout=self.timeout)
                if r.status_code == 429 and attempt == 1:
                    log.warning("Finnhub rate limited on %s; backing off", path)
                    time.sleep(self.backoff * 2)
                    continue
                r.raise_for_status()
                time.sleep(self.pause)  # stay well under 60 req/min
                return r.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt == 2:
                    raise
                log.warning("Finnhub %s failed (%s); retrying once", path, exc)
                time.sleep(self.backoff)
        return None


def _parse_items(items, ticker: str | None, since: datetime, until: datetime) -> list[dict]:
    out = []
    for it in items or []:
        try:
            ts = datetime.fromtimestamp(int(it["datetime"]), tz=ET)
        except (KeyError, TypeError, ValueError):
            continue
        if not (since <= ts <= until):
            continue
        head = (it.get("headline") or "").strip()
        if not head:
            continue
        out.append({
            "ticker": ticker, "headline": head, "source": it.get("source", ""),
            "url": it.get("url", ""), "published": ts,
        })
    return out


def dedupe(raw: list[dict], prefix: str = "h") -> list[Headline]:
    """Merge identical headlines (after normalization) and track every ticker they mention."""
    seen: dict[str, Headline] = {}
    for item in sorted(raw, key=lambda x: x["published"], reverse=True):
        key = normalize_headline(item["headline"])
        if key in seen:
            h = seen[key]
            if item["ticker"] and item["ticker"] not in h.tickers:
                h.tickers.append(item["ticker"])
            continue
        seen[key] = Headline(
            id="", tickers=[item["ticker"]] if item["ticker"] else [],
            headline=item["headline"], source=item["source"], url=item["url"],
            published=item["published"],
        )
    out = list(seen.values())
    for i, h in enumerate(out, 1):
        h.id = f"{prefix}{i}"
    return out


def fetch_news(client: FinnhubClient, tickers: list[str], as_of: datetime, lookback_hours: int,
               brief_date: date) -> NewsBundle:
    bundle = NewsBundle()
    if not client.key:
        bundle.available = False
        bundle.earnings_available = False
        bundle.error = "FINNHUB_API_KEY not set"
        return bundle

    since = as_of - timedelta(hours=lookback_hours)
    raw: list[dict] = []
    failed = []
    for t in tickers:
        try:
            items = client.get("/company-news", symbol=t,
                               **{"from": since.date().isoformat(), "to": as_of.date().isoformat()})
            raw.extend(_parse_items(items, t, since, as_of))
        except Exception as exc:  # noqa: BLE001
            log.warning("News failed for %s: %s", t, exc)
            failed.append(t)
    if failed and len(failed) == len(tickers):
        bundle.available = False
        bundle.error = "Finnhub news unavailable"
    bundle.all = dedupe(raw)
    for h in bundle.all:
        for t in h.tickers:
            bundle.by_ticker.setdefault(t, []).append(h)

    mon, fri = week_bounds(brief_date)
    try:
        cal = client.get("/calendar/earnings", **{"from": mon.isoformat(), "to": fri.isoformat()})
        wanted = set(tickers)
        for e in (cal or {}).get("earningsCalendar", []):
            sym = (e.get("symbol") or "").upper()
            if sym in wanted:
                try:
                    d = date.fromisoformat(e["date"])
                except (KeyError, ValueError):
                    continue
                if d >= brief_date and sym not in bundle.earnings:
                    bundle.earnings[sym] = EarningsEvent(sym, d, (e.get("hour") or "").lower())
    except Exception as exc:  # noqa: BLE001
        log.warning("Earnings calendar failed: %s", exc)
        bundle.earnings_available = False
    return bundle


def fetch_market_news(client: FinnhubClient, as_of: datetime, lookback_hours: int) -> list[Headline]:
    since = as_of - timedelta(hours=lookback_hours)
    items = client.get("/news", category="general")
    return dedupe(_parse_items(items, None, since, as_of), prefix="m")[:15]


HOUR_LABEL = {"bmo": "BMO", "amc": "AMC", "dmh": "intraday"}


def apply_news_flags(a: Analysis, bundle: NewsBundle, cfg: dict) -> None:
    counts = {t: len(bundle.by_ticker.get(t, [])) for t in a.rows}
    stock_counts = [c for t, c in counts.items() if not a.rows[t].is_index]
    median = statistics.median(stock_counts) if stock_counts else 0
    for t, r in a.rows.items():
        r.news_count = counts[t]
        r.heavy_news = (
            bundle.available
            and counts[t] >= cfg.get("heavy_news_min", 8)
            and counts[t] >= cfg.get("heavy_news_multiple", 2.5) * max(median, 1)
        )
        ev = bundle.earnings.get(t)
        if ev:
            r.earnings_today = ev.date == a.brief_date
            day = "today" if r.earnings_today else ev.date.strftime("%a")
            hour = HOUR_LABEL.get(ev.hour, "")
            r.earnings = f"{day} {hour}".strip()
