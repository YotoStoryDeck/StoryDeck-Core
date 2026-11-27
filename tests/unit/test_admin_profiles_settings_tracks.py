"""Unit tests for admin profiles, settings, tracks, scheduler settings, and version service."""
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from tests.harness import IsolatedTestCase
from app import app
import clients
from core import version_service
import database


class TestAdminProfilesSettingsTracksAndVersion(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "admin"
            sess["username"] = "admin"
            sess["role"] = "admin"

    def test_01_refresh_settings_endpoints(self):
        """GET and PUT /api/admin/settings/catalog validate refresh intervals and persistence."""
        res = self.client.get("/api/admin/settings/catalog")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["auto_library_refresh_hours"], 6.0)
        self.assertTrue(data["auto_library_refresh_enabled"])
        self.assertIn("scheduler_status", data)

        put_res = self.client.put("/api/admin/settings/catalog", json={
            "auto_library_refresh_enabled": False,
            "auto_library_refresh_hours": 12.0,
        })
        self.assertEqual(put_res.status_code, 200)
        self.assertFalse(put_res.get_json()["auto_library_refresh_enabled"])
        self.assertEqual(database.get_system_settings()["auto_library_refresh_hours"], 12.0)

        self.client.put("/api/admin/settings/catalog", json={
            "auto_library_refresh_enabled": True,
            "auto_library_refresh_hours": 6.0,
        })

    def test_02_admin_profiles_crud_and_avatar_generation(self):
        """Covers routes/admin_profiles.py profile CRUD, base64 avatar upload, and /api/avatar/<id>."""
        buf = io.BytesIO()
        Image.new("RGB", (40, 40), (30, 120, 220)).save(buf, format="PNG")
        b64_avatar = f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"

        put_res = self.client.put("/api/admin/profiles/new_kid", json={
            "display_name": "New Kid",
            "avatar_color": "#2563eb",
            "avatar_initial": "N",
            "avatar_url": b64_avatar,
            "default_playback_mode": "streaming",
            "catalog_rotation_limit": 40,
        })
        self.assertIn(put_res.status_code, (200, 201))

        list_res = self.client.get("/api/admin/profiles")
        self.assertEqual(list_res.status_code, 200)
        profiles_map = list_res.get_json()
        self.assertIn("new_kid", profiles_map)
        self.assertEqual(profiles_map["new_kid"]["display_name"], "New Kid")

        av_res = self.client.get("/api/avatar/new_kid")
        self.assertEqual(av_res.status_code, 200)
        self.assertEqual(av_res.mimetype, "image/jpeg")
        av_res.close()

        del_res = self.client.delete("/api/admin/profiles/new_kid")
        self.assertEqual(del_res.status_code, 200)
        self.assertIsNone(database.get_profile_by_id("new_kid"))

    def test_03_admin_abs_permission_status_and_tracks_custom_icon_versioning(self):
        """Covers routes/admin_settings.py /abs/permission-status and routes/admin_tracks.py custom icon URLs."""
        with patch.object(clients, "abs_client", None):
            perm_res = self.client.get("/api/admin/abs/permission-status")
            self.assertEqual(perm_res.status_code, 200)
            self.assertFalse(perm_res.get_json()["connected"])

        media_id = "track_test_book_1"
        database.upsert_media_items([{"id": media_id, "type": "abs", "title": "Tracked Book", "author": "A"}])
        fake_item = {
            "id": media_id,
            "media": {
                "metadata": {"title": "Tracked Book", "authorName": "A"},
                "chapters": [{"start": 0.0, "end": 300.0, "title": "Chapter 1: Dragon"}],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            icon_file = tmp_path / f"{media_id}_0.png"
            buf = io.BytesIO()
            Image.new("RGBA", (16, 16), (0, 200, 100, 255)).save(buf, format="PNG")
            icon_file.write_bytes(buf.getvalue())
            expected_v = str(int(icon_file.stat().st_mtime))

            with patch("core.config.CUSTOM_ICONS_DIR", tmp_path), \
                 patch.object(clients.abs_client, "get_item", return_value=fake_item):
                tr_res = self.client.get(f"/api/admin/media/{media_id}/tracks")
                self.assertEqual(tr_res.status_code, 200)
                tr_data = tr_res.get_json()
                self.assertTrue(tr_data["ok"])
                track0 = tr_data["tracks"][0]
                self.assertTrue(track0["has_custom_icon"])
                self.assertIn(f"&v={expected_v}", track0["icon_url"])

                with patch("icon_engine.IconEngine.search_all_sources", return_value=[{"name": "dragon", "url": "https://example.invalid/dragon.png"}]):
                    am_res = self.client.post(f"/api/admin/media/{media_id}/auto-match-icons", json={})
                    self.assertEqual(am_res.status_code, 200)
                    am_data = am_res.get_json()
                    self.assertEqual(am_data["matched_count"], 1)

    def test_04_version_service_semver_resolution_and_routes(self):
        """Tests SemVer parsing, VERSION resolution hierarchy, mocked GitHub checks, and Flask /version routes."""
        p1 = version_service.parse_semver("v1.0.0")
        p2 = version_service.parse_semver("v1.1.0")
        rc1 = version_service.parse_semver("v1.1.0-rc.20261105.1")
        rc2 = version_service.parse_semver("v1.1.0-rc.20261108.2")
        dev1 = version_service.parse_semver("v1.2.0-dev.20261123.1")
        dev2 = version_service.parse_semver("v1.2.0-dev.20261123.2")
        self.assertGreater(dev2, dev1)
        self.assertGreater(p2, rc2)
        self.assertGreater(rc2, rc1)
        self.assertGreater(rc1, p1)

        with patch.dict(os.environ, {"APP_VERSION": "v2.5.0"}):
            self.assertEqual(version_service.get_installed_version(), "v2.5.0")

        expected_version = (Path(__file__).resolve().parent.parent.parent / "VERSION").read_text(encoding="utf-8").strip()
        with patch.dict(os.environ, {}, clear=True):
            ver = version_service.get_installed_version()
            self.assertEqual(ver, expected_version)

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [{"tag_name": "v9.9.9", "html_url": "https://github.com/YotoStoryDeck/StoryDeck-Core/releases/tag/v9.9.9", "body": "Major update!", "published_at": "2026-11-08T12:00:00Z", "draft": False, "prerelease": False}]
        with patch("core.version_service.requests.get", return_value=mock_resp):
            res_up = version_service.check_github_updates(force=True)
            self.assertTrue(res_up["has_update"])
            self.assertEqual(res_up["latest_version"], "v9.9.9")

        with patch("core.version_service.requests.get", side_effect=Exception("Network unreachable")):
            res_off = version_service.check_github_updates(force=True)
            self.assertTrue(res_off["ok"])
            self.assertFalse(res_off["has_update"])
            self.assertIn("Network unreachable", res_off["error"])

        anon = app.test_client()
        self.assertEqual(anon.get("/version").get_json()["version"], expected_version)
        self.assertEqual(anon.get("/health").get_json()["status"], "healthy")
        self.assertEqual(anon.get("/api/admin/version").status_code, 401)


if __name__ == "__main__":
    unittest.main()
