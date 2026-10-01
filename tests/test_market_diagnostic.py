import unittest

from market_diagnostic import summarize


class MarketDiagnosticTests(unittest.TestCase):
    def test_marks_only_current_successful_run_as_generated(self):
        status = {
            "schemaVersion": 2,
            "status": "degraded",
            "generatedAt": "2026-10-01T10:22:00+08:00",
            "service": {"runId": "123", "commit": "abc", "generatedAt": "2026-10-01T10:22:00+08:00"},
            "markets": {"taiwan": {"status": "stale"}},
            "instruments": {"006208": {"status": "stale", "latestSessionDate": "2026-09-29",
                                          "expectedSessionDate": "2026-09-30", "sourceAttempts": 3}},
        }
        current = summarize(status, "123", "abc", "success", "RUN_UPDATE_MISSING_OR_DEGRADED", "RUN_CANDIDATE_NOT_OLDER_THAN_LIVE", "v22.23.3")
        self.assertEqual(current["stage"], "data_degraded")
        self.assertEqual(current["instruments"]["006208"]["latestSessionDate"], "2026-09-29")
        self.assertEqual(current["runtimeVersions"]["node"], "v22.23.3")
        self.assertEqual(current["publicationReason"], "RUN_CANDIDATE_NOT_OLDER_THAN_LIVE")

        stale_checkout = summarize(status, "456", "def", "failure")
        self.assertEqual(stale_checkout["stage"], "generation_not_verified")
        self.assertNotEqual(stale_checkout["stage"], "generated")


if __name__ == "__main__":
    unittest.main()
