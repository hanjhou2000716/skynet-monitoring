import datetime as dt
import json
import os
import ssl
import tempfile
import unittest
from unittest.mock import patch

from market_health import (
    CalendarUnavailable,
    TAIPEI,
    TWSE_URL,
    _request_text,
    expected_taiwan_session,
    expected_us_session,
    load_calendars,
    market_contract,
    parse_cboe_calendar,
    parse_twse_calendar,
    taiwan_next_due,
)


TWSE_FIXTURE = """
<table><tr><th>日期</th><th>名稱</th><th>說明</th></tr>
<tr><td>2026-01-02</td><td>國曆新年開始交易日</td><td>開始交易</td></tr>
<tr><td>2026-09-25</td><td>中秋節</td><td>依規定放假</td></tr>
<tr><td>2026-09-28</td><td>孔子誕辰紀念日/教師節</td><td>依規定放假</td></tr>
<tr><td>2026-12-31</td><td>農曆春節前最後交易日</td><td>最後交易</td></tr>
</table>
"""
CBOE_FIXTURE = """Holiday Name,Date,Regular Trading Hours,Global Trading Hours
New Year's Day,2026-01-01,None,None
Good Friday,2026-04-03,None,None
Early Close,2026-11-27,9:30 a.m. - 1:00 p.m. ET,Open
"""


class MarketCalendarTests(unittest.TestCase):
    def test_twse_tls_relaxes_only_strict_profile_and_keeps_trust_checks(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"verified calendar"

        with patch("market_health.urlopen", return_value=Response()) as open_url:
            self.assertEqual(_request_text(TWSE_URL), "verified calendar")
        context = open_url.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict_flag:
            self.assertEqual(context.verify_flags & strict_flag, 0)

    def test_twse_calendar_identifies_closures_and_open_special_dates(self):
        parsed = parse_twse_calendar(TWSE_FIXTURE)
        self.assertEqual(parsed["years"], [2026])
        self.assertEqual(parsed["closedDates"], ["2026-09-25", "2026-09-28"])

    def test_cboe_calendar_only_marks_full_closures(self):
        parsed = parse_cboe_calendar(CBOE_FIXTURE)
        self.assertEqual(parsed["closedDates"], ["2026-01-01", "2026-04-03"])

    def test_cboe_calendar_skips_current_metadata_preamble(self):
        response = (
            '# Generated: 2026:09:30 00:00:47\\n#\\n'
            '# Start CSV parsing at the line after the "##".\\n#\\n##\r\n'
            + CBOE_FIXTURE
        )
        parsed = parse_cboe_calendar(response)
        self.assertEqual(parsed["years"], [2026])
        self.assertEqual(parsed["closedDates"], ["2026-01-01", "2026-04-03"])

    def test_four_day_taiwan_closure_expected_session_and_next_deadline(self):
        calendar = {"closedDates": ["2026-09-25", "2026-09-28"], "years": [2026]}
        before_cutoff = dt.datetime(2026, 9, 29, 13, 59, tzinfo=TAIPEI)
        after_cutoff = dt.datetime(2026, 9, 29, 14, 30, tzinfo=TAIPEI)
        holiday = dt.datetime(2026, 9, 28, 15, 0, tzinfo=TAIPEI)
        self.assertEqual(expected_taiwan_session(before_cutoff, calendar), dt.date(2026, 9, 24))
        self.assertEqual(expected_taiwan_session(holiday, calendar), dt.date(2026, 9, 24))
        self.assertEqual(expected_taiwan_session(after_cutoff, calendar), dt.date(2026, 9, 29))
        deadline = taiwan_next_due(before_cutoff, dt.date(2026, 9, 24), calendar)
        self.assertEqual(deadline, dt.datetime(2026, 9, 29, 14, 30, tzinfo=TAIPEI))

    def test_vix_cutoff_respects_dst_transition(self):
        calendar = {"closedDates": [], "years": [2025, 2026]}
        spring_before = dt.datetime(2026, 3, 10, 6, 29, tzinfo=TAIPEI)
        spring_after = dt.datetime(2026, 3, 10, 6, 30, tzinfo=TAIPEI)
        fall_before = dt.datetime(2026, 11, 3, 6, 29, tzinfo=TAIPEI)
        fall_after = dt.datetime(2026, 11, 3, 6, 30, tzinfo=TAIPEI)
        self.assertEqual(expected_us_session(spring_before, calendar), dt.date(2026, 3, 6))
        self.assertEqual(expected_us_session(spring_after, calendar), dt.date(2026, 3, 9))
        self.assertEqual(expected_us_session(fall_before, calendar), dt.date(2026, 10, 30))
        self.assertEqual(expected_us_session(fall_after, calendar), dt.date(2026, 11, 2))

    def test_mismatched_latest_session_is_stale(self):
        value = market_contract(
            dt.date(2026, 9, 24), dt.date(2026, 9, 29),
            dt.datetime(2026, 9, 29, 14, 30, tzinfo=TAIPEI), True,
        )
        self.assertEqual(value["status"], "stale")
        self.assertEqual(value["reasonCode"], "MARKET_DATA_STALE")

    def test_cached_calendar_is_used_only_inside_24_hour_window(self):
        now = dt.datetime(2026, 9, 29, 7, 0, tzinfo=TAIPEI)
        with tempfile.TemporaryDirectory() as directory:
            with patch("market_health._request_text", side_effect=[TWSE_FIXTURE, CBOE_FIXTURE]):
                first = load_calendars(now, directory)
            self.assertFalse(first["cacheUsed"])
            with patch("market_health._request_text", side_effect=RuntimeError("offline")):
                cached = load_calendars(now + dt.timedelta(hours=23), directory)
            self.assertTrue(cached["cacheUsed"])
            with patch("market_health._request_text", side_effect=RuntimeError("offline")):
                with self.assertRaises(CalendarUnavailable):
                    load_calendars(now + dt.timedelta(hours=25), directory)

    def test_corrupt_calendar_cache_is_rejected(self):
        now = dt.datetime(2026, 9, 29, 7, 0, tzinfo=TAIPEI)
        with tempfile.TemporaryDirectory() as directory:
            with patch("market_health._request_text", side_effect=[TWSE_FIXTURE, CBOE_FIXTURE]):
                load_calendars(now, directory)
            path = os.path.join(directory, "market-calendar.json")
            with open(path, encoding="utf-8") as file:
                corrupted = json.load(file)
            corrupted["twse"]["closedDates"].append("2026-09-29")
            with open(path, "w", encoding="utf-8") as file:
                json.dump(corrupted, file)
            with patch("market_health._request_text", side_effect=RuntimeError("offline")):
                with self.assertRaises(CalendarUnavailable):
                    load_calendars(now + dt.timedelta(hours=1), directory)


if __name__ == "__main__":
    unittest.main()
