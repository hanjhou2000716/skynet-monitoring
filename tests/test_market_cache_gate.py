import unittest

from market_cache_gate import is_cache_eligible


class MarketCacheGateTests(unittest.TestCase):
    def _healthy_payload(self):
        return {
            "status": "ok",
            "calendar": {"status": "verified"},
            "markets": {
                "taiwan": {"status": "fresh", "latestSessionDate": "2026-09-30", "expectedSessionDate": "2026-09-30"},
                "us": {"status": "fresh", "latestSessionDate": "2026-09-30", "expectedSessionDate": "2026-09-30"},
            },
            "instruments": {
                "^TWII": {"status": "fresh", "latestSessionDate": "2026-09-30", "expectedSessionDate": "2026-09-30", "reasonCode": "OK"},
                "006208": {"status": "fresh", "latestSessionDate": "2026-09-30", "expectedSessionDate": "2026-09-30", "reasonCode": "SECONDARY_LAGGING"},
            },
            "sources": {"taiex": "ok", "006208": "ok", "vix": "ok"},
        }

    def test_only_fully_verified_current_market_snapshot_can_refresh_cache(self):
        self.assertTrue(is_cache_eligible(self._healthy_payload()))

    def test_internal_nested_source_contract_remains_compatible(self):
        nested = self._healthy_payload()
        nested["dataQuality"] = {"sources": nested.pop("sources")}
        self.assertTrue(is_cache_eligible(nested))

    def test_failed_source_cannot_refresh_cache(self):
        failed = self._healthy_payload()
        failed["sources"]["006208"] = "stale"
        self.assertFalse(is_cache_eligible(failed))

    def test_degraded_or_conflicted_snapshot_cannot_refresh_cache(self):
        degraded = self._healthy_payload()
        degraded["status"] = "degraded"
        self.assertFalse(is_cache_eligible(degraded))

        conflict = self._healthy_payload()
        conflict["instruments"]["006208"]["reasonCode"] = "SOURCE_CONFLICT"
        self.assertFalse(is_cache_eligible(conflict))

    def test_future_or_missing_market_session_cannot_refresh_cache(self):
        stale = self._healthy_payload()
        stale["instruments"]["006208"]["latestSessionDate"] = "2026-09-29"
        self.assertFalse(is_cache_eligible(stale))


if __name__ == "__main__":
    unittest.main()
