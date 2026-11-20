"""Check the brief's numbers against an independent calculation on live data.

    python -m opening_brief.verify NVDA AAPL SPY

The independent side uses a different yfinance path (Ticker.history instead
of the batch download) and pandas ewm instead of our EMA loop. It prints both
values side by side and exits with code 1 if anything disagrees beyond
rounding. Compare the 8 EMA with TradingView's daily EMA(8) too: it should
match to the cent.
"""
from __future__ import annotations

import sys
from datetime import datetime, time

import pandas as pd

from .analysis import build_analysis
from .config import load_config
from .data import YFinanceProvider, load_market_data
from .market_calendar import ET, now_et


def independent(ticker: str, as_of: datetime) -> dict:
    import yfinance as yf

    tk = yf.Ticker(ticker)
    daily = tk.history(period="2y", interval="1d", auto_adjust=False)
    dates = pd.DatetimeIndex(daily.index).tz_localize(None).normalize().date if daily.index.tz is not None \
        else pd.DatetimeIndex(daily.index).normalize().date
    daily = daily[dates < as_of.date()]
    closes = daily["Close"].reset_index(drop=True)
    seeded = pd.concat([pd.Series([closes.iloc[:8].mean()]), closes.iloc[8:]], ignore_index=True)
    ema8 = seeded.ewm(alpha=2 / 9, adjust=False).mean().iloc[-1]

    pm = tk.history(period="1d", interval="1m", prepost=True)
    gap = pm_high = pm_low = None
    if not pm.empty:
        pm.index = pm.index.tz_convert(ET)
        start = datetime.combine(as_of.date(), time(4, 0), tzinfo=ET)
        cutoff = min(as_of, datetime.combine(as_of.date(), time(9, 30), tzinfo=ET))
        pm = pm[(pm.index >= start) & (pm.index < cutoff)]
        if not pm.empty:
            gap = (pm["Close"].iloc[-1] / closes.iloc[-1] - 1) * 100
            pm_high, pm_low = pm["High"].max(), pm["Low"].min()
    return {"prior_close": closes.iloc[-1], "ema8": ema8, "gap_pct": gap,
            "pm_high": pm_high, "pm_low": pm_low}


def main(argv=None) -> int:
    tickers = [t.upper() for t in (argv or sys.argv[1:])] or ["NVDA", "AAPL", "SPY"]
    cfg = load_config()
    as_of = now_et()
    universe = sorted(set(tickers) | {"SPY", "QQQ"})
    data = load_market_data(YFinanceProvider(), universe, as_of.date(), as_of)
    a = build_analysis(data, as_of.date(), cfg.analysis, cfg.index_tickers)

    tol = {"prior_close": 0.006, "ema8": 0.006, "gap_pct": 0.006, "pm_high": 0.006, "pm_low": 0.006}
    failed = False
    print(f"As of {as_of:%Y-%m-%d %H:%M} ET\n")
    print(f"{'ticker':<7}{'field':<12}{'brief':>12}{'independent':>14}  ok")
    for t in tickers:
        r = a.rows[t]
        ind = independent(t, as_of)
        for f in ("prior_close", "ema8", "gap_pct", "pm_high", "pm_low"):
            ours, theirs = getattr(r, f), ind[f]
            if ours is None and theirs is None:
                ok = True
            elif ours is None or theirs is None:
                ok = False
            else:
                ok = abs(ours - theirs) <= tol[f]
            failed |= not ok
            fmt = lambda v: "n/a" if v is None else f"{v:.4f}"  # noqa: E731
            print(f"{t:<7}{f:<12}{fmt(ours):>12}{fmt(theirs):>14}  {'yes' if ok else 'NO'}")
        print(f"{t:<7}{'status':<12}{r.status:>12}")
    print("\nAll numbers match." if not failed else "\nMISMATCH found: see rows marked NO.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
