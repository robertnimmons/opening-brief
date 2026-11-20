import unittest
from datetime import date, datetime

from opening_brief.market_calendar import (ET, closure_reason, in_window, nyse_holidays,
                                           previous_trading_day)


class TestHolidays(unittest.TestCase):
    def test_2026(self):
        expected = {
            date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
            date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
            date(2026, 11, 26), date(2026, 12, 25),
        }
        self.assertEqual(set(nyse_holidays(2026)), expected)

    def test_2025(self):
        expected = {
            date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
            date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
            date(2025, 11, 27), date(2025, 12, 25),
        }
        self.assertEqual(set(nyse_holidays(2025)), expected)

    def test_weekend_observance_rules(self):
        h27 = nyse_holidays(2027)
        self.assertIn(date(2027, 6, 18), h27)    # Juneteenth Sat -> Fri
        self.assertIn(date(2027, 12, 24), h27)   # Christmas Sat -> Fri
        self.assertIn(date(2027, 7, 5), h27)     # July 4 Sun -> Mon
        h28 = nyse_holidays(2028)
        self.assertNotIn(date(2027, 12, 31), h28)  # New Year's on Sat is not observed
        self.assertFalse(any(d.month == 1 and d.day == 1 for d in h28))

    def test_closure_reason(self):
        self.assertEqual(closure_reason(date(2026, 11, 26)), "Thanksgiving Day")
        self.assertEqual(closure_reason(date(2026, 11, 21)), "weekend")
        self.assertIsNone(closure_reason(date(2026, 11, 27)))  # half day, still opens 9:30
        self.assertEqual(closure_reason(date(2025, 1, 9), ["2025-01-09"]), "special closure")

    def test_previous_trading_day(self):
        self.assertEqual(previous_trading_day(date(2026, 11, 27)), date(2026, 11, 25))
        self.assertEqual(previous_trading_day(date(2026, 11, 23)), date(2026, 11, 20))
        self.assertEqual(previous_trading_day(date(2026, 1, 20)), date(2026, 1, 16))


class TestWindow(unittest.TestCase):
    def test_window_edges_and_dst(self):
        w = ("08:25", "08:50")
        self.assertTrue(in_window(datetime(2026, 11, 20, 8, 30, tzinfo=ET), *w))
        self.assertFalse(in_window(datetime(2026, 11, 20, 7, 30, tzinfo=ET), *w))
        self.assertFalse(in_window(datetime(2026, 11, 20, 8, 51, tzinfo=ET), *w))
        # 13:30 UTC is 8:30 in EST (Nov) and 9:30 in EDT (Jul)
        from zoneinfo import ZoneInfo
        utc = ZoneInfo("UTC")
        self.assertTrue(in_window(datetime(2026, 11, 20, 13, 30, tzinfo=utc), *w))
        self.assertFalse(in_window(datetime(2026, 7, 20, 13, 30, tzinfo=utc), *w))
        self.assertTrue(in_window(datetime(2026, 7, 20, 12, 30, tzinfo=utc), *w))


if __name__ == "__main__":
    unittest.main()
