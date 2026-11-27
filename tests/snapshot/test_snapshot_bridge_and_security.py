"""Snapshot tests for Bridge slots, chapter mapping, and strict PII/secret sanitization."""

import json
import re

import database
from app import app
from core.bridge.icons import resolve_source_chapter_idx
from core.bridge.manager import get_bridge_manager
from tests.harness import SnapshotTestCase, TEST_MASTER_API_KEY

_RFC1918_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)
_QUERY_TOKEN_RE = re.compile(r"[?&](?:token|apikey|api_key)=(?!REDACTED\b)[^&\"'\s]+", re.IGNORECASE)


class TestSnapshotBridgeAndSecurity(SnapshotTestCase):
    """Exercise bridge state and assert zero credential/PII/LAN IP leaks in snapshot."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = app.test_client()
        cls.api_headers = {"X-Api-Key": TEST_MASTER_API_KEY}

    def test_01_bridge_slots_and_chapter_resolution_on_real_shapes(self):
        mgr = get_bridge_manager()
        slots = mgr.list_slots()
        self.assertIsInstance(slots, list)
        self.assertGreaterEqual(len(slots), 1)

        total_tracks = 0
        for slot in slots:
            tracks = slot.get("tracks") or []
            total_tracks += len(tracks)
            if tracks:
                # Build synthetic monotonic chapters spanning the real slot's total duration
                last_end = max(float(t.get("end_time") or 10.0) for t in tracks)
                mid = max(1.0, last_end / 2.0)
                chapters = [
                    {"start": 0.0, "end": mid, "title": "Part 1"},
                    {"start": mid, "end": last_end + 10.0, "title": "Part 2"},
                ]
                idx = resolve_source_chapter_idx(
                    chapters,
                    float(tracks[0].get("start_time") or 0.0),
                    float(tracks[0].get("end_time") or 1.0),
                )
                self.assertIn(idx, (0, 1))
        self.assertGreaterEqual(total_tracks, 1)

    def test_02_sqlite_tables_have_zero_unsanitized_secrets_or_ips(self):
        conn = database.get_db_connection()
        try:
            # system_settings credentials blanked or replaced
            settings = database.get_system_settings()
            self.assertEqual(settings.get("master_api_key"), TEST_MASTER_API_KEY)
            for secret_key in (
                "abs_api_key",
                "yoto_client_id",
                "yoto_client_secret",
                "yoto_refresh_token",
                "yoto_access_token",
                "tinker_api_key",
                "gemini_api_key",
                "anthropic_api_key",
                "openrouter_api_key",
                "sso_oidc_client_secret",
                "yoto_bridge_api_key",
                "session_secret",
            ):
                self.assertEqual(
                    settings.get(secret_key, ""),
                    "",
                    f"Sensitive setting {secret_key} was not scrubbed",
                )

            sso_cfg = database.get_sso_config()
            self.assertEqual(sso_cfg.get("extra_trusted_proxies") or [], [])
            self.assertEqual(sso_cfg.get("oidc_client_secret") or "", "")

            # admin_users sanitized
            users = conn.execute(
                "SELECT username, email, display_name, sso_subject, abs_api_key, abs_username, abs_user_id FROM admin_users"
            ).fetchall()
            for row in users:
                self.assertRegex(row["username"], r"^test_user_\d+$")
                if row["email"]:
                    self.assertTrue(row["email"].endswith("@example.test"))
                self.assertRegex(row["display_name"], r"^Test User \d+$")
                self.assertEqual(row["sso_subject"] or "", "")
                self.assertEqual(row["abs_api_key"] or "", "")
                self.assertEqual(row["abs_username"] or "", "")
                self.assertEqual(row["abs_user_id"] or "", "")

            # profiles sanitized
            profiles = conn.execute(
                "SELECT display_name, abs_api_key, abs_username FROM profiles"
            ).fetchall()
            for row in profiles:
                self.assertRegex(row["display_name"], r"^Profile \d+$")
                self.assertEqual(row["abs_api_key"] or "", "")
                self.assertEqual(row["abs_username"] or "", "")

            # esp32_devices sanitized (RFC5737 IPs, test JWT secret & PIN)
            devices = conn.execute(
                "SELECT friendly_name, ip_address, jwt_secret, parent_pin FROM esp32_devices"
            ).fetchall()
            for row in devices:
                self.assertRegex(row["friendly_name"], r"^Test Device \d+$")
                self.assertRegex(row["ip_address"], r"^198\.51\.100\.\d+$")
                self.assertIsNone(_RFC1918_RE.search(row["ip_address"]))
                if row["jwt_secret"]:
                    self.assertTrue(row["jwt_secret"].startswith("test_jwt_secret_"))
                if row["parent_pin"]:
                    self.assertEqual(row["parent_pin"], "1234")

            # bridge_tracks & media_items query tokens & RFC1918 IPs
            track_rows = conn.execute(
                "SELECT stream_url FROM bridge_tracks WHERE stream_url IS NOT NULL"
            ).fetchall()
            for row in track_rows:
                url = row["stream_url"] or ""
                self.assertIsNone(_RFC1918_RE.search(url))
                self.assertIsNone(_QUERY_TOKEN_RE.search(url))

            media_rows = conn.execute(
                "SELECT cover_url, raw_metadata FROM media_items"
            ).fetchall()
            for row in media_rows:
                for val in (row["cover_url"] or "", row["raw_metadata"] or ""):
                    self.assertIsNone(_QUERY_TOKEN_RE.search(val))
        finally:
            conn.close()

    def test_03_api_responses_expose_no_secrets_or_lan_ips(self):
        profiles = database.get_all_profiles()
        first_pid = next(iter(profiles))

        endpoints = [
            "/api/device/config",
            "/api/admin/catalog/matrix",
            f"/api/kids/{first_pid}/catalog?client=kiosk",
        ]
        for ep in endpoints:
            resp = self.client.get(ep, headers=self.api_headers)
            self.assertEqual(resp.status_code, 200, f"Unexpected status on {ep}")
            body_text = json.dumps(resp.get_json())
            self.assertIsNone(_RFC1918_RE.search(body_text), f"RFC1918 IP leaked by {ep}")
            self.assertIsNone(_QUERY_TOKEN_RE.search(body_text), f"Query token leaked by {ep}")
            self.assertNotIn("@", body_text.replace("@example.test", ""), f"Unexpected email leaked by {ep}")
