"""Turn raw price data into the analysis table, market bias and focus list."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date

from . import indicators as ind
from .data import TickerData
from .market_calendar import previous_trading_day

STATUS_TEXT = {
    "above": "holding above 8EMA",
    "below": "rejected below 8EMA",
    "chop": "chopping around 8EMA",
    "unknown": "8EMA n/a",
}
STATUS_CODE = {"above": "ABV", "below": "BLW", "chop": "CHP", "unknown": "--"}


@dataclass
class Row:
    ticker: str
    available: bool = True
    error: str | None = None
    is_index: bool = False
    prior_date: str | None = None
    prior_close: float | None = None
    ema8: float | None = None
    status: str = "unknown"
    consolidation_days: int = 0
    atr14: float | None = None
    pm_high: float | None = None
    pm_low: float | None = None
    pm_last: float | None = None
    pm_volume: int | None = None
    gap_pct: float | None = None
    gap_source: str | None = None   # "premarket" | "open" | None
    rs_vs_spy: float | None = None
    rank: int | None = None
    stale_note: str | None = None
    # filled in by the news step
    news_count: int = 0
    heavy_news: bool = False
    earnings: str | None = None        # e.g. "Thu AMC"
    earnings_today: bool = False
    # filled in by the focus step
    is_focus: bool = False
    focus_rank: int | None = None
    focus_reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Analysis:
    brief_date: date
    rows: dict[str, Row]
    bias: str                       # "Bullish" | "Bearish" | "Mixed" | "Unknown"
    bias_detail: str
    candidates: list[str] = field(default_factory=list)  # trend-aligned names, best first
    focus: list[str] = field(default_factory=list)
    focus_source: str = "rules"

    def ranked(self) -> list[Row]:
        """Non-index rows by RS (strongest first), unavailable last, then indexes."""
        stocks = [r for r in self.rows.values() if not r.is_index]
        avail = [r for r in stocks if r.available and r.rs_vs_spy is not None]
        avail.sort(key=lambda r: r.rs_vs_spy, reverse=True)
        rest = [r for r in stocks if r not in avail]
        idx = [r for r in self.rows.values() if r.is_index]
        return avail + rest + idx


def _r(x: float | None, nd: int = 2) -> float | None:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return round(float(x), nd)


def analyze_ticker(td: TickerData, brief_date: date, cfg: dict, is_index: bool,
                   extra_closures: list[str] | None = None) -> Row:
    row = Row(ticker=td.ticker, is_index=is_index)
    if td.error or td.daily is None:
        row.available = False
        row.error = td.error or "no data"
        return row

    d = td.daily
    high, low, close = list(d["High"]), list(d["Low"]), list(d["Close"])
    emas = ind.ema(close, cfg.get("ema_length", 8))
    atrs = ind.atr(high, low, close, cfg.get("atr_length", 14))

    row.prior_date = d.index[-1].date().isoformat()
    row.prior_close = _r(close[-1], 4)
    row.ema8 = _r(emas[-1], 4)
    row.atr14 = _r(atrs[-1], 4)
    row.status = ind.ema_status(close, emas, cfg.get("status_lookback", 3))
    row.consolidation_days = ind.consolidation_days(
        high, low, close, emas, atrs[-1],
        cfg.get("consolidation_min_days", 2), cfg.get("consolidation_max_days", 4),
        cfg.get("consolidation_close_atr", 0.5), cfg.get("consolidation_range_atr", 1.5),
    )

    expected_prior = previous_trading_day(brief_date, extra_closures)
    stale = d.index[-1].date() != expected_prior
    if stale:
        row.stale_note = f"last daily bar is {row.prior_date}, expected {expected_prior.isoformat()}"

    lv = ind.premarket_levels(td.premarket)
    row.pm_high = _r(lv["pm_high"], 4)
    row.pm_low = _r(lv["pm_low"], 4)
    row.pm_last = _r(lv["pm_last"], 4)
    row.pm_volume = lv["pm_volume"]
    if stale:
        # A gap against the wrong prior close would be silently wrong, so leave it blank.
        pass
    elif lv["pm_last"] is not None:
        row.gap_pct = _r(ind.pct_change(lv["pm_last"], close[-1]), 2)
        row.gap_source = "premarket"
    elif td.session_open is not None:
        row.gap_pct = _r(ind.pct_change(td.session_open, close[-1]), 2)
        row.gap_source = "open"
    return row


def market_bias(rows: dict[str, Row]) -> tuple[str, str]:
    spy, qqq = rows.get("SPY"), rows.get("QQQ")
    parts = []
    states = []
    for r in (spy, qqq):
        if r is None or not r.available:
            parts.append(f"{r.ticker if r else '?'} unavailable")
            states.append("unknown")
        else:
            parts.append(f"{r.ticker} {STATUS_TEXT[r.status]}")
            states.append(r.status)
    if "unknown" in states:
        return "Unknown", "; ".join(parts)
    if all(s == "above" for s in states):
        return "Bullish", "; ".join(parts)
    if all(s == "below" for s in states):
        return "Bearish", "; ".join(parts)
    return "Mixed", "; ".join(parts)


def _aligned(row: Row, direction: str) -> bool:
    """Is this name's own daily 8EMA picture in line with `direction`?"""
    if direction == "long":
        return row.status == "above" or (
            row.consolidation_days > 0 and row.prior_close is not None
            and row.ema8 is not None and row.prior_close >= row.ema8
        )
    return row.status == "below" or (
        row.consolidation_days > 0 and row.prior_close is not None
        and row.ema8 is not None and row.prior_close < row.ema8
    )


def focus_candidates(rows: dict[str, Row], bias: str, cfg: dict) -> list[tuple[str, str]]:
    """Trend-aligned names ordered best first, as (ticker, direction) pairs."""
    min_rs = cfg.get("min_focus_rs", 0.3)
    stocks = [r for r in rows.values() if not r.is_index and r.available and r.rs_vs_spy is not None]
    longs = sorted(
        [r for r in stocks if r.rs_vs_spy >= min_rs and _aligned(r, "long")],
        key=lambda r: r.rs_vs_spy, reverse=True,
    )
    shorts = sorted(
        [r for r in stocks if r.rs_vs_spy <= -min_rs and _aligned(r, "short")],
        key=lambda r: r.rs_vs_spy,
    )
    if bias == "Bullish":
        return [(r.ticker, "long") for r in longs]
    if bias == "Bearish":
        return [(r.ticker, "short") for r in shorts]
    if bias == "Mixed":
        merged = [(r, "long") for r in longs] + [(r, "short") for r in shorts]
        merged.sort(key=lambda p: abs(p[0].rs_vs_spy), reverse=True)
        return [(r.ticker, d) for r, d in merged]
    return []


def rule_reason(row: Row) -> str:
    """Deterministic one-liner built only from table values."""
    bits = []
    if row.rs_vs_spy is not None:
        bits.append(f"RS {row.rs_vs_spy:+.1f} vs SPY")
    if row.gap_pct is not None:
        bits.append(f"gap {row.gap_pct:+.1f}%")
    bits.append(STATUS_TEXT[row.status])
    if row.consolidation_days:
        bits.append(f"{row.consolidation_days}-day coil on 8EMA")
    if row.earnings:
        bits.append(f"earnings {row.earnings}")
    if row.heavy_news:
        bits.append(f"heavy news ({row.news_count} headlines)")
    return ", ".join(bits)


def build_analysis(data: dict[str, TickerData], brief_date: date, cfg_analysis: dict,
                   index_tickers: list[str], extra_closures: list[str] | None = None) -> Analysis:
    rows = {
        t: analyze_ticker(td, brief_date, cfg_analysis, t in index_tickers, extra_closures)
        for t, td in data.items()
    }
    spy = rows.get("SPY")
    spy_gap = spy.gap_pct if spy and spy.available else None
    for r in rows.values():
        if r.available and r.gap_pct is not None and spy_gap is not None:
            r.rs_vs_spy = _r(r.gap_pct - spy_gap, 2)

    bias, detail = market_bias(rows)
    a = Analysis(brief_date=brief_date, rows=rows, bias=bias, bias_detail=detail)
    for i, r in enumerate([r for r in a.ranked() if not r.is_index and r.rs_vs_spy is not None], 1):
        r.rank = i
    return a


def apply_rule_focus(a: Analysis, cfg: dict) -> None:
    """Fill candidates and the rule-based focus list. Call after news flags are set."""
    pairs = focus_candidates(a.rows, a.bias, cfg)
    a.candidates = [t for t, _ in pairs][: cfg.get("agent_candidates", 5)]
    set_focus(a, [t for t, _ in pairs][: cfg.get("max_focus", 2)], {}, "rules")


def set_focus(a: Analysis, tickers: list[str], reasons: dict[str, str], source: str) -> None:
    for r in a.rows.values():
        r.is_focus, r.focus_rank, r.focus_reason = False, None, None
    for i, t in enumerate(tickers, 1):
        r = a.rows[t]
        r.is_focus = True
        r.focus_rank = i
        r.focus_reason = reasons.get(t) or rule_reason(r)
    a.focus = list(tickers)
    a.focus_source = source


def direction_of(row: Row) -> str:
    return "strength" if (row.rs_vs_spy or 0) >= 0 else "weakness"
