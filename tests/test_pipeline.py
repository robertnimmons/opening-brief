"""End-to-end tests of run() with fake data sources."""
import csv
import json
import shutil
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

from opening_brief import main as m
from opening_brief.config import ROOT, Secrets
from opening_brief.formatting import split_messages
from opening_brief.indicators import ema
from opening_brief.market_calendar import ET
from opening_brief.news import FinnhubClient
from tests.fakes import (DEFAULT_GAPS, FakeFinnhubSession, FakeProvider, ScriptedClaude, final,
                         make_daily, tool_use)

D = date(2026, 11, 20)            # a Friday
NOW = datetime(2026, 11, 20, 8, 31, tzinfo=ET)


def trending(ticker, end, up=True):
    """Daily bars whose last 4 closes push firmly away from the 8 EMA."""
    df = make_daily(ticker, end)
    hist = df.index.date < end
    pos = [i for i, h in enumerate(hist) if h][-4:]
    for i in pos:
        prev = df["Close"].iloc[i - 1]
        c = prev * (1.02 if up else 0.98)
        df.iloc[i, df.columns.get_loc("Open")] = prev
        df.iloc[i, df.columns.get_loc("Close")] = c
        df.iloc[i, df.columns.get_loc("High")] = max(prev, c) * 1.003
        df.iloc[i, df.columns.get_loc("Low")] = min(prev, c) * 0.997
    return df


def bullish_overrides(end=D):
    return {t: trending(t, end) for t in ("SPY", "QQQ", "NVDA", "AMD", "MU", "AVGO", "GOOGL")}


FinnhubClient.DEFAULT_PAUSE = 0.0
FinnhubClient.DEFAULT_BACKOFF = 0.01


def read_csv(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copy(ROOT / "config.toml", self.tmp / "config.toml")
        self.cfg = str(self.tmp / "config.toml")
        self.lines: list[str] = []
        self.sent: list[list[str]] = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_brief(self, *argv, provider=None, secrets=None, claude=None, now=NOW, finnhub=None):
        provider = provider or FakeProvider(daily_overrides=bullish_overrides())
        secrets = secrets if secrets is not None else Secrets(finnhub="x", anthropic=None,
                                                              discord="https://discord.test/hook")
        code = m.run(["--config", self.cfg, *argv], provider=provider,
                     finnhub_session=finnhub or FakeFinnhubSession(now),
                     anthropic_client=claude, now=now, secrets=secrets,
                     poster=lambda url, msgs: self.sent.append(msgs), out=self.lines.append)
        return code, "\n".join(self.lines)


class TestDryRun(Harness):
    def test_full_message_shape(self):
        code, text = self.run_brief("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.sent, [])
        self.assertIn("**Opening Brief · Fri Nov 20**", text)
        self.assertIn("Bias: **Bullish**", text)
        self.assertIn("Check them on the chart at 9:15", text)
        for t in DEFAULT_GAPS:
            self.assertRegex(text, rf"\n{t}\*?\s")
        # focus first, then table, then headlines
        self.assertLess(text.index("**Focus**"), text.index("```"))
        self.assertLess(text.index("```"), text.index("**Headlines**"))
        print("\n" + text)

    def test_messages_respect_discord_limit(self):
        self.run_brief("--dry-run")
        sizes = [int(l.split("(")[1].split()[0]) for l in self.lines if l.startswith("----- message")]
        self.assertTrue(sizes and all(s <= 2000 for s in sizes))


class TestNumbers(Harness):
    def test_gap_ema_prior_close_independent(self):
        """Recompute prior close, 8 EMA and gap with pandas from the raw fake bars."""
        prov = FakeProvider(daily_overrides=bullish_overrides())
        from opening_brief.data import load_market_data
        from opening_brief.analysis import build_analysis
        data = load_market_data(prov, list(DEFAULT_GAPS), D, NOW)
        a = build_analysis(data, D, {}, ["SPY", "QQQ", "IWM"])
        for t in ("NVDA", "AAPL", "COIN", "SPY"):
            raw = prov._daily[t]
            hist = raw[raw.index.date < D]                  # the partial "today" row must be ignored
            prior_close = hist["Close"].iloc[-1]
            # independent EMA: SMA seed then the recursive formula via pandas
            closes = hist["Close"]
            seeded = pd.concat([pd.Series([closes.iloc[:8].mean()]), closes.iloc[8:]], ignore_index=True)
            ema_ind = seeded.ewm(alpha=2 / 9, adjust=False).mean().iloc[-1]
            pm = prov.premarket([t], D)[t]
            pm = pm[pm.index < NOW]
            gap_ind = (pm["Close"].iloc[-1] / prior_close - 1) * 100
            r = a.rows[t]
            self.assertAlmostEqual(r.prior_close, prior_close, places=3, msg=t)
            self.assertAlmostEqual(r.ema8, ema_ind, places=3, msg=t)
            self.assertAlmostEqual(r.gap_pct, round(gap_ind, 2), places=6, msg=t)
            self.assertAlmostEqual(r.pm_high, round(pm["High"].max(), 4), places=6, msg=t)
            self.assertAlmostEqual(r.pm_low, round(pm["Low"].min(), 4), places=6, msg=t)
            self.assertAlmostEqual(r.ema8, ema(list(closes), 8)[-1], places=3)
        spy_gap = a.rows["SPY"].gap_pct
        self.assertAlmostEqual(a.rows["NVDA"].rs_vs_spy, round(a.rows["NVDA"].gap_pct - spy_gap, 2))


class TestFailures(Harness):
    def test_failed_ticker_still_sends(self):
        prov = FakeProvider(daily_overrides=bullish_overrides(), fail={"COIN"})
        code, text = self.run_brief(provider=prov)
        self.assertEqual(code, 0)
        body = "\n".join(self.sent[0])
        self.assertRegex(body, r"\nCOIN\s+unavailable")
        self.assertIn("Unavailable (no daily data): COIN", body)
        self.assertIn("NVDA", body)
        rows = read_csv(self.tmp / "logs" / "brief_log.csv")
        coin = [r for r in rows if r["ticker"] == "COIN"][0]
        self.assertEqual(coin["available"], "False")

    def test_everything_fails_still_sends(self):
        prov = FakeProvider(fail=set(DEFAULT_GAPS))
        code, _ = self.run_brief(provider=prov)
        self.assertEqual(code, 0)
        self.assertIn("Market bias unavailable", "\n".join(self.sent[0]))

    def test_missing_keys(self):
        none = Secrets(None, None, None)
        code, text = self.run_brief("--dry-run", secrets=none)
        self.assertEqual(code, 0)
        self.assertIn("News unavailable (FINNHUB_API_KEY not set)", text)
        self.assertIn("(rule pick)", text)
        self.lines.clear()
        code, text = self.run_brief(secrets=none)
        self.assertEqual(code, m.EXIT_CONFIG)
        self.assertIn("DISCORD_WEBHOOK_URL is not set", text)
        self.assertEqual(self.sent, [])

    def test_news_failure_for_one_ticker(self):
        fh = FakeFinnhubSession(NOW, fail_tickers={"NVDA"})
        code, text = self.run_brief("--dry-run", finnhub=fh)
        self.assertEqual(code, 0)
        # one retry, never more
        nvda_calls = [r for r in fh.requests if r[1].get("symbol") == "NVDA"]
        self.assertEqual(len(nvda_calls), 2)

    def test_discord_failure_exit_code(self):
        def boom(url, msgs):
            raise RuntimeError("404")
        code = m.run(["--config", self.cfg], provider=FakeProvider(), finnhub_session=FakeFinnhubSession(NOW),
                     now=NOW, secrets=Secrets("x", None, "https://d"), poster=boom, out=self.lines.append)
        self.assertEqual(code, m.EXIT_SEND_FAILED)
        self.assertFalse((self.tmp / "logs" / "brief_log.csv").exists())


class TestSchedule(Harness):
    def test_holiday_replay_and_live(self):
        code, text = self.run_brief("--date", "2026-11-26", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Market closed on 2026-11-26 (Thanksgiving Day)", text)
        code, _ = self.run_brief(now=datetime(2026, 11, 26, 8, 30, tzinfo=ET))
        self.assertEqual(self.sent, [])

    def test_outside_window(self):
        code, text = self.run_brief(now=datetime(2026, 11, 20, 9, 30, tzinfo=ET))
        self.assertIn("Outside the run window", text)
        self.assertEqual(self.sent, [])
        code, _ = self.run_brief("--force", now=datetime(2026, 11, 20, 9, 30, tzinfo=ET))
        self.assertEqual(len(self.sent), 1)

    def test_second_run_same_day_is_skipped(self):
        self.run_brief()
        self.run_brief(now=datetime(2026, 11, 20, 8, 41, tzinfo=ET))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("already sent", self.lines[-1])
        rows = read_csv(self.tmp / "logs" / "brief_log.csv")
        self.assertEqual(len(rows), 17)
        self.assertEqual(sum(r["is_focus"] == "True" for r in rows), 2)

    def test_replay_uses_open_when_no_premarket(self):
        prov = FakeProvider(daily_overrides=bullish_overrides(date(2026, 10, 1)), no_pm=set(DEFAULT_GAPS))
        code, text = self.run_brief("--date", "2026-10-01", "--dry-run", provider=prov)
        self.assertEqual(code, 0)
        self.assertIn("replay, as of 08:30 ET", text)
        self.assertIn("gaps use the official open", text)


class TestNews(Harness):
    def test_dedupe_flags_and_earnings(self):
        fh = FakeFinnhubSession(NOW, earnings=[
            {"symbol": "AVGO", "date": "2026-11-20", "hour": "bmo"},
            {"symbol": "ZZZZ", "date": "2026-11-20", "hour": "amc"}])
        code, text = self.run_brief("--dry-run", finnhub=fh)
        self.assertIn("Earnings: AVGO today BMO", text)
        self.assertIn("Heavy news: TSLA (15)", text)  # 14 + shared chip story
        self.assertNotIn("old story", text)
        self.assertEqual(text.count("Chip stocks rally"), 1)


class TestAgent(Harness):
    def _final(self, **kw):
        base = {"bias_note": "SPY and QQQ both hold above the 8EMA.",
                "focus": [{"ticker": "AMD", "reason": "Leads the semis group on chip demand news."}],
                "headline_ids": ["h1"], "note": ""}
        base.update(kw)
        return final(base)

    def test_agent_pick_used_and_logged(self):
        claude = ScriptedClaude([tool_use("get_company_news", {"ticker": "AMD"}), self._final()])
        code, text = self.run_brief("--dry-run", claude=claude)
        self.assertIn("(agent pick)", text)
        self.assertIn("**1. AMD**", text)
        self.assertIn("> Leads the semis group on chip demand news.", text)
        self.assertIn("Agent used 1 tool call.", text)
        log = (self.tmp / "logs" / "agent" / "2026-11-20-dryrun.jsonl").read_text().splitlines()
        kinds = [json.loads(l)["kind"] for l in log]
        self.assertEqual(kinds[0], "start")
        self.assertIn("tool_call", kinds)
        self.assertEqual(kinds[-1], "validated")

    def test_tool_cap(self):
        script = [tool_use("get_price_history", {"ticker": "NVDA", "days": 5}, uid=f"t{i}") for i in range(12)]
        claude = ScriptedClaude(script[:10] + [self._final()])
        code, text = self.run_brief("--dry-run", claude=claude)
        self.assertEqual(len(claude.calls), 11)
        self.assertEqual(claude.calls[-1]["tool_choice"], {"type": "none"})
        self.assertIn("Agent used 10 tool calls.", text)

    def test_parallel_tool_requests_beyond_cap_are_refused(self):
        many = {"stop_reason": "tool_use", "content": [{"type": "text", "text": "batch"}] + [
            {"type": "tool_use", "id": f"x{i}", "name": "get_earnings_calendar", "input": {"ticker": "MU"}}
            for i in range(13)]}
        claude = ScriptedClaude([many, self._final()])
        self.run_brief("--dry-run", claude=claude)
        results = claude.calls[1]["messages"][2]["content"]
        refused = [r for r in results if r.get("is_error")]
        self.assertEqual(len(refused), 3)

    def test_time_cap_falls_back_to_rules(self):
        claude = ScriptedClaude([tool_use("get_market_news", {})] * 50)
        from opening_brief import llm
        orig = llm.run_agent
        ticks = iter(range(0, 10_000, 40))  # each clock read advances 40s

        def fast_clock_agent(*a, **k):
            return orig(*a, clock=lambda: next(ticks), **k)
        llm_mod = m
        old = llm_mod.run_agent
        llm_mod.run_agent = fast_clock_agent
        try:
            code, text = self.run_brief("--dry-run", claude=claude)
        finally:
            llm_mod.run_agent = old
        self.assertEqual(code, 0)
        self.assertIn("AI summary hit its time cap; focus list and headlines are rule-based.", text)
        self.assertIn("(rule pick)", text)

    def test_invalid_pick_and_invented_number(self):
        claude = ScriptedClaude([self._final(
            focus=[{"ticker": "TSLA", "reason": "weak"},
                   {"ticker": "NVDA", "reason": "Could run 9.7% today on the gap."}],
            bias_note="SPY up 3.33% premarket.", headline_ids=["h1", "h999"])])
        code, text = self.run_brief("--dry-run", claude=claude)
        self.assertNotIn("**1. TSLA**", text)
        self.assertIn("**1. NVDA**", text)
        self.assertNotIn("9.7%", text)
        self.assertRegex(text, r"> RS \+\d\.\d vs SPY, gap")   # rule reason replaced the invented one
        self.assertNotIn("3.33", text)

    def test_api_error_falls_back(self):
        claude = ScriptedClaude([])
        code, text = self.run_brief("--dry-run", claude=claude)
        self.assertEqual(code, 0)
        self.assertIn("AI summary failed", text)

    def test_summary_mode(self):
        p = self.tmp / "config.toml"
        p.write_text(p.read_text().replace("agent = true", "agent = false"))
        claude = ScriptedClaude([self._final()])
        code, text = self.run_brief("--dry-run", claude=claude)
        self.assertIn("(summary pick)", text)
        self.assertNotIn("tools", claude.calls[0])


class TestSplit(unittest.TestCase):
    def test_table_split_keeps_code_blocks_balanced(self):
        rows = "\n".join(f"T{i:03d}  +1.0  +0.5 ABV  100.00/99.00" for i in range(150))
        msgs = split_messages(["header", "TABLE::TKR GAP\n" + rows, "x" * 1990], 2000)
        self.assertTrue(all(len(x) <= 2000 for x in msgs))
        self.assertTrue(all(x.count("```") % 2 == 0 for x in msgs))
        self.assertEqual(sum(x.count("T0") + x.count("T1") for x in msgs), 150)


if __name__ == "__main__":
    unittest.main()


class TestNumberGuard(unittest.TestCase):
    def test_roundings_of_table_values_pass_and_inventions_fail(self):
        from opening_brief.llm import bad_numbers
        allowed = {2.38, 0.48, 248.85, 3.0, 20.0}
        self.assertEqual(bad_numbers("RS +2.38, rounds to +2.4 or 2, SPY 0.5%", allowed), [])
        self.assertEqual(bad_numbers("PM high $248.85 at 8:15, 3-day coil", allowed), [])
        self.assertEqual(bad_numbers("target 260 and gap 2.7%", allowed), ["260", "2.7%"])
        self.assertEqual(bad_numbers("down 0.48%", allowed), [])


class TestDataShapes(unittest.TestCase):
    def test_split_download_both_multiindex_orders(self):
        from opening_brief.data import _split_download
        idx = pd.date_range("2026-01-01", periods=3)
        cols_a = pd.MultiIndex.from_product([["NVDA", "AMD"], ["Open", "Close"]])
        raw = pd.DataFrame(1.0, index=idx, columns=cols_a)
        raw[("AMD", "Open")] = float("nan")
        raw[("AMD", "Close")] = float("nan")
        self.assertEqual(list(_split_download(raw, ["NVDA", "AMD"])), ["NVDA"])
        cols_b = pd.MultiIndex.from_product([["Open", "Close"], ["NVDA", "AMD"]])
        raw_b = pd.DataFrame(1.0, index=idx, columns=cols_b)
        got = _split_download(raw_b, ["NVDA", "AMD"])
        self.assertEqual(sorted(got), ["AMD", "NVDA"])
        self.assertEqual(list(got["NVDA"].columns), ["Open", "Close"])

    def test_stale_prior_close_blanks_gap(self):
        from opening_brief.analysis import build_analysis
        from opening_brief.data import load_market_data
        prov = FakeProvider()
        df = make_daily("NVDA", D)
        prov.overrides = {"NVDA": df[df.index.date < date(2026, 11, 19)]}  # missing Nov 19 bar
        data = load_market_data(prov, ["NVDA", "SPY", "QQQ"], D, NOW)
        a = build_analysis(data, D, {}, ["SPY", "QQQ"])
        self.assertIsNone(a.rows["NVDA"].gap_pct)
        self.assertIn("expected 2026-11-19", a.rows["NVDA"].stale_note)
        self.assertIsNotNone(a.rows["SPY"].gap_pct)


class TestForcedRerun(Harness):
    def test_forced_rerun_sends_but_does_not_duplicate_log(self):
        self.run_brief()
        self.run_brief("--force", now=datetime(2026, 11, 20, 9, 5, tzinfo=ET))
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(read_csv(self.tmp / "logs" / "brief_log.csv")), 17)
