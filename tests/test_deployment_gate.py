import json
import os
import tempfile
import unittest
from unittest.mock import patch

from deployment_gate import main, should_publish


def status(day="2026-09-30", window="morning", generated="2026-09-30T06:40:00+08:00", commit="abc"):
    return {
        "schemaVersion": 2,
        "generatedAt": generated,
        "service": {
            "status": "ok", "windowDate": day, "window": window,
            "generatedAt": generated, "commit": commit,
        },
    }


class DeploymentGateTests(unittest.TestCase):
    def test_current_main_candidate_can_publish_when_live_status_is_absent(self):
        self.assertEqual(should_publish(status(), "abc", "abc", None), (True, "RUN_NO_LIVE_STATUS"))

    def test_old_commit_is_never_published_after_main_advances(self):
        self.assertEqual(
            should_publish(status(), "abc", "def", None),
            (False, "SKIP_OLD_MAIN_COMMIT"),
        )

    def test_later_live_window_blocks_an_older_queued_run(self):
        older = status(window="morning", generated="2026-09-30T06:40:00+08:00")
        newer = status(window="afternoon", generated="2026-09-30T14:45:00+08:00")
        self.assertEqual(
            should_publish(older, "abc", "abc", newer),
            (False, "SKIP_NEWER_LIVE_PUBLICATION"),
        )

    def test_later_date_blocks_an_older_calendar_day(self):
        older = status(day="2026-09-29", window="afternoon", generated="2026-09-29T14:45:00+08:00")
        newer = status(day="2026-09-30", window="morning", generated="2026-09-30T06:40:00+08:00")
        self.assertEqual(should_publish(older, "abc", "abc", newer)[0], False)

    def test_later_publication_in_same_window_blocks_older_candidate(self):
        older = status(generated="2026-09-30T06:40:00+08:00")
        newer = status(generated="2026-09-30T07:40:00+08:00")
        self.assertEqual(should_publish(older, "abc", "abc", newer)[0], False)

    def test_newer_candidate_is_allowed_and_unverified_live_does_not_block_recovery(self):
        older_live = status(generated="2026-09-30T06:40:00+08:00")
        newer_candidate = status(generated="2026-09-30T07:40:00+08:00")
        self.assertTrue(should_publish(newer_candidate, "abc", "abc", older_live)[0])
        self.assertEqual(
            should_publish(newer_candidate, "abc", "abc", {"status": "legacy"}),
            (True, "RUN_LIVE_STATUS_UNVERIFIED"),
        )

    def test_candidate_commit_must_match_workflow_commit(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            should_publish(status(commit="old"), "abc", "abc", None)

    def test_main_writes_skip_reason_to_github_output(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate_path = os.path.join(directory, "status.json")
            output_path = os.path.join(directory, "output")
            with open(candidate_path, "w", encoding="utf-8") as file:
                json.dump(status(), file)
            with patch.dict(os.environ, {"GITHUB_SHA": "abc", "GITHUB_REPOSITORY": "owner/repo"}, clear=False):
                self.assertEqual(
                    main(candidate_path, output_path, main_sha_fetcher=lambda *_: "abc",
                         status_fetcher=lambda: status(window="afternoon", generated="2026-09-30T14:45:00+08:00")),
                    0,
                )
            with open(output_path, encoding="utf-8") as file:
                self.assertIn("deploy=false", file.read())

    def test_workflow_uses_external_primary_and_github_backup_queue(self):
        workflow_path = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "deploy.yml")
        with open(workflow_path, encoding="utf-8") as file:
            workflow = file.read()
        self.assertIn('cron: "40 23 * * *"', workflow)
        self.assertIn('cron: "45 7 * * *"', workflow)
        self.assertNotIn('cron: "40 22 * * *"', workflow)
        self.assertNotIn('cron: "45 6 * * *"', workflow)
        self.assertIn("queue: max", workflow)
        self.assertIn("deployment_gate.py public/status.json", workflow)
        self.assertIn("needs.build.outputs.publish == 'true'", workflow)
        self.assertIn("verify-publication:", workflow)
        self.assertIn("python pages_publication.py verify-live .private-build", workflow)
        self.assertIn("name: skynet-publication-verification", workflow)


if __name__ == "__main__":
    unittest.main()
