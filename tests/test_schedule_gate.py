import datetime as dt
from io import BytesIO
import json
import unittest
from unittest.mock import patch

from schedule_gate import TAIPEI, already_succeeded, decide, fetch_run_success, target_window


def good_status(day="2026-09-29", window="morning", commit="abc"):
    return {
        "schemaVersion": 2,
        "status": "ok",
        "service": {"status": "ok", "windowDate": day, "window": window, "commit": commit, "runId": "123"},
        "calendar": {"status": "verified"},
        "markets": {
            "taiwan": {"status": "market_closed", "latestSessionDate": "2026-09-24",
                       "expectedSessionDate": "2026-09-24", "nextDueAt": "2026-09-29T14:30:00+08:00"},
            "us": {"status": "fresh", "latestSessionDate": "2026-09-28",
                   "expectedSessionDate": "2026-09-28", "nextDueAt": "2026-09-29T21:30:00+08:00"},
        },
    }


class ScheduleGateTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 9, 29, 7, 45, tzinfo=TAIPEI)

    def test_primary_success_skips_fallback_for_same_window_commit_and_data(self):
        run, reason, day, window = decide(
            "schedule", "abc", self.now, good_status(),
            run_checker=lambda run_id, commit: True,
            window_checker=lambda payload, target_date, target, now: True,
        )
        self.assertFalse(run)
        self.assertEqual(reason, "SKIP_ALREADY_SUCCEEDED")
        self.assertEqual((day, window), ("2026-09-29", "morning"))

    def test_missing_successful_actions_deployment_record_runs_fallback(self):
        run, reason, _, _ = decide(
            "schedule", "abc", self.now, good_status(), run_checker=lambda run_id, commit: False
        )
        self.assertTrue(run)
        self.assertEqual(reason, "RUN_UPDATE_MISSING_OR_DEGRADED")

    def test_actions_api_unavailable_runs_fallback(self):
        def unavailable(run_id, commit):
            raise OSError("api temporarily unavailable")

        run, reason, _, _ = decide(
            "schedule", "abc", self.now, good_status(), run_checker=unavailable
        )
        self.assertTrue(run)
        self.assertEqual(reason, "RUN_DEPLOYMENT_RECORD_UNVERIFIED")

    def test_fallback_gate_requires_independent_publication_verification_artifact(self):
        run = {"status": "completed", "conclusion": "success", "head_sha": "abc"}
        verified = {"artifacts": [{"name": "skynet-publication-verification", "expired": False}]}
        with patch("schedule_gate.urllib.request.urlopen", side_effect=[
            BytesIO(json.dumps(run).encode()), BytesIO(json.dumps(verified).encode()),
        ]):
            self.assertTrue(fetch_run_success("123", "abc"))

        unverified = {"artifacts": [{"name": "skynet-publication-deployment-summary", "expired": False}]}
        with patch("schedule_gate.urllib.request.urlopen", side_effect=[
            BytesIO(json.dumps(run).encode()), BytesIO(json.dumps(unverified).encode()),
        ]):
            self.assertFalse(fetch_run_success("123", "abc"))

    def test_degraded_market_data_allows_fallback(self):
        payload = good_status()
        payload["status"] = "degraded"
        run, reason, _, _ = decide("schedule", "abc", self.now, payload)
        self.assertTrue(run)
        self.assertEqual(reason, "RUN_UPDATE_MISSING_OR_DEGRADED")

    def test_old_window_or_old_commit_does_not_block(self):
        self.assertTrue(decide("schedule", "abc", self.now, good_status(day="2026-09-28"))[0])
        self.assertTrue(decide("schedule", "def", self.now, good_status())[0])

    def test_unknown_status_runs_availability_first(self):
        run, reason, _, _ = decide("schedule", "abc", self.now, fetcher=lambda: (_ for _ in ()).throw(OSError()))
        self.assertTrue(run)
        self.assertEqual(reason, "RUN_STATUS_UNVERIFIED")

    def test_manual_and_push_are_never_gated(self):
        for event in ("workflow_dispatch", "push"):
            self.assertTrue(decide(event, "abc", self.now, payload={"status": "ok"})[0])

    def test_delayed_morning_event_targets_latest_due_afternoon_window(self):
        delayed = dt.datetime(2026, 9, 29, 16, 10, tzinfo=TAIPEI)
        self.assertEqual(target_window(delayed), ("2026-09-29", "afternoon"))

    def test_delayed_update_before_morning_window_keeps_previous_due_window(self):
        delayed = dt.datetime(2026, 9, 29, 4, 0, tzinfo=TAIPEI)
        self.assertEqual(target_window(delayed), ("2026-09-28", "afternoon"))

    def test_expired_market_deadline_cannot_skip_fallback(self):
        payload = good_status()
        payload["markets"]["taiwan"]["nextDueAt"] = "2026-09-29T07:30:00+08:00"
        self.assertFalse(already_succeeded(payload, "2026-09-29", "morning", "abc", self.now, run_success=True))


if __name__ == "__main__":
    unittest.main()
