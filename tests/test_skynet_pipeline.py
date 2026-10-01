import datetime as dt
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tg_bot_optimized import TAIPEI, main, market_snapshot


class FakeTicker:
    def __init__(self, symbol, frames):
        self.symbol = symbol
        self.frames = frames

    def history(self, period, auto_adjust=False, timeout=None):
        return self.frames[self.symbol]


class SkynetPipelineTests(unittest.TestCase):
    def _frames(self):
        import pandas as pd

        tw_dates = list(pd.bdate_range("2025-09-01", "2026-09-29"))
        tw_dates = [day for day in tw_dates if day.strftime("%Y-%m-%d") not in {"2026-09-25", "2026-09-28"}]
        us_dates = list(pd.bdate_range("2025-12-01", "2026-09-28"))
        return {
            "^TWII": pd.DataFrame({"Close": [20000 + i for i in range(len(tw_dates))]}, index=tw_dates),
            "006208.TW": pd.DataFrame({
                "Close": [100 + i / 10 for i in range(len(tw_dates))],
                "High": [101 + i / 10 for i in range(len(tw_dates))],
            }, index=tw_dates),
            "^VIX": pd.DataFrame({"Close": [18 + (i % 5) for i in range(len(us_dates))]}, index=us_dates),
        }

    def _calendars(self):
        return {
            "schemaVersion": 1,
            "checkedAt": "2026-09-29T00:00:00Z",
            "sha256": "a" * 64,
            "twse": {"closedDates": ["2026-09-25", "2026-09-28"], "years": [2025, 2026]},
            "cboe": {"closedDates": [], "years": [2025, 2026]},
        }

    def _history_resolver(self, symbol, period, timezone, expected, months, cache_dir):
        import pandas as pd

        frame = self._frames()[symbol].copy()
        days = [value.date() if hasattr(value, "date") else value for value in frame.index]
        selected = [day <= expected for day in days]
        frame = frame.loc[selected].copy()
        frame.index = pd.Index([day for day, keep in zip(days, selected) if keep], dtype=object)
        latest = frame.index[-1]
        current = latest == expected
        return frame, {
            "selectedSource": "fixture",
            "sourceAttempts": 1, "sampleCount": len(frame), "latestSessionDate": latest.isoformat(),
            "expectedSessionDate": expected.isoformat(), "status": "fresh" if current else "stale",
            "reasonCode": "OK" if current else "SOURCE_LAGGING", "cacheUsed": False,
        }

    def test_uses_last_completed_taiwan_close_before_daily_cutoff(self):
        frames = self._frames()
        now = dt.datetime(2026, 9, 29, 13, 0, tzinfo=TAIPEI)
        with patch("tg_bot_optimized.load_calendars", return_value=self._calendars()), \
             patch("tg_bot_optimized.yf.Ticker", side_effect=lambda symbol: FakeTicker(symbol, frames)):
            values, sources, markets, calendar, instruments = market_snapshot(
                now, history_resolver=self._history_resolver
            )
        self.assertEqual(markets["taiwan"]["expectedSessionDate"], "2026-09-24")
        self.assertEqual(markets["taiwan"]["latestSessionDate"], "2026-09-24")
        self.assertEqual(markets["taiwan"]["status"], "fresh")
        self.assertEqual(markets["us"]["expectedSessionDate"], "2026-09-28")
        self.assertTrue(all(value == "ok" for value in sources.values()))
        self.assertIsNotNone(values["ma200"])
        self.assertEqual(calendar["status"], "verified")
        self.assertEqual(instruments["^TWII"]["latestSessionDate"], "2026-09-24")
        self.assertEqual(instruments["006208"]["latestSessionDate"], "2026-09-24")

    def test_after_close_includes_current_day_only_when_present(self):
        frames = self._frames()
        now = dt.datetime(2026, 9, 29, 14, 30, tzinfo=TAIPEI)
        with patch("tg_bot_optimized.load_calendars", return_value=self._calendars()), \
             patch("tg_bot_optimized.yf.Ticker", side_effect=lambda symbol: FakeTicker(symbol, frames)):
            _, sources, markets, _, instruments = market_snapshot(
                now, history_resolver=self._history_resolver
            )
        self.assertEqual(markets["taiwan"]["expectedSessionDate"], "2026-09-29")
        self.assertEqual(markets["taiwan"]["latestSessionDate"], "2026-09-29")
        self.assertEqual(markets["taiwan"]["status"], "fresh")
        self.assertTrue(all(value == "ok" for value in sources.values()))
        self.assertEqual(instruments["^TWII"]["latestSessionDate"], "2026-09-29")

    def test_october_first_replay_identifies_lagging_006208_and_preserves_dates(self):
        import pandas as pd

        frames = self._frames()
        twii = frames["^TWII"].copy()
        twii.loc[pd.Timestamp("2026-09-30")] = 22000.0
        frames["^TWII"] = twii.sort_index()
        vix = frames["^VIX"].copy()
        vix.loc[pd.Timestamp("2026-09-29")] = 18.0
        vix.loc[pd.Timestamp("2026-09-30")] = 19.0
        frames["^VIX"] = vix.sort_index()

        def resolver(symbol, period, timezone, expected, months, cache_dir):
            import pandas as pd

            frame = frames[symbol].copy()
            days = [value.date() if hasattr(value, "date") else value for value in frame.index]
            selected = [day <= expected for day in days]
            frame = frame.loc[selected].copy()
            frame.index = pd.Index([day for day, keep in zip(days, selected) if keep], dtype=object)
            latest = frame.index[-1]
            is_current = latest == expected
            return frame, {
                "selectedSource": "TWSE", "sourceAttempts": 2, "sampleCount": len(frame),
                "latestSessionDate": latest.isoformat(), "expectedSessionDate": expected.isoformat(),
                "status": "fresh" if is_current else "stale",
                "reasonCode": "OK" if is_current else "SOURCE_LAGGING", "cacheUsed": False,
            }

        now = dt.datetime(2026, 10, 1, 10, 22, tzinfo=TAIPEI)
        with patch("tg_bot_optimized.load_calendars", return_value=self._calendars()):
            _, sources, markets, _, instruments = market_snapshot(
                now, history_resolver=resolver,
            )
        self.assertEqual(instruments["^TWII"]["latestSessionDate"], "2026-09-30")
        self.assertEqual(instruments["006208"]["latestSessionDate"], "2026-09-29")
        self.assertEqual(instruments["006208"]["expectedSessionDate"], "2026-09-30")
        self.assertEqual(instruments["006208"]["status"], "stale")
        self.assertEqual(instruments["006208"]["reasonCode"], "SOURCE_LAGGING")
        self.assertEqual(markets["taiwan"]["status"], "stale")
        self.assertEqual(sources["006208"], "stale")

    def test_yahoo_transport_timeout_retries_at_most_three_times(self):
        import pandas as pd

        from tg_bot_optimized import _fetch_market_history

        class Ticker:
            def __init__(self):
                self.calls = 0

            def history(self, **kwargs):
                self.calls += 1
                if self.calls < 3:
                    raise TimeoutError("temporary timeout")
                return pd.DataFrame({"Close": [100]}, index=[pd.Timestamp("2026-09-29")])

        ticker = Ticker()
        with patch("tg_bot_optimized.yf.Ticker", return_value=ticker), patch("tg_bot_optimized.time.sleep"):
            frame = _fetch_market_history("^TWII", "400d")
        self.assertEqual(len(frame), 1)
        self.assertEqual(ticker.calls, 3)

    def test_non_transient_market_error_does_not_retry(self):
        from tg_bot_optimized import _fetch_market_history

        class Ticker:
            calls = 0

            def history(self, **kwargs):
                self.calls += 1
                raise ValueError("invalid ticker configuration")

        ticker = Ticker()
        with patch("tg_bot_optimized.yf.Ticker", return_value=ticker):
            with self.assertRaisesRegex(ValueError, "invalid ticker"):
                _fetch_market_history("^TWII", "400d")
        self.assertEqual(ticker.calls, 1)

    def test_current_official_history_remains_healthy_when_yahoo_is_unavailable(self):
        import pandas as pd

        from tg_bot_optimized import _resolve_history

        expected = dt.date(2026, 9, 30)
        official = pd.DataFrame({
            "Open": [100.0, 101.0], "High": [102.0, 103.0],
            "Low": [99.0, 100.0], "Close": [101.0, 102.0],
        }, index=pd.Index([dt.date(2026, 9, 29), expected], dtype=object))
        with tempfile.TemporaryDirectory() as directory, \
             patch("tg_bot_optimized.fetch_twse_history", return_value=(official, 2)), \
             patch("tg_bot_optimized._history_for_completed_sessions", side_effect=TimeoutError("provider timeout")):
            frame, diagnostic = _resolve_history(
                "006208.TW", "400d", TAIPEI, expected, 14, directory,
            )
        self.assertEqual(frame.index[-1], expected)
        self.assertEqual(diagnostic["status"], "fresh")
        self.assertEqual(diagnostic["reasonCode"], "SECONDARY_UNAVAILABLE")
        self.assertEqual(diagnostic["comparison"], "SECONDARY_UNAVAILABLE")

    def test_duplicate_or_non_finite_market_bars_fail_closed(self):
        import pandas as pd

        from tg_bot_optimized import _history_for_completed_sessions

        duplicate = pd.DataFrame({"Close": [100, 101]}, index=[
            pd.Timestamp("2026-09-29"), pd.Timestamp("2026-09-29")
        ])
        with patch("tg_bot_optimized._fetch_market_history", return_value=duplicate):
            with self.assertRaisesRegex(ValueError, "duplicate"):
                _history_for_completed_sessions("^TWII", "400d", TAIPEI, dt.date(2026, 9, 29))

        invalid = pd.DataFrame({"Close": [100, float("inf")]}, index=[
            pd.Timestamp("2026-09-28"), pd.Timestamp("2026-09-29")
        ])
        with patch("tg_bot_optimized._fetch_market_history", return_value=invalid):
            with self.assertRaisesRegex(ValueError, "non-finite"):
                _history_for_completed_sessions("^TWII", "400d", TAIPEI, dt.date(2026, 9, 29))

    def test_generated_contract_separates_service_and_market_time(self):
        values = {"taiex": 22000.0, "ma200": 21000.0, "daysBelowMa": 0,
                  "vix": 18.0, "daysVixAbove20": 0, "peak_006208": 130.0,
                  "asset_006208": 125.0}
        sources = {"taiex": "ok", "vix": "ok", "006208": "ok"}
        markets = {
            "taiwan": {"status": "fresh", "latestSessionDate": "2026-09-29",
                       "expectedSessionDate": "2026-09-29", "nextDueAt": "2026-09-30T14:30:00+08:00"},
            "us": {"status": "fresh", "latestSessionDate": "2026-09-28",
                   "expectedSessionDate": "2026-09-28", "nextDueAt": "2026-09-30T06:30:00+08:00"},
        }
        calendar = {"status": "verified", "version": "fixture"}
        with tempfile.TemporaryDirectory() as directory, patch("tg_bot_optimized.market_snapshot",
                return_value=(values, sources, markets, calendar, {
                    "^TWII": {"status": "fresh", "selectedSource": "TWSE"},
                    "006208": {"status": "fresh", "selectedSource": "TWSE"},
                    "^VIX": {"status": "fresh", "selectedSource": "Yahoo"},
                })), patch.dict(
                    os.environ, {"TARGET_WINDOW": "morning", "TARGET_WINDOW_DATE": "2026-09-29",
                                 "GITHUB_SHA": "abc", "GITHUB_RUN_ID": "123"}, clear=False):
            previous = os.getcwd()
            os.chdir(directory)
            try:
                main()
            finally:
                os.chdir(previous)
            with open(os.path.join(directory, "public", "status.json"), encoding="utf-8") as file:
                status = json.load(file)
            with open(os.path.join(directory, "public", "data.json"), encoding="utf-8") as file:
                data = json.load(file)
        self.assertEqual(status["schemaVersion"], 2)
        self.assertEqual(status["service"]["windowDate"], "2026-09-29")
        self.assertEqual(status["service"]["commit"], "abc")
        self.assertEqual(status["markets"]["taiwan"]["latestSessionDate"], "2026-09-29")
        self.assertRegex(status["generatedAt"], r"\+08:00$")
        self.assertEqual(data["dataQuality"]["staleAfterHours"], 18)


if __name__ == "__main__":
    unittest.main()
