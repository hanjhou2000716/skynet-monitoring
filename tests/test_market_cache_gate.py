import unittest

from market_cache_gate import cacheable_instruments, is_cache_eligible


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
                "^TWII": {"status": "fresh", "latestSessionDate": "2026-09-30", "expectedSessionDate": "2026-09-30", "reasonCode": "OK", "selectedSource": "TWSE"},
                "006208": {"status": "fresh", "latestSessionDate": "2026-09-30", "expectedSessionDate": "2026-09-30", "reasonCode": "SECONDARY_LAGGING", "selectedSource": "TWSE"},
            },
            "sources": {"taiex": "ok", "006208": "ok", "vix": "ok"},
        }

    def test_only_fully_verified_current_market_snapshot_can_refresh_cache(self):
        self.assertTrue(is_cache_eligible(self._healthy_payload()))

    def test_internal_nested_source_contract_remains_compatible(self):
        nested = self._healthy_payload()
        nested["dataQuality"] = {"sources": nested.pop("sources")}
        self.assertTrue(is_cache_eligible(nested))

    def test_failed_instrument_cannot_refresh_its_cache(self):
        failed = self._healthy_payload()
        failed["instruments"]["006208"]["status"] = "unavailable"
        self.assertEqual(cacheable_instruments(failed), {"^TWII"})
        self.assertTrue(is_cache_eligible(failed))

    def test_degraded_snapshot_preserves_good_instrument_cache_without_refreshing_conflict(self):
        degraded = self._healthy_payload()
        degraded["status"] = "degraded"
        degraded["instruments"]["006208"]["status"] = "unavailable"
        self.assertEqual(cacheable_instruments(degraded), {"^TWII"})

        conflict = self._healthy_payload()
        conflict["instruments"]["006208"]["reasonCode"] = "SOURCE_CONFLICT"
        self.assertEqual(cacheable_instruments(conflict), {"^TWII"})

    def test_future_or_missing_market_session_cannot_refresh_cache(self):
        stale = self._healthy_payload()
        stale["instruments"]["006208"]["latestSessionDate"] = "2026-09-29"
        self.assertNotIn("006208", cacheable_instruments(stale))


if __name__ == "__main__":
    unittest.main()
