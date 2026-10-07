import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

from pages_publication import PublicationError, pages_build_version, prepare_manifest, verify_live


class _Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def wait(self, seconds):
        self.value += max(seconds, 0.001)


class PagesPublicationTests(unittest.TestCase):
    def _site(self, root, *, stamp="2026-10-07T06:40:00+08:00"):
        root.mkdir()
        (root / "index.html").write_bytes(b"<html>skynet</html>\n")
        (root / "data.json").write_bytes(b'{"index":42}\n')
        (root / "status.json").write_text(json.dumps({
            "schemaVersion": 2,
            "status": "ok",
            "service": {"generatedAt": stamp, "windowDate": "2026-10-07", "window": "morning", "commit": "abc", "runId": "101"},
        }, separators=(",", ":")), encoding="utf-8")

    def test_unique_manifest_contains_public_identity_and_core_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            self._site(site)
            manifest = prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/skynet", "GITHUB_RUN_ID": "101",
                "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "abc",
            })
            self.assertEqual(manifest["publicationId"], "owner/skynet:101:2:1")
            self.assertEqual(set(manifest["criticalFiles"]), {"index.html", "data.json", "status.json"})
            self.assertNotEqual(pages_build_version(manifest["publicationId"], 1), pages_build_version(manifest["publicationId"], 2))

    def test_live_readback_checks_manifest_and_public_status_data_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            self._site(site)
            manifest = prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/skynet", "GITHUB_RUN_ID": "101",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "abc",
            })

            def reader(url, *, headers):
                path = unquote(urlsplit(url).path).removeprefix("/skynet/")
                return (site / path).read_bytes()

            clock = _Clock()
            self.assertEqual(verify_live(site, "https://owner.github.io/skynet", reader=reader,
                                        wait=clock.wait, clock=clock.now, max_wait=0), "VERIFIED")
            self.assertNotIn("publication.json", manifest["files"])
            self.assertTrue(manifest["contentHash"])

    def test_newer_verified_manifest_supersedes_old_run_only_after_hash_check(self):
        with tempfile.TemporaryDirectory() as directory:
            old_site = Path(directory) / "old"
            new_site = Path(directory) / "new"
            self._site(old_site, stamp="2026-10-07T05:40:00+08:00")
            self._site(new_site, stamp="2026-10-07T06:40:00+08:00")
            old = prepare_manifest(old_site, env={
                "GITHUB_REPOSITORY": "owner/skynet", "GITHUB_RUN_ID": "101",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "abc",
            })
            prepare_manifest(new_site, env={
                "GITHUB_REPOSITORY": "owner/skynet", "GITHUB_RUN_ID": "102",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "abc",
            })

            def reader(url, *, headers):
                path = unquote(urlsplit(url).path).removeprefix("/skynet/")
                return (new_site / path).read_bytes()

            clock = _Clock()
            self.assertEqual(verify_live(old_site, "https://owner.github.io/skynet", reader=reader,
                                        wait=clock.wait, clock=clock.now, max_wait=0), "SUPERSEDED")
            self.assertEqual(old["runId"], "101")

    def test_content_mismatch_never_returns_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            self._site(site)
            prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/skynet", "GITHUB_RUN_ID": "101",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "abc",
            })

            def reader(url, *, headers):
                path = unquote(urlsplit(url).path).removeprefix("/skynet/")
                if path == "data.json":
                    return b'{"index":0}\n'
                return (site / path).read_bytes()

            clock = _Clock()
            with self.assertRaisesRegex(PublicationError, "PUBLICATION_NOT_VISIBLE"):
                verify_live(site, "https://owner.github.io/skynet", reader=reader,
                            wait=clock.wait, clock=clock.now, max_wait=1)


if __name__ == "__main__":
    unittest.main()
