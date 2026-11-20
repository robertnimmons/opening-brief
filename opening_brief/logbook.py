"""Append each day's table and focus list to logs/brief_log.csv."""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

from .analysis import Analysis

FIELDS = [
    "date", "as_of_et", "ticker", "available", "error", "is_index", "market_bias",
    "prior_date", "prior_close", "ema8", "ema8_status", "consolidation_days", "atr14",
    "pm_high", "pm_low", "pm_last", "pm_volume", "gap_pct", "gap_source", "rs_vs_spy", "rank",
    "news_count", "heavy_news", "earnings", "is_candidate", "is_focus", "focus_rank",
    "focus_source", "focus_reason",
]


def already_logged(path: Path, d: date) -> bool:
    if not path.exists():
        return False
    with open(path, newline="", encoding="utf-8") as fh:
        return any(row.get("date") == d.isoformat() for row in csv.DictReader(fh))


def append_log(path: Path, a: Analysis, as_of: datetime) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    n = 0
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for r in a.ranked():
            w.writerow({
                "date": a.brief_date.isoformat(), "as_of_et": as_of.strftime("%H:%M"),
                "ticker": r.ticker, "available": r.available, "error": r.error or "",
                "is_index": r.is_index, "market_bias": a.bias,
                "prior_date": r.prior_date or "", "prior_close": r.prior_close, "ema8": r.ema8,
                "ema8_status": r.status, "consolidation_days": r.consolidation_days,
                "atr14": r.atr14, "pm_high": r.pm_high, "pm_low": r.pm_low, "pm_last": r.pm_last,
                "pm_volume": r.pm_volume, "gap_pct": r.gap_pct, "gap_source": r.gap_source or "",
                "rs_vs_spy": r.rs_vs_spy, "rank": r.rank, "news_count": r.news_count,
                "heavy_news": r.heavy_news, "earnings": r.earnings or "",
                "is_candidate": r.ticker in a.candidates, "is_focus": r.is_focus,
                "focus_rank": r.focus_rank or "", "focus_source": a.focus_source if r.is_focus else "",
                "focus_reason": r.focus_reason or "",
            })
            n += 1
    return n
