"""NYSE full-day holidays and the run-time window check.

Rules follow the NYSE holiday schedule: Saturday holidays are observed on the
Friday before and Sunday holidays on the Monday after, except New Year's Day
on a Saturday, which is not observed (NYSE stays open on Dec 31).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    # Anonymous Gregorian algorithm
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> dict[date, str]:
    hol: dict[date, str] = {}
    ny = date(year, 1, 1)
    if ny.weekday() != 5:  # Saturday New Year's is not observed
        hol[_observed(ny)] = "New Year's Day"
    hol[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    hol[_nth_weekday(year, 2, 0, 3)] = "Presidents' Day"
    hol[_easter(year) - timedelta(days=2)] = "Good Friday"
    hol[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        hol[_observed(date(year, 6, 19))] = "Juneteenth"
    hol[_observed(date(year, 7, 4))] = "Independence Day"
    hol[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    hol[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    hol[_observed(date(year, 12, 25))] = "Christmas Day"
    return hol


def closure_reason(d: date, extra_closures: list[str] | None = None) -> str | None:
    """Return why the market is closed on d, or None if it is a trading day."""
    if d.weekday() >= 5:
        return "weekend"
    if extra_closures and d.isoformat() in extra_closures:
        return "special closure"
    return nyse_holidays(d.year).get(d)


def is_trading_day(d: date, extra_closures: list[str] | None = None) -> bool:
    return closure_reason(d, extra_closures) is None


def previous_trading_day(d: date, extra_closures: list[str] | None = None) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d, extra_closures):
        d -= timedelta(days=1)
    return d


def week_bounds(d: date) -> tuple[date, date]:
    """Monday..Friday of the week containing d."""
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=4)


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_window(now_et: datetime, start: str, end: str) -> bool:
    t = now_et.astimezone(ET).time()
    return _parse_hhmm(start) <= t <= _parse_hhmm(end)


def now_et() -> datetime:
    return datetime.now(tz=ET)
