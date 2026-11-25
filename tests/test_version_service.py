"""
Unit tests for core/version_service.py.
Tests SemVer parsing, version resolution hierarchy, and mock GitHub API checks.
"""

import tests.test_isolation  # noqa: F401 - must precede app/core imports
import unittest
from unittest.mock import patch, MagicMock
import os
from pathlib import Path

from core import version_service


class TestVersionService(unittest.TestCase):
    def test_01_parse_semver(self):
        # Stable releases
        p1 = version_service.parse_semver("v1.0.0")
        self.assertEqual(p1, (1, 0, 0, 1, 0, 0))

        p2 = version_service.parse_semver("v1.1.0")
        self.assertEqual(p2, (1, 1, 0, 1, 0, 0))

        # RC & dev releases
        rc1 = version_service.parse_semver("v1.1.0-rc.20261105.1")
        self.assertEqual(rc1, (1, 1, 0, 0, 20261105, 1))

        rc2 = version_service.parse_semver("v1.1.0-rc.20261108.2")
        self.assertEqual(rc2, (1, 1, 0, 0, 20261108, 2))

        dev1 = version_service.parse_semver("v1.2.0-dev.20261123.1")
        self.assertEqual(dev1, (1, 2, 0, 0, 20261123, 1))

        dev2 = version_service.parse_semver("v1.2.0-dev.20261123.2")
        self.assertEqual(dev2, (1, 2, 0, 0, 20261123, 2))
        self.assertGreater(dev2, dev1)

        # Comparisons
        # Stable v1.1.0 is greater than RC v1.1.0-rc.20261108.2
        self.assertGreater(p2, rc2)
        # Newer RC is greater than older RC
        self.assertGreater(rc2, rc1)
        # Newer minor RC is greater than older major stable
        self.assertGreater(rc1, p1)

    def test_02_get_installed_version_resolution(self):
        # Tier 1: Env var priority
        with patch.dict(os.environ, {"APP_VERSION": "v2.5.0"}):
            self.assertEqual(version_service.get_installed_version(), "v2.5.0")

        # Tier 2: Without env var, reads VERSION file matching v[0.0.0]-dev.[yyyymmdd].[iteration]
        expected_version = (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
        with patch.dict(os.environ, {}, clear=True):
            ver = version_service.get_installed_version()
            self.assertEqual(ver, expected_version)
            self.assertRegex(ver, r"^v\d+\.\d+\.\d+-dev\.\d{8}\.\d+$")

    @patch("core.version_service.requests.get")
    def test_03_check_github_updates_mocked_has_update(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "tag_name": "v9.9.9",
                "html_url": "https://github.com/YotoStoryDeck/StoryDeck-Core/releases/tag/v9.9.9",
                "body": "Major update released!",
                "published_at": "2026-11-08T12:00:00Z",
                "draft": False,
                "prerelease": False
            }
        ]
        mock_get.return_value = mock_resp

        # Force fresh check
        res = version_service.check_github_updates(force=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res["has_update"])
        self.assertEqual(res["latest_version"], "v9.9.9")
        self.assertIn("v9.9.9", res["release_url"])

    @patch("core.version_service.requests.get")
    def test_04_check_github_updates_offline_graceful(self, mock_get):
        mock_get.side_effect = Exception("Network unreachable")

        res = version_service.check_github_updates(force=True)
        self.assertTrue(res["ok"])
        self.assertFalse(res["has_update"])
        self.assertIsNotNone(res["error"])
        self.assertIn("Network unreachable", res["error"])

    def test_05_flask_version_routes(self):
        from app import app
        client = app.test_client()

        # Public /version probe
        expected_version = (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
        res = client.get("/version")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("version", data)
        self.assertEqual(data["version"], expected_version)

        # Health endpoint includes version
        h_res = client.get("/health")
        self.assertEqual(h_res.status_code, 200)
        h_data = h_res.get_json()
        self.assertEqual(h_data.get("status"), "healthy")
        self.assertIn("version", h_data)

        # Admin version endpoint requires auth
        unauth = client.get("/api/admin/version")
        self.assertEqual(unauth.status_code, 401)


if __name__ == "__main__":
    unittest.main()
