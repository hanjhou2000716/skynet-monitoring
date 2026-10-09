import json
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from email.message import Message
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from unittest.mock import patch

from pages_publication import PublicationError, _deploy, _oidc, _request_json, pages_build_version, prepare_manifest, verify_live


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

    def test_pages_api_http_error_includes_safe_status_and_reason(self):
        error = HTTPError(
            "https://api.github.com/repos/owner/skynet/pages/deployments",
            422,
            "Validation failed",
            hdrs={"X-GitHub-Request-Id": "ABC-123"},
            fp=BytesIO(b'{"message":"Invalid build version; token ghp_123456789012345678901234567890123456"}'),
        )
        with patch("pages_publication.urlopen", side_effect=error):
            with self.assertRaises(PublicationError) as raised:
                _request_json("POST", "https://api.github.com/example", "not-a-real-token", {})
        self.assertIn("HTTP 422", str(raised.exception))
        self.assertIn("request_id=ABC-123", str(raised.exception))
        self.assertIn("Invalid build version", str(raised.exception))
        self.assertNotIn("ghp_123456789012345678901234567890123456", str(raised.exception))

    def test_oidc_uses_runner_default_audience_without_rewriting_url(self):
        endpoint = "https://actions.example/oidc?api-version=2.0"
        env = {"ACTIONS_ID_TOKEN_REQUEST_URL": endpoint, "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "runner-token"}
        with patch("pages_publication._read_bytes", return_value=b'{"value":"oidc-token"}') as read:
            self.assertEqual(_oidc(env), "oidc-token")
        self.assertEqual(read.call_args.args[0], endpoint)

    def test_pages_api_deployment_uses_source_commit_as_build_version(self):
        source_commit = "a" * 40
        with patch("pages_publication._request_json", return_value={"status_url": "https://api.example/status"}) as request:
            _deploy("owner/skynet", 42, source_commit, "token", "oidc")
        body = request.call_args.args[3]
        self.assertEqual(set(body), {"artifact_id", "pages_build_version", "oidc_token"})
        self.assertEqual(body["pages_build_version"], source_commit)
        self.assertEqual(pages_build_version(source_commit), source_commit)
        with self.assertRaises(PublicationError):
            pages_build_version("short-sha")

    def test_unique_manifest_contains_public_identity_and_core_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            self._site(site)
            (site / ".nojekyll").write_text("", encoding="utf-8")
            manifest = prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/skynet", "GITHUB_RUN_ID": "101",
                "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40,
            })
            self.assertEqual(manifest["publicationId"], "owner/skynet:101:2:1")
            self.assertEqual(manifest["sourceCommit"], "a" * 40)
            self.assertEqual(set(manifest["criticalFiles"]), {"index.html", "data.json", "status.json"})
            self.assertTrue((site / ".nojekyll").is_file())
            self.assertNotIn(".nojekyll", manifest["files"])
            self.assertEqual(pages_build_version(manifest["sourceCommit"]), manifest["sourceCommit"])

    def test_live_readback_checks_manifest_and_public_status_data_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            self._site(site)
            manifest = prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/skynet", "GITHUB_RUN_ID": "101",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40,
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
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40,
            })
            prepare_manifest(new_site, env={
                "GITHUB_REPOSITORY": "owner/skynet", "GITHUB_RUN_ID": "102",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40,
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
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40,
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
