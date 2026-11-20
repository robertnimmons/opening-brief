import math
import unittest
from datetime import date, datetime, time

import pandas as pd

from opening_brief import indicators as ind
from opening_brief.data import slice_premarket
from opening_brief.market_calendar import ET


class TestEMA(unittest.TestCase):
    def test_hand_computed(self):
        # length 3: seed = SMA(1,2,3) = 2; k = 0.5
        # 4 -> 3.0 ; 5 -> 4.0 ; 10 -> 7.0
        self.assertEqual(ind.ema([1, 2, 3, 4, 5, 10], 3)[2:], [2.0, 3.0, 4.0, 7.0])
        self.assertTrue(math.isnan(ind.ema([1, 2, 3], 3)[0]))

    def test_matches_pandas_ewm_after_warmup(self):
        # Independent implementation: pandas ewm(adjust=False) seeded on the
        # first value. Over 250 bars both seeds converge to the same EMA.
        closes = [100 + 10 * math.sin(i / 7) + i * 0.05 for i in range(250)]
        ours = ind.ema(closes, 8)[-1]
        theirs = pd.Series(closes).ewm(span=8, adjust=False).mean().iloc[-1]
        self.assertAlmostEqual(ours, theirs, places=8)


class TestATR(unittest.TestCase):
    def test_constant_range(self):
        h = [11.0] * 20
        l = [9.0] * 20
        c = [10.0] * 20
        self.assertAlmostEqual(ind.atr(h, l, c, 14)[-1], 2.0)


class TestStatus(unittest.TestCase):
    def test_above_below_chop(self):
        self.assertEqual(ind.ema_status([11, 12, 13], [10, 10, 10]), "above")
        self.assertEqual(ind.ema_status([9, 8, 7], [10, 10, 10]), "below")
        self.assertEqual(ind.ema_status([11, 9, 11], [10, 10, 10]), "chop")
        self.assertEqual(ind.ema_status([11, 12], [10, 10], 3), "unknown")


class TestConsolidation(unittest.TestCase):
    def test_tight_on_ema(self):
        e = [100.0] * 6
        h = [104, 103, 100.6, 100.5, 100.7, 100.4]
        l = [98, 97, 99.6, 99.5, 99.7, 99.6]
        c = [103, 98, 100.1, 99.9, 100.2, 100.0]
        # ATR 2: last 4 days range 100.7-99.5 = 1.2 <= 3, closes within 1 of EMA
        self.assertEqual(ind.consolidation_days(h, l, c, e, 2.0), 4)

    def test_wide_or_off_ema(self):
        e = [100.0] * 4
        self.assertEqual(ind.consolidation_days([110] * 4, [90] * 4, [100] * 4, e, 2.0), 0)
        self.assertEqual(ind.consolidation_days([106] * 4, [105] * 4, [105.5] * 4, e, 2.0), 0)


class TestGapAndPremarket(unittest.TestCase):
    def test_pct_change(self):
        self.assertAlmostEqual(ind.pct_change(102.0, 100.0), 2.0)
        self.assertAlmostEqual(ind.pct_change(97.5, 100.0), -2.5)

    def test_slice_excludes_overnight_and_after_cutoff(self):
        d = date(2026, 11, 20)
        idx = pd.DatetimeIndex([
            datetime(2026, 11, 20, 3, 59, tzinfo=ET),   # before 04:00
            datetime(2026, 11, 20, 4, 0, tzinfo=ET),
            datetime(2026, 11, 20, 8, 29, tzinfo=ET),
            datetime(2026, 11, 20, 8, 30, tzinfo=ET),   # at/after as_of
            datetime(2026, 11, 20, 9, 31, tzinfo=ET),   # regular session
        ])
        df = pd.DataFrame({"Open": [1, 2, 3, 4, 5], "High": [50, 2, 3, 99, 99],
                           "Low": [0.1, 2, 3, 0.1, 0.1], "Close": [1, 2, 3, 4, 5],
                           "Volume": [1, 1, 1, 1, 1]}, index=idx)
        s = slice_premarket(df, d, datetime(2026, 11, 20, 8, 30, tzinfo=ET))
        self.assertEqual(list(s["Close"]), [2, 3])
        lv = ind.premarket_levels(s)
        self.assertEqual((lv["pm_high"], lv["pm_low"], lv["pm_last"]), (3.0, 2.0, 3.0))

    def test_slice_handles_utc_index(self):
        d = date(2026, 11, 20)
        idx = pd.DatetimeIndex(["2026-11-20 13:00", "2026-11-20 13:29"]).tz_localize("UTC")  # 8:00, 8:29 ET
        df = pd.DataFrame({"Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [1, 2],
                           "Volume": [1, 1]}, index=idx)
        s = slice_premarket(df, d, datetime(2026, 11, 20, 8, 30, tzinfo=ET))
        self.assertEqual(len(s), 2)


if __name__ == "__main__":
    unittest.main()
