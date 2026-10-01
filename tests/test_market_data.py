import datetime as dt
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import pandas as pd

from market_data import (
    MarketDataError,
    compare_sources,
    fetch_twse_history,
    has_complete_range,
    has_complete_sessions,
    save_validated_cache,
    validated_cache,
)


class MarketDataTests(unittest.TestCase):
    def _response(self, symbol="^TWII"):
        if symbol == "^TWII":
            payload = {
                "stat": "OK",
                "fields": ["日期", "開盤指數", "最高指數", "最低指數", "收盤指數"],
                "data": [
                    ["115/09/29", "100", "102", "99", "101"],
                    ["115/09/30", "101", "103", "100", "102"],
                    ["115/10/01", "102", "104", "101", "103"],
                ],
            }
        else:
            payload = {
                "stat": "OK",
                "fields": ["日期", "成交股數", "開盤價", "最高價", "最低價", "收盤價"],
                "data": [
                    ["115/09/29", "1000", "100", "102", "99", "101"],
                    ["115/09/30", "1000", "101", "103", "100", "102"],
                    ["115/10/01", "1000", "102", "104", "101", "103"],
                ],
            }
        return io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def test_official_history_uses_named_fields_and_excludes_unfinished_session(self):
        with patch("market_data.urllib.request.urlopen", return_value=self._response()):
            frame, attempts = fetch_twse_history("^TWII", dt.date(2026, 9, 30), 1)
        self.assertEqual(attempts, 1)
        self.assertEqual(list(frame.index), [dt.date(2026, 9, 29), dt.date(2026, 9, 30)])
        self.assertEqual(float(frame.loc[dt.date(2026, 9, 30), "Close"]), 102)

    def test_official_etf_endpoint_uses_ohlc_field_names(self):
        with patch("market_data.urllib.request.urlopen", return_value=self._response("006208.TW")) as request:
            frame, _ = fetch_twse_history("006208.TW", dt.date(2026, 9, 30), 1)
        self.assertIn("006208", request.call_args.args[0].full_url)
        self.assertEqual(float(frame.loc[dt.date(2026, 9, 30), "High"]), 103)

    def test_http_503_retries_three_times_with_bounded_backoff(self):
        responses = [HTTPError("url", 503, "busy", {}, None),
                     HTTPError("url", 503, "busy", {}, None), self._response()]
        with patch("market_data.urllib.request.urlopen", side_effect=responses) as request, \
             patch("market_data.time.sleep") as sleep:
            _, attempts = fetch_twse_history("^TWII", dt.date(2026, 9, 30), 1)
        self.assertEqual(attempts, 3)
        self.assertEqual(request.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_recent_common_price_conflict_is_not_treated_as_source_lag(self):
        day = dt.date(2026, 9, 30)
        official = pd.DataFrame({"Close": [100.0]}, index=[day])
        secondary = pd.DataFrame({"Close": [102.0]}, index=[day])
        self.assertEqual(compare_sources(official, secondary), "SOURCE_CONFLICT")

    def test_secondary_lag_is_distinct_from_price_conflict(self):
        official = pd.DataFrame({"Close": [100.0, 101.0]}, index=[
            dt.date(2026, 9, 29), dt.date(2026, 9, 30),
        ])
        secondary = pd.DataFrame({"Close": [100.0]}, index=[dt.date(2026, 9, 29)])
        self.assertEqual(compare_sources(official, secondary), "SECONDARY_LAGGING")

    def test_verified_history_cache_hash_and_age_are_checked(self):
        frame = pd.DataFrame({
            "Open": [100.0, 101.0], "High": [102.0, 103.0],
            "Low": [99.0, 100.0], "Close": [101.0, 102.0],
        }, index=pd.Index([dt.date(2026, 9, 29), dt.date(2026, 9, 30)], dtype=object))
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/index.json"
            save_validated_cache(path, frame, "TWSE", dt.datetime.now(dt.timezone.utc).isoformat())
            loaded = validated_cache(path, dt.date(2026, 9, 30), 2)
            self.assertIsNotNone(loaded)
            with open(path, encoding="utf-8") as file:
                cached = json.load(file)
            cached["bars"]["2026-09-30"]["Close"] = 999.0
            with open(path, "w", encoding="utf-8") as file:
                json.dump(cached, file)
            self.assertIsNone(validated_cache(path, dt.date(2026, 9, 30), 2))

    def test_ma_history_must_cover_last_200_official_sessions(self):
        calendar = {"years": [2025, 2026], "closedDates": ["2026-09-30"]}
        expected = dt.date(2026, 10, 1)
        complete = []
        day = expected
        while len(complete) < 200:
            if day.weekday() < 5 and day.isoformat() not in calendar["closedDates"]:
                complete.append(day)
            day -= dt.timedelta(days=1)
        frame = pd.DataFrame({"Close": [100.0] * 200}, index=pd.Index(complete, dtype=object))
        self.assertTrue(has_complete_sessions(frame, expected, calendar, 200))
        self.assertFalse(has_complete_sessions(frame.iloc[:-1], expected, calendar, 200))

    def test_prior_year_session_dates_are_reused_to_validate_etf_coverage(self):
        calendar = {"years": [2026], "closedDates": ["2026-01-01"]}
        reference = [dt.date(2025, 12, 26), dt.date(2025, 12, 29), dt.date(2025, 12, 30),
                     dt.date(2025, 12, 31), dt.date(2026, 1, 2)]
        complete = pd.DataFrame({"Close": [100.0] * len(reference)},
                                 index=pd.Index(reference, dtype=object))
        self.assertTrue(has_complete_sessions(
            complete, dt.date(2026, 1, 2), calendar, 5, reference_sessions=reference,
        ))
        missing = complete.drop(dt.date(2025, 12, 30))
        self.assertFalse(has_complete_sessions(
            missing, dt.date(2026, 1, 2), calendar, 5, reference_sessions=reference,
        ))

    def test_calculation_range_requires_each_expected_session(self):
        calendar = {"years": [2026], "closedDates": ["2026-01-02"]}
        sessions = [dt.date(2026, 1, 1), dt.date(2026, 1, 5), dt.date(2026, 1, 6)]
        complete = pd.DataFrame({"Close": [100.0] * 3}, index=pd.Index(sessions, dtype=object))
        self.assertTrue(has_complete_range(
            complete, dt.date(2026, 1, 1), dt.date(2026, 1, 6), calendar,
        ))
        missing = complete.drop(dt.date(2026, 1, 5))
        self.assertFalse(has_complete_range(
            missing, dt.date(2026, 1, 1), dt.date(2026, 1, 6), calendar,
        ))

    def test_duplicate_official_rows_fail_closed(self):
        response = {
            "stat": "OK", "fields": ["日期", "開盤指數", "最高指數", "最低指數", "收盤指數"],
            "data": [["115/09/30", "100", "102", "99", "101"]] * 2,
        }
        with patch("market_data.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
            with self.assertRaisesRegex(MarketDataError, "SOURCE_DUPLICATE_DATE"):
                fetch_twse_history("^TWII", dt.date(2026, 9, 30), 1)


if __name__ == "__main__":
    unittest.main()
